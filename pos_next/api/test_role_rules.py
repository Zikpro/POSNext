# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

"""POS Role Rule: working out a user's till powers, and sending them to the till.

Runs as test users holding one role each, never as Administrator. Every test is rolled
back to a savepoint, so nothing it creates is kept. Test roles, users and till names are
made up here; nothing depends on a particular site.

The helpers and RoleRulesTestCase are shared with the other POS Role Rule tests. The
doctype's own validation tests are in pos_next/pos_next/doctype/pos_role_rule/
test_pos_role_rule.py, so `bench run-tests --doctype "POS Role Rule"` finds them.
"""

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import cint

from pos_next.api import role_rules

TILL_A = "_Test POS Rule Till A"
TILL_B = "_Test POS Rule Till B"
ROLE_CASHIER = "_Test POS Rule Cashier"
ROLE_SUPERVISOR = "_Test POS Rule Supervisor"
ROLE_MANAGER = "_Test POS Rule Manager"


def make_role(name):
	if not frappe.db.exists("Role", name):
		frappe.get_doc({"doctype": "Role", "role_name": name, "desk_access": 1}).insert(ignore_permissions=True)
	return name


def make_user(email, *roles):
	user = frappe.get_doc({
		"doctype": "User",
		"email": email,
		"first_name": email.split("@")[0],
		"send_welcome_email": 0,
		"roles": [{"role": role} for role in roles],
	})
	user.flags.ignore_permissions = True
	user.insert()
	frappe.clear_cache(user=email)
	return email


def make_rule(role, pos_profile=None, **values):
	doc = frappe.get_doc({"doctype": "POS Role Rule", "role": role, "pos_profile": pos_profile, "enabled": 1, **values})
	doc.flags.ignore_links = True  # the test tills are not real POS Profiles
	doc.insert(ignore_permissions=True)
	return doc


def make_pos_settings(pos_profile, **values):
	doc = frappe.get_doc({
		"doctype": "POS Settings",
		"pos_profile": pos_profile,
		"enabled": 1,
		# Keep the site's Stock Settings untouched (POS Settings syncs this value on save).
		"allow_negative_stock": cint(frappe.db.get_single_value("Stock Settings", "allow_negative_stock")),
		**values,
	})
	doc.flags.ignore_links = True
	doc.insert(ignore_permissions=True)
	return doc


