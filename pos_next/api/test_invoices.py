# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

"""validate_manual_rate_edit (pos_next.api.invoices): a manual price change at the till.

Checked on its own here, separately from the Sales Invoice validate hook, which also
refuses price changes (pos_next.api.discount_rules). Covers the permission, the discount
limit, and the fallback to POS Settings when no POS Role Rule applies to the user.

Runs as test users holding one role each, never as Administrator; every test is rolled
back to a savepoint.
"""

import frappe

from pos_next.api.invoices import validate_manual_rate_edit
from pos_next.api.test_role_rules import (
	ROLE_CASHIER,
	ROLE_MANAGER,
	TILL_A,
	RoleRulesTestCase,
	make_pos_settings,
	make_rule,
	make_user,
)

UNKNOWN_TILL = "_Test POS Rule Till Without Settings"


def edit(rate, original_rate=100, **values):
	item = {"is_rate_manually_edited": 1, "item_code": "ITEM-1", "rate": rate, "original_rate": original_rate}
	item.update(values)
	return item


def rules(**values):
	base = {
		"allow_any_discount": 0,
		"max_discount_percent": 10,
		"allow_item_discount": 1,
		"allow_additional_discount": 1,
		"allow_rate_edit": 1,
		"allow_return_without_receipt": 0,
		"allow_award_loyalty_points": 0,
		"source": "rules",
	}
	base.update(values)
	return base


class TestValidateManualRateEditWithRules(RoleRulesTestCase):
	"""The rules passed in (as update_invoice does)."""

	def check(self, item, **rule_values):
		return validate_manual_rate_edit(item, TILL_A, rules(**rule_values))

	def test_not_a_manual_edit_is_always_valid(self):
		self.assertTrue(self.check(edit(50, is_rate_manually_edited=0), allow_rate_edit=0)["valid"])

	def test_rate_must_be_above_zero(self):
		result = self.check(edit(0))
		self.assertFalse(result["valid"])
		self.assertIn("must be greater than zero", result["message"])

	def test_pos_profile_is_required(self):
		result = validate_manual_rate_edit(edit(95), None, rules())
		self.assertFalse(result["valid"])
		self.assertIn("POS Profile is required", result["message"])

	def test_permission_denied(self):
		result = self.check(edit(95), allow_rate_edit=0)
		self.assertFalse(result["valid"])
		self.assertIn("not allowed to change the price of ITEM-1", result["message"])

	def test_permission_allowed_price_increase_ignores_the_limit(self):
		self.assertTrue(self.check(edit(120), max_discount_percent=0)["valid"])

	def test_reduction_at_the_limit_is_valid(self):
		self.assertTrue(self.check(edit(90))["valid"])

	def test_reduction_just_above_the_limit_is_refused(self):
		result = self.check(edit(89.99))
		self.assertFalse(result["valid"])
		self.assertEqual(result["message"], "Discount 10.01% on ITEM-1 (price change) is above your limit of 10%.")

	def test_a_limit_of_0_refuses_any_reduction(self):
		self.assertFalse(self.check(edit(99), max_discount_percent=0)["valid"])

	def test_any_discount_allows_a_large_reduction(self):
		self.assertTrue(self.check(edit(10), allow_any_discount=1)["valid"])

	def test_rounding_never_lets_a_price_change_go_over_the_limit(self):
		# 10% off 0.99 is 0.891: 0.90 is the lowest price allowed (rounded UP).
		self.assertTrue(self.check(edit(0.90, original_rate=0.99))["valid"])
		self.assertFalse(self.check(edit(0.89, original_rate=0.99))["valid"])
		# 10% off 4.49 is 4.041: 4.05 allowed, 4.04 (10.02%) refused.
		self.assertTrue(self.check(edit(4.05, original_rate=4.49))["valid"])
		self.assertFalse(self.check(edit(4.04, original_rate=4.49))["valid"])

	def test_price_list_rate_is_used_when_there_is_no_original_rate(self):
		item = edit(85, original_rate=None, price_list_rate=100)
		self.assertFalse(self.check(item)["valid"])


