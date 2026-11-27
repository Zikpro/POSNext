# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

"""POS Role Rule validation: the discount percent range and one enabled rule per role and till.

Run with either:
    bench --site <site> run-tests --doctype "POS Role Rule"
    bench --site <site> run-tests --module pos_next.pos_next.doctype.pos_role_rule.test_pos_role_rule

Every test is rolled back to a savepoint (see RoleRulesTestCase), so nothing is kept.
"""

import frappe

from pos_next.api.test_role_rules import (
	ROLE_CASHIER,
	ROLE_SUPERVISOR,
	TILL_A,
	TILL_B,
	RoleRulesTestCase,
	make_rule,
)

# Frappe would otherwise create and commit test records for the doctypes POS Role Rule
# links to (a POS Profile pulls in a company, warehouses and accounts). These tests make
# their own roles and use made-up till names, so none are needed.
IGNORE_TEST_RECORD_DEPENDENCIES = ["Role", "POS Profile"]


class TestPOSRoleRuleValidation(RoleRulesTestCase):
	def test_percent_above_100_is_refused_and_names_field_and_value(self):
		with self.assertRaisesRegex(frappe.ValidationError, r"Max Discount %.*between 0 and 100, not 100\.01"):
			make_rule(ROLE_CASHIER, max_discount_percent=100.01)

	def test_negative_percent_is_refused(self):
		with self.assertRaisesRegex(frappe.ValidationError, "not -1"):
			make_rule(ROLE_CASHIER, max_discount_percent=-1)

	def test_0_and_100_are_accepted(self):
		make_rule(ROLE_CASHIER, max_discount_percent=0)
		make_rule(ROLE_SUPERVISOR, max_discount_percent=100)

	def test_second_enabled_every_till_rule_for_a_role_is_refused(self):
		first = make_rule(ROLE_CASHIER, max_discount_percent=5)
		with self.assertRaises(frappe.ValidationError) as cm:
			make_rule(ROLE_CASHIER, max_discount_percent=10)
		message = str(cm.exception)
		self.assertIn(ROLE_CASHIER, message)
		self.assertIn("every till", message)
		self.assertIn(first.name, message)

	def test_second_enabled_rule_for_the_same_till_is_refused(self):
		make_rule(ROLE_CASHIER, TILL_A, max_discount_percent=5)
		with self.assertRaisesRegex(frappe.ValidationError, TILL_A):
			make_rule(ROLE_CASHIER, TILL_A, max_discount_percent=10)

	def test_every_till_rule_and_one_till_rule_can_both_exist(self):
		make_rule(ROLE_CASHIER, max_discount_percent=5)
		make_rule(ROLE_CASHIER, TILL_A, max_discount_percent=10)
		make_rule(ROLE_CASHIER, TILL_B, max_discount_percent=15)

	def test_a_disabled_rule_does_not_count_as_a_duplicate(self):
		make_rule(ROLE_CASHIER, max_discount_percent=5, enabled=0)
		make_rule(ROLE_CASHIER, max_discount_percent=10)