class RoleRulesTestCase(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		frappe.db.savepoint("role_rules_test")
		# A clean slate: this test decides which rules exist.
		frappe.db.delete("POS Role Rule")
		role_rules.clear_role_rules_cache()
		for role in (ROLE_CASHIER, ROLE_SUPERVISOR, ROLE_MANAGER):
			make_role(role)

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback(save_point="role_rules_test")
		role_rules.clear_role_rules_cache()
		super().tearDown()

	def rules(self, user, pos_profile=TILL_A):
		role_rules.clear_role_rules_cache()
		return role_rules.get_effective_rules(user, pos_profile)


class TestEffectiveRules(RoleRulesTestCase):
	def test_one_role_gets_its_rule(self):
		user = make_user("pos-rule-cashier@example.com", ROLE_CASHIER)
		make_rule(ROLE_CASHIER, max_discount_percent=10, allow_item_discount=1)
		rules = self.rules(user)
		self.assertEqual(rules["source"], role_rules.SOURCE_RULES)
		self.assertEqual(rules["allow_any_discount"], 0)
		self.assertEqual(rules["max_discount_percent"], 10)
		self.assertEqual(rules["allow_item_discount"], 1)
		for flag in ("allow_additional_discount", "allow_rate_edit", "allow_return_without_receipt", "allow_award_loyalty_points"):
			self.assertEqual(rules[flag], 0, flag)
		self.assertEqual(role_rules.discount_limit(rules), 10)

	def test_a_till_rule_replaces_the_every_till_rule_for_that_role(self):
		user = make_user("pos-rule-cashier@example.com", ROLE_CASHIER)
		make_rule(ROLE_CASHIER, max_discount_percent=5, allow_rate_edit=1)
		make_rule(ROLE_CASHIER, TILL_A, max_discount_percent=20)
		at_a = self.rules(user, TILL_A)
		self.assertEqual(at_a["max_discount_percent"], 20)
		self.assertEqual(at_a["allow_rate_edit"], 0)  # replaced, not merged
		at_b = self.rules(user, TILL_B)
		self.assertEqual(at_b["max_discount_percent"], 5)
		self.assertEqual(at_b["allow_rate_edit"], 1)

	def test_a_till_rule_replaces_the_every_till_rule_whichever_was_made_first(self):
		user = make_user("pos-rule-cashier@example.com", ROLE_CASHIER)
		make_rule(ROLE_CASHIER, TILL_A, max_discount_percent=20)
		make_rule(ROLE_CASHIER, max_discount_percent=5, allow_rate_edit=1)
		at_a = self.rules(user, TILL_A)
		self.assertEqual(at_a["max_discount_percent"], 20)
		self.assertEqual(at_a["allow_rate_edit"], 0)

	def test_two_roles_the_more_generous_wins(self):
		user = make_user("pos-rule-two@example.com", ROLE_CASHIER, ROLE_SUPERVISOR)
		make_rule(ROLE_CASHIER, max_discount_percent=5, allow_item_discount=1)
		make_rule(ROLE_SUPERVISOR, max_discount_percent=15, allow_rate_edit=1, allow_return_without_receipt=1)
		rules = self.rules(user)
		self.assertEqual(rules["max_discount_percent"], 15)
		self.assertEqual(rules["allow_item_discount"], 1)
		self.assertEqual(rules["allow_rate_edit"], 1)
		self.assertEqual(rules["allow_return_without_receipt"], 1)
		self.assertEqual(rules["allow_any_discount"], 0)

	def test_any_discount_beats_a_percent(self):
		user = make_user("pos-rule-two@example.com", ROLE_SUPERVISOR, ROLE_MANAGER)
		make_rule(ROLE_SUPERVISOR, max_discount_percent=15)
		make_rule(ROLE_MANAGER, allow_any_discount=1)
		rules = self.rules(user)
		self.assertEqual(rules["allow_any_discount"], 1)
		self.assertIsNone(role_rules.discount_limit(rules))

	def test_a_disabled_rule_is_ignored(self):
		user = make_user("pos-rule-cashier@example.com", ROLE_CASHIER)
		make_pos_settings(TILL_A, max_discount_allowed=7)
		make_rule(ROLE_CASHIER, max_discount_percent=50, enabled=0)
		rules = self.rules(user)
		self.assertEqual(rules["source"], role_rules.SOURCE_POS_SETTINGS)
		self.assertEqual(rules["max_discount_percent"], 7)

	def test_user_without_a_matching_rule_gets_todays_pos_settings(self):
		user = make_user("pos-rule-cashier@example.com", ROLE_CASHIER)
		make_rule(ROLE_MANAGER, allow_any_discount=1)  # rules exist, but not for this user's role
		make_pos_settings(
			TILL_A,
			max_discount_allowed=12,
			allow_user_to_edit_item_discount=1,
			allow_user_to_edit_additional_discount=0,
			allow_user_to_edit_rate=1,
			allow_return_without_invoice=1,
		)
		rules = self.rules(user)
		self.assertEqual(rules["source"], role_rules.SOURCE_POS_SETTINGS)
		self.assertEqual(
			{k: rules[k] for k in role_rules.RULE_KEYS},
			{
				"allow_any_discount": 0,
				"max_discount_percent": 12,
				"allow_item_discount": 1,
				"allow_additional_discount": 0,
				"allow_rate_edit": 1,
				"allow_return_without_receipt": 1,
				"allow_award_loyalty_points": 0,
			},
		)

	def test_pos_settings_0_means_no_limit(self):
		user = make_user("pos-rule-cashier@example.com", ROLE_CASHIER)
		make_pos_settings(TILL_A, max_discount_allowed=0)
		rules = self.rules(user)
		self.assertEqual(rules["allow_any_discount"], 1)

	def test_system_manager_may_do_everything_once_the_site_has_rules(self):
		user = make_user("pos-rule-admin@example.com", "System Manager")
		make_rule(ROLE_CASHIER, max_discount_percent=5)
		rules = self.rules(user)
		self.assertEqual(rules["source"], role_rules.SOURCE_FULL_ACCESS)
		self.assertEqual(rules["allow_any_discount"], 1)
		for flag in role_rules.RULE_FLAGS:
			self.assertEqual(rules[flag], 1, flag)

	def test_system_manager_on_a_site_without_rules_keeps_pos_settings_and_may_award_points(self):
		user = make_user("pos-rule-admin@example.com", "System Manager")
		make_pos_settings(TILL_A, max_discount_allowed=10, allow_user_to_edit_rate=0)
		rules = self.rules(user)
		self.assertEqual(rules["source"], role_rules.SOURCE_POS_SETTINGS)
		self.assertEqual(rules["max_discount_percent"], 10)
		self.assertEqual(rules["allow_rate_edit"], 0)
		self.assertEqual(rules["allow_award_loyalty_points"], 1)

	def test_saving_a_rule_clears_the_cached_rules(self):
		user = make_user("pos-rule-cashier@example.com", ROLE_CASHIER)
		make_pos_settings(TILL_A, max_discount_allowed=7)
		self.assertEqual(self.rules(user)["max_discount_percent"], 7)
		self.assertEqual(role_rules.get_effective_rules(user, TILL_A)["max_discount_percent"], 7)  # cached
		rule = make_rule(ROLE_CASHIER, max_discount_percent=25)
		self.assertEqual(role_rules.get_effective_rules(user, TILL_A)["max_discount_percent"], 25)
		rule.delete(ignore_permissions=True)
		self.assertEqual(role_rules.get_effective_rules(user, TILL_A)["max_discount_percent"], 7)


class TestRulesSentToTheTill(RoleRulesTestCase):
	def test_no_rules_on_the_site_leaves_the_till_settings_unchanged(self):
		user = make_user("pos-rule-cashier@example.com", ROLE_CASHIER)
		make_pos_settings(
			TILL_A,
			max_discount_allowed=10,
			allow_user_to_edit_item_discount=1,
			allow_user_to_edit_additional_discount=1,
			allow_user_to_edit_rate=1,
		)
		frappe.set_user(user)
		from pos_next.api.pos_profile import get_pos_settings

		role_rules.clear_role_rules_cache()
		settings = get_pos_settings(TILL_A)
		self.assertEqual(settings["max_discount_allowed"], 10)
		self.assertEqual(settings["allow_user_to_edit_item_discount"], 1)
		self.assertEqual(settings["allow_user_to_edit_additional_discount"], 1)
		self.assertEqual(settings["allow_user_to_edit_rate"], 1)
		self.assertEqual(settings["role_rules_source"], role_rules.SOURCE_POS_SETTINGS)

	def test_rules_overlay_the_settings_the_till_already_reads(self):
		user = make_user("pos-rule-cashier@example.com", ROLE_CASHIER)
		make_pos_settings(
			TILL_A,
			max_discount_allowed=0,
			allow_user_to_edit_item_discount=1,
			allow_user_to_edit_additional_discount=1,
			allow_user_to_edit_rate=1,
		)
		make_rule(ROLE_CASHIER, max_discount_percent=10, allow_item_discount=1, allow_additional_discount=0)
		frappe.set_user(user)
		from pos_next.api.pos_profile import get_pos_settings

		role_rules.clear_role_rules_cache()
		settings = get_pos_settings(TILL_A)
		self.assertEqual(settings["role_rules_source"], role_rules.SOURCE_RULES)
		self.assertEqual(settings["max_discount_percent"], 10)
		self.assertEqual(settings["max_discount_allowed"], 10)
		self.assertEqual(settings["allow_user_to_edit_item_discount"], 1)
		self.assertEqual(settings["allow_user_to_edit_additional_discount"], 0)
		self.assertEqual(settings["allow_user_to_edit_rate"], 0)
		self.assertEqual(settings["allow_return_without_invoice"], 0)

	def test_a_limit_of_0_switches_discounts_off(self):
		settings = {"max_discount_allowed": 50}
		user = make_user("pos-rule-cashier@example.com", ROLE_CASHIER)
		make_rule(ROLE_CASHIER, max_discount_percent=0, allow_item_discount=1, allow_additional_discount=1)
		role_rules.clear_role_rules_cache()
		role_rules.apply_rules_to_settings(settings, TILL_A, user)
		self.assertEqual(settings["allow_user_to_edit_item_discount"], 0)
		self.assertEqual(settings["allow_user_to_edit_additional_discount"], 0)

	def test_any_discount_is_sent_as_no_limit(self):
		settings = {"max_discount_allowed": 50}
		user = make_user("pos-rule-manager@example.com", ROLE_MANAGER)
		make_rule(ROLE_MANAGER, allow_any_discount=1, allow_item_discount=1, allow_rate_edit=1, allow_return_without_receipt=1)
		role_rules.clear_role_rules_cache()
		role_rules.apply_rules_to_settings(settings, TILL_A, user)
		self.assertEqual(settings["max_discount_allowed"], 0)
		self.assertEqual(settings["allow_user_to_edit_item_discount"], 1)
		self.assertEqual(settings["allow_user_to_edit_rate"], 1)
		self.assertEqual(settings["allow_return_without_invoice"], 1)

	def test_bootstrap_sends_the_users_rules(self):
		from pos_next.api.bootstrap import _get_pos_settings

		user = make_user("pos-rule-supervisor@example.com", ROLE_SUPERVISOR)
		make_pos_settings(TILL_A, max_discount_allowed=0)
		make_rule(ROLE_SUPERVISOR, max_discount_percent=15, allow_rate_edit=1)
		frappe.set_user(user)
		role_rules.clear_role_rules_cache()
		profile = frappe._dict(name=TILL_A, write_off_account=None, write_off_limit=0, disable_rounded_total=0)
		settings = _get_pos_settings(profile)
		self.assertEqual(settings["max_discount_percent"], 15)
		self.assertEqual(settings["allow_user_to_edit_rate"], 1)
		self.assertEqual(settings["role_rules_source"], role_rules.SOURCE_RULES)

	def test_the_pos_settings_editor_still_gets_the_saved_values(self):
		from pos_next.pos_next.doctype.pos_settings.pos_settings import get_pos_settings

		# A System Manager may open the editor; with rules on the site they have full till powers.
		user = make_user("pos-rule-admin@example.com", "System Manager")
		make_pos_settings(TILL_A, max_discount_allowed=10, allow_user_to_edit_rate=0)
		make_rule(ROLE_CASHIER, max_discount_percent=5)
		frappe.set_user(user)
		role_rules.clear_role_rules_cache()
		editor = get_pos_settings(TILL_A)
		self.assertEqual(editor["max_discount_allowed"], 10)
		self.assertEqual(editor["allow_user_to_edit_rate"], 0)
		self.assertNotIn("role_rules_source", editor)
		till = get_pos_settings(TILL_A, for_till=1)
		self.assertEqual(till["role_rules_source"], role_rules.SOURCE_FULL_ACCESS)
		self.assertEqual(till["max_discount_allowed"], 0)
		self.assertEqual(till["allow_user_to_edit_rate"], 1)


class TestAwardLoyaltyPoints(RoleRulesTestCase):
	"""Bonus loyalty points (item 7): "Award Loyalty Points" on the user's rule, no role names in code."""

	def award(self, user, customer, points=10):
		from pos_next.api.loyalty_award import award_loyalty_points

		frappe.set_user(user)
		role_rules.clear_role_rules_cache()
		try:
			return award_loyalty_points(customer, points, "Test goodwill")
		finally:
			frappe.set_user("Administrator")

	def make_loyal_customer(self):
		company = frappe.db.get_value("Company", {}, "name")
		program = frappe.get_doc({
			"doctype": "Loyalty Program", "loyalty_program_name": "_Test POS Rule Loyalty", "company": company,
			"from_date": frappe.utils.today(), "loyalty_program_type": "Single Tier Program",
			"conversion_factor": 1, "expiry_duration": 30,
			"collection_rules": [{"tier_name": "Base", "collection_factor": 1, "min_spent": 0}],
		}).insert(ignore_permissions=True)
		return frappe.get_doc({
			"doctype": "Customer", "customer_name": "_Test POS Rule Loyal Customer", "customer_type": "Individual",
			"customer_group": frappe.db.get_value("Customer Group", {"is_group": 0}, "name"),
			"territory": frappe.db.get_value("Territory", {"is_group": 0}, "name"),
			"loyalty_program": program.name,
		}).insert(ignore_permissions=True).name

	def test_the_rule_decides_who_may_award(self):
		cashier = make_user("pos-rule-cashier@example.com", ROLE_CASHIER)
		manager = make_user("pos-rule-manager@example.com", ROLE_MANAGER)
		make_rule(ROLE_CASHIER, allow_award_loyalty_points=0)
		make_rule(ROLE_MANAGER, TILL_A, allow_award_loyalty_points=1)  # a rule for one till counts too
		role_rules.clear_role_rules_cache()
		self.assertFalse(role_rules.can_award_loyalty_points(cashier))
		self.assertTrue(role_rules.can_award_loyalty_points(manager))

	def test_system_manager_always_may_and_no_rule_means_no(self):
		admin = make_user("pos-rule-admin@example.com", "System Manager")
		other = make_user("pos-rule-two@example.com", ROLE_SUPERVISOR)
		self.assertTrue(role_rules.can_award_loyalty_points(admin))
		self.assertFalse(role_rules.can_award_loyalty_points(other))

	def test_a_disabled_rule_does_not_count(self):
		cashier = make_user("pos-rule-cashier@example.com", ROLE_CASHIER)
		rule = make_rule(ROLE_CASHIER, allow_award_loyalty_points=1)
		rule.db_set("enabled", 0)
		role_rules.clear_role_rules_cache()
		self.assertFalse(role_rules.can_award_loyalty_points(cashier))

	def test_award_is_refused_without_the_tick(self):
		cashier = make_user("pos-rule-cashier@example.com", ROLE_CASHIER)
		make_rule(ROLE_CASHIER, allow_award_loyalty_points=0)
		customer = self.make_loyal_customer()
		with self.assertRaisesRegex(frappe.PermissionError, "not allowed to award loyalty points"):
			self.award(cashier, customer)
		self.assertEqual(frappe.db.count("Loyalty Point Entry", {"customer": customer}), 0)

	def test_award_is_accepted_with_the_tick(self):
		manager = make_user("pos-rule-manager@example.com", ROLE_MANAGER)
		make_rule(ROLE_MANAGER, allow_award_loyalty_points=1)
		customer = self.make_loyal_customer()
		self.award(manager, customer, points=25)
		self.assertEqual(
			frappe.get_all("Loyalty Point Entry", filters={"customer": customer}, pluck="loyalty_points"), [25]
		)

	def test_the_customer_form_asks_the_server(self):
		from pos_next.api.loyalty_award import can_award

		cashier = make_user("pos-rule-cashier@example.com", ROLE_CASHIER)
		make_rule(ROLE_CASHIER, allow_award_loyalty_points=1)
		frappe.set_user(cashier)
		role_rules.clear_role_rules_cache()
		try:
			self.assertTrue(can_award())
		finally:
			frappe.set_user("Administrator")