class TestValidateManualRateEditForTheUser(RoleRulesTestCase):
	"""No rules passed in: the session user's own rules are worked out."""

	def check_as(self, user, item, pos_profile=TILL_A):
		frappe.set_user(user)
		try:
			return validate_manual_rate_edit(item, pos_profile)
		finally:
			frappe.set_user("Administrator")

	def test_the_users_rule_is_used(self):
		cashier = make_user("pos-rule-cashier@example.com", ROLE_CASHIER)
		make_rule(ROLE_CASHIER, allow_rate_edit=1, max_discount_percent=5)
		self.assertTrue(self.check_as(cashier, edit(95))["valid"])
		self.assertFalse(self.check_as(cashier, edit(94))["valid"])

	def test_the_users_rule_can_deny_the_change(self):
		cashier = make_user("pos-rule-cashier@example.com", ROLE_CASHIER)
		make_rule(ROLE_CASHIER, allow_rate_edit=0, max_discount_percent=50)
		self.assertFalse(self.check_as(cashier, edit(99))["valid"])

	def test_the_most_generous_role_wins(self):
		user = make_user("pos-rule-two@example.com", ROLE_CASHIER, ROLE_MANAGER)
		make_rule(ROLE_CASHIER, allow_rate_edit=0, max_discount_percent=5)
		make_rule(ROLE_MANAGER, allow_rate_edit=1, allow_any_discount=1)
		self.assertTrue(self.check_as(user, edit(40))["valid"])

	def test_system_manager_may_change_any_price_once_the_site_has_rules(self):
		admin = make_user("pos-rule-admin@example.com", "System Manager")
		make_rule(ROLE_CASHIER, allow_rate_edit=0)
		self.assertTrue(self.check_as(admin, edit(20))["valid"])

	# Fallback: no POS Role Rule applies, so POS Settings decides, as before.

	def test_fallback_pos_settings_with_no_limit(self):
		cashier = make_user("pos-rule-cashier@example.com", ROLE_CASHIER)
		make_pos_settings(TILL_A, allow_user_to_edit_rate=1, max_discount_allowed=0)
		self.assertTrue(self.check_as(cashier, edit(30))["valid"])  # POS Settings 0 = no limit

	def test_fallback_pos_settings_limit(self):
		cashier = make_user("pos-rule-cashier@example.com", ROLE_CASHIER)
		make_pos_settings(TILL_A, allow_user_to_edit_rate=1, max_discount_allowed=10)
		self.assertTrue(self.check_as(cashier, edit(90))["valid"])
		result = self.check_as(cashier, edit(85))
		self.assertFalse(result["valid"])
		self.assertIn("above your limit of 10%", result["message"])

	def test_fallback_pos_settings_rate_editing_off(self):
		cashier = make_user("pos-rule-cashier@example.com", ROLE_CASHIER)
		make_pos_settings(TILL_A, allow_user_to_edit_rate=0)
		self.assertFalse(self.check_as(cashier, edit(99))["valid"])

	def test_fallback_without_pos_settings_refuses(self):
		# No POS Settings for this till: the defaults keep price editing off.
		cashier = make_user("pos-rule-cashier@example.com", ROLE_CASHIER)
		self.assertFalse(self.check_as(cashier, edit(99), pos_profile=UNKNOWN_TILL)["valid"])

	def test_a_rule_for_another_role_does_not_replace_the_fallback(self):
		cashier = make_user("pos-rule-cashier@example.com", ROLE_CASHIER)
		make_rule(ROLE_MANAGER, allow_rate_edit=1, allow_any_discount=1)
		make_pos_settings(TILL_A, allow_user_to_edit_rate=1, max_discount_allowed=10)
		self.assertFalse(self.check_as(cashier, edit(85))["valid"])
