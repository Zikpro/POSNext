# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

"""Server enforcement of discount limits and price changes (POS Role Rule, item 4).

Two layers:
* The checks themselves (find_problems) on in-memory invoices: limits as a percent, an
  amount and a price change, per item and on the whole sale, offers and coupons, and the
  stricter rules for offline sales.
* Real calls to submit_invoice and update_invoice as test users holding one role each,
  proving the server refuses what the screen would not stop (a direct API call).

Every test is rolled back to a savepoint. Nothing is kept.
"""

from unittest.mock import patch

import frappe
from frappe.utils import nowdate

from pos_next.api import discount_rules, role_rules
from pos_next.api.test_role_rules import (
	ROLE_CASHIER,
	ROLE_MANAGER,
	ROLE_SUPERVISOR,
	RoleRulesTestCase,
	make_rule,
	make_user,
)

TILL = "_Test POS Rule Till A"


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


def row(idx=1, price=100.0, rate=None, **values):
	data = frappe._dict(
		idx=idx,
		item_code=f"ITEM-{idx}",
		item_name=f"Item {idx}",
		qty=1,
		price_list_rate=price,
		rate=price if rate is None else rate,
		discount_percentage=0,
		discount_amount=0,
		is_rate_manually_edited=0,
		is_free_item=0,
	)
	data.update(values)
	return data


def invoice(items=None, **values):
	doc = frappe._dict(
		doctype="Sales Invoice",
		is_pos=1,
		is_return=0,
		pos_profile=TILL,
		items=items or [],
		total=sum(r.rate * r.qty for r in (items or [])),
		discount_amount=0,
		additional_discount_percentage=0,
		apply_discount_on="Grand Total",
		flags=frappe._dict(),
	)
	doc.update(values)
	doc.setdefault("grand_total", doc.total - doc.discount_amount)
	return doc


def no_offers():
	return patch("pos_next.api.invoices.apply_offers", return_value={"items": [], "free_items": []})


class TestItemDiscounts(RoleRulesTestCase):
	def problems(self, doc, **rule_values):
		with no_offers():
			return discount_rules.find_problems(doc, rules(**rule_values))

	def test_discount_at_the_limit_is_accepted(self):
		self.assertEqual(self.problems(invoice([row(rate=90, discount_percentage=10)])), [])

	def test_discount_just_above_the_limit_is_refused_with_item_given_and_limit(self):
		problems = self.problems(invoice([row(rate=89.99, discount_percentage=10.01)]))
		self.assertEqual(len(problems), 1)
		self.assertIn("Discount 10.01%", problems[0])
		self.assertIn("Item 1", problems[0])
		self.assertIn("above your limit of 10%", problems[0])

	def test_rounding_never_lets_a_discount_go_over_the_limit(self):
		# 10% off 4.49 is 4.041: the lowest price allowed is 4.05 (rounded UP), 9.8% off.
		self.assertEqual(self.problems(invoice([row(price=4.49, rate=4.05, discount_percentage=10)])), [])
		# 4.04 would be 10.02%: refused, even though it is the "nearest" penny.
		problems = self.problems(invoice([row(price=4.49, rate=4.04, discount_percentage=10)]))
		self.assertEqual(len(problems), 1)
		self.assertIn("Discount 10.02%", problems[0])
		# 10% off 0.99 is 0.891: 0.90 is allowed, 0.89 (10.1%) is not.
		self.assertEqual(self.problems(invoice([row(price=0.99, rate=0.90, discount_percentage=10)])), [])
		self.assertEqual(len(self.problems(invoice([row(price=0.99, rate=0.89, discount_percentage=10)]))), 1)

	def test_whole_sale_amount_is_rounded_down_to_the_limit(self):
		# 10% of 22.45 is 2.245: at most 2.24 may come off; 2.25 is 10.02%.
		self.assertEqual(self.problems(invoice([row(price=22.45)], discount_amount=2.24, grand_total=20.21)), [])
		self.assertEqual(len(self.problems(invoice([row(price=22.45)], discount_amount=2.25, grand_total=20.20))), 1)
		# 15% of 59.97 is 8.9955: 8.99 may come off, not 9.00 (normal rounding's answer).
		self.assertEqual(
			self.problems(invoice([row(price=59.97)], discount_amount=8.99, grand_total=50.98), max_discount_percent=15), []
		)
		self.assertEqual(
			len(self.problems(invoice([row(price=59.97)], discount_amount=9.00, grand_total=50.97), max_discount_percent=15)), 1
		)

	def test_a_limit_of_0_refuses_any_discount(self):
		self.assertEqual(len(self.problems(invoice([row(rate=99, discount_percentage=1)]), max_discount_percent=0)), 1)

	def test_a_limit_of_100_accepts_a_full_discount(self):
		self.assertEqual(self.problems(invoice([row(rate=0, discount_percentage=100)]), max_discount_percent=100), [])

	def test_any_discount_accepts_a_large_discount(self):
		self.assertEqual(self.problems(invoice([row(rate=5, discount_percentage=95)]), allow_any_discount=1), [])

	def test_discount_given_as_an_amount_is_measured_as_a_percent(self):
		problems = self.problems(invoice([row(price=20, rate=17, discount_amount=3)]))
		self.assertEqual(len(problems), 1)
		self.assertIn("Discount 15.0%", problems[0])

	def test_a_rate_sent_lower_than_the_price_without_discount_fields_is_a_discount(self):
		problems = self.problems(invoice([row(price=10, rate=8)]))
		self.assertEqual(len(problems), 1)
		self.assertIn("Discount 20.0%", problems[0])

	def test_a_lowered_rate_without_discount_fields_needs_the_item_discount_tick(self):
		problems = self.problems(invoice([row(price=10, rate=9.5)]), allow_item_discount=0, max_discount_percent=50)
		self.assertEqual(len(problems), 1)
		self.assertIn("cannot give item discounts", problems[0])

	def test_item_discount_needs_its_tick_box(self):
		problems = self.problems(invoice([row(rate=95, discount_percentage=5)]), allow_item_discount=0)
		self.assertEqual(len(problems), 1)
		self.assertIn("cannot give item discounts", problems[0])

	def test_no_discount_needs_no_permission(self):
		self.assertEqual(
			self.problems(invoice([row()]), allow_item_discount=0, allow_rate_edit=0, max_discount_percent=0), []
		)

	def test_manual_price_change_needs_its_tick_box_even_upwards(self):
		problems = self.problems(
			invoice([row(rate=120, is_rate_manually_edited=1, original_rate=100)]), allow_rate_edit=0
		)
		self.assertEqual(len(problems), 1)
		self.assertIn("not allowed to change the price", problems[0])

	def test_manual_price_reduction_is_held_to_the_limit(self):
		self.assertEqual(self.problems(invoice([row(rate=90, is_rate_manually_edited=1, original_rate=100)])), [])
		problems = self.problems(invoice([row(rate=85, is_rate_manually_edited=1, original_rate=100)]))
		self.assertEqual(len(problems), 1)
		self.assertIn("Discount 15.0%", problems[0])

	def test_manual_price_reduction_does_not_need_the_item_discount_tick(self):
		self.assertEqual(
			self.problems(invoice([row(rate=95, is_rate_manually_edited=1, original_rate=100)]), allow_item_discount=0),
			[],
		)

	def test_discount_as_an_amount_at_the_limit_and_a_penny_over(self):
		self.assertEqual(self.problems(invoice([row(price=20, rate=18, discount_amount=2)])), [])
		self.assertEqual(len(self.problems(invoice([row(price=20, rate=17.99, discount_amount=2.01)]))), 1)

	def test_manual_price_reduction_just_above_the_limit_is_refused(self):
		problems = self.problems(invoice([row(rate=89.99, is_rate_manually_edited=1, original_rate=100)]))
		self.assertEqual(len(problems), 1)
		self.assertIn("Discount 10.01%", problems[0])

	def test_manual_price_reduction_at_limits_0_and_100(self):
		edited = invoice([row(rate=99, is_rate_manually_edited=1, original_rate=100)])
		self.assertEqual(len(self.problems(edited, max_discount_percent=0)), 1)
		cut_to_1 = invoice([row(rate=1, is_rate_manually_edited=1, original_rate=100)])
		self.assertEqual(self.problems(cut_to_1, max_discount_percent=100), [])

	def test_every_offending_item_is_listed(self):
		problems = self.problems(invoice([row(1, rate=80, discount_percentage=20), row(2, rate=70, discount_percentage=30)]))
		self.assertEqual(len(problems), 2)


class TestWholeSaleDiscounts(RoleRulesTestCase):
	def problems(self, doc, **rule_values):
		with no_offers():
			return discount_rules.find_problems(doc, rules(**rule_values))

	def test_percent_at_the_limit_is_accepted(self):
		doc = invoice([row()], additional_discount_percentage=10, discount_amount=10)
		self.assertEqual(self.problems(doc), [])

	def test_percent_above_the_limit_is_refused(self):
		doc = invoice([row()], additional_discount_percentage=12, discount_amount=12)
		problems = self.problems(doc)
		self.assertEqual(len(problems), 1)
		self.assertIn("Discount 12.0% on the whole sale is above your limit of 10%", problems[0])

	def test_amount_is_measured_against_the_total_before_the_discount(self):
		# 15 off a 100 sale (grand total after discount 85): 15%, above 10%.
		doc = invoice([row()], discount_amount=15, grand_total=85)
		self.assertEqual(len(self.problems(doc)), 1)
		doc = invoice([row()], discount_amount=10, grand_total=90)
		self.assertEqual(self.problems(doc), [])

	def test_net_total_discount_uses_the_net_total(self):
		doc = invoice([row()], apply_discount_on="Net Total", discount_amount=11, grand_total=89)
		self.assertEqual(len(self.problems(doc)), 1)

	def test_percent_just_above_the_limit_is_refused(self):
		doc = invoice([row()], additional_discount_percentage=10.01, discount_amount=10.01)
		problems = self.problems(doc)
		self.assertEqual(len(problems), 1)
		self.assertIn("Discount 10.01% on the whole sale", problems[0])

	def test_a_limit_of_0_refuses_any_whole_sale_discount(self):
		doc = invoice([row()], discount_amount=1, grand_total=99)
		self.assertEqual(len(self.problems(doc, max_discount_percent=0)), 1)

	def test_a_limit_of_100_and_any_discount_accept_a_full_whole_sale_discount(self):
		doc = invoice([row()], additional_discount_percentage=100, discount_amount=100, grand_total=0)
		self.assertEqual(self.problems(doc, max_discount_percent=100), [])
		self.assertEqual(self.problems(doc, allow_any_discount=1), [])

	def test_whole_sale_discount_needs_its_tick_box(self):
		doc = invoice([row()], discount_amount=5, grand_total=95)
		problems = self.problems(doc, allow_additional_discount=0)
		self.assertEqual(len(problems), 1)
		self.assertIn("cannot give a discount on the whole sale", problems[0])


class TestSystemDiscounts(RoleRulesTestCase):
	"""Offers and coupons are the system's discounts, not the cashier's."""

	def test_an_offers_discount_is_not_the_cashiers(self):
		offer = {"items": [{"price_list_rate": 100, "discount_percentage": 20}], "free_items": []}
		with patch("pos_next.api.invoices.apply_offers", return_value=offer):
			problems = discount_rules.find_problems(
				invoice([row(rate=80, discount_percentage=20)]), rules(max_discount_percent=0, allow_item_discount=0)
			)
		self.assertEqual(problems, [])

	def test_the_cashiers_limit_applies_on_top_of_the_offer(self):
		offer = {"items": [{"price_list_rate": 100, "discount_percentage": 20}], "free_items": []}
		with patch("pos_next.api.invoices.apply_offers", return_value=offer):
			self.assertEqual(
				discount_rules.find_problems(invoice([row(rate=72, discount_percentage=28)]), rules(max_discount_percent=10)),
				[],
			)
			problems = discount_rules.find_problems(
				invoice([row(rate=65, discount_percentage=35)]), rules(max_discount_percent=10)
			)
		self.assertEqual(len(problems), 1)
		self.assertIn("on top of its offer (20.0%)", problems[0])

	def test_an_offers_free_item_is_not_a_discount(self):
		offer = {"items": [], "free_items": [{"item_code": "ITEM-2", "qty": 1}]}
		with patch("pos_next.api.invoices.apply_offers", return_value=offer):
			problems = discount_rules.find_problems(
				invoice([row(1), row(2, rate=0, is_free_item=1)]), rules(max_discount_percent=0, allow_item_discount=0)
			)
		self.assertEqual(problems, [])

	def test_a_transaction_offer_covers_the_whole_sale_discount(self):
		offer = {"items": [{"price_list_rate": 100}], "free_items": [], "additional_discount_percentage": 10}
		with patch("pos_next.api.invoices.apply_offers", return_value=offer):
			problems = discount_rules.find_problems(
				invoice([row()], discount_amount=10, grand_total=90), rules(allow_additional_discount=0)
			)
		self.assertEqual(problems, [])

	def test_a_coupon_covers_the_whole_sale_discount(self):
		with no_offers(), patch(
			"pos_next.pos_next.doctype.pos_coupon.pos_coupon.check_coupon_code",
			return_value={"valid": True, "coupon": frappe._dict()},
		), patch(
			"pos_next.pos_next.doctype.pos_coupon.pos_coupon.apply_coupon_discount",
			return_value={"valid": True, "discount": 10},
		):
			problems = discount_rules.find_problems(
				invoice([row()], coupon_code="SAVE10", discount_amount=10, grand_total=90),
				rules(allow_additional_discount=0),
			)
		self.assertEqual(problems, [])

	def test_a_whole_sale_discount_beyond_the_coupon_plus_the_limit_is_refused(self):
		with no_offers(), patch(
			"pos_next.pos_next.doctype.pos_coupon.pos_coupon.check_coupon_code",
			return_value={"valid": True, "coupon": frappe._dict()},
		), patch(
			"pos_next.pos_next.doctype.pos_coupon.pos_coupon.apply_coupon_discount",
			return_value={"valid": True, "discount": 10},
		):
			# Coupon 10% + the cashier's own 5% = 15% allowed; 20% is refused.
			within = invoice([row()], coupon_code="SAVE10", discount_amount=15, grand_total=85)
			self.assertEqual(discount_rules.find_problems(within, rules(max_discount_percent=5)), [])
			beyond = invoice([row()], coupon_code="SAVE10", discount_amount=20, grand_total=80)
			problems = discount_rules.find_problems(beyond, rules(max_discount_percent=5))
		self.assertEqual(len(problems), 1)
		self.assertIn("on top of its offer or coupon (10.0%)", problems[0])

	def test_offers_are_only_worked_out_when_needed(self):
		with patch("pos_next.api.invoices.apply_offers") as apply_offers:
			discount_rules.find_problems(invoice([row(rate=95, discount_percentage=5)]), rules())
		apply_offers.assert_not_called()

	def test_offers_failing_gives_them_no_credit(self):
		with patch("pos_next.api.invoices.apply_offers", side_effect=RuntimeError("boom")), patch(
			"pos_next.api.discount_rules.frappe.log_error"
		) as log_error:
			problems = discount_rules.find_problems(
				invoice([row(rate=80, discount_percentage=20)]), rules(max_discount_percent=0)
			)
		self.assertEqual(len(problems), 1)
		log_error.assert_called_once()


class TestWhichRules(RoleRulesTestCase):
	def test_returns_and_desk_invoices_are_not_checked(self):
		with patch("pos_next.api.discount_rules.find_problems") as find_problems:
			discount_rules.enforce_till_rules(invoice([row(rate=1)], is_return=1, return_against="SINV-ORIGINAL"))
			discount_rules.enforce_till_rules(invoice([row(rate=1)], is_pos=0))
			discount_rules.enforce_till_rules(invoice([row(rate=1)], pos_profile=None))
		find_problems.assert_not_called()

	def test_refund_without_receipt_needs_its_tick_box(self):
		cashier = make_user("pos-rule-cashier@example.com", ROLE_CASHIER)
		supervisor = make_user("pos-rule-supervisor@example.com", ROLE_SUPERVISOR)
		make_rule(ROLE_CASHIER, allow_return_without_receipt=0)
		make_rule(ROLE_SUPERVISOR, allow_return_without_receipt=1)
		refund = invoice([row(qty=-1)], is_return=1)
		frappe.set_user(cashier)
		with self.assertRaisesRegex(frappe.ValidationError, "not allowed to refund without a receipt"):
			discount_rules.enforce_till_rules(refund)
		frappe.set_user(supervisor)
		discount_rules.enforce_till_rules(refund)  # allowed: no error
		# A device claiming the supervisor rang it up cannot give the cashier the power.
		frappe.set_user(cashier)
		refund.flags.pos_next_rung_up_by = supervisor
		with self.assertRaisesRegex(frappe.ValidationError, "not allowed to refund without a receipt"):
			discount_rules.enforce_till_rules(refund)

	def test_refund_without_receipt_on_a_site_without_rules_follows_pos_settings(self):
		from pos_next.api.test_role_rules import make_pos_settings

		cashier = make_user("pos-rule-cashier@example.com", ROLE_CASHIER)
		refund = invoice([row(qty=-1)], is_return=1)
		frappe.set_user(cashier)
		with self.assertRaisesRegex(frappe.ValidationError, "not allowed to refund without a receipt"):
			discount_rules.enforce_till_rules(refund)  # no POS Settings: off by default
		frappe.set_user("Administrator")
		make_pos_settings(TILL, allow_return_without_invoice=1)
		role_rules.clear_role_rules_cache()
		frappe.set_user(cashier)
		discount_rules.enforce_till_rules(refund)  # POS Settings allows it: no error

	def test_stricter_takes_the_lower_powers(self):
		a = rules(allow_any_discount=1, allow_rate_edit=1, allow_item_discount=1)
		b = rules(max_discount_percent=5, allow_rate_edit=0, allow_item_discount=1)
		combined = discount_rules.stricter(a, b)
		self.assertEqual(combined["allow_any_discount"], 0)
		self.assertEqual(combined["max_discount_percent"], 5)
		self.assertEqual(combined["allow_rate_edit"], 0)
		self.assertEqual(combined["allow_item_discount"], 1)

	def test_offline_sale_uses_the_stricter_of_rung_up_by_and_syncing_user(self):
		cashier = make_user("pos-rule-cashier@example.com", ROLE_CASHIER)
		manager = make_user("pos-rule-manager@example.com", ROLE_MANAGER)
		make_rule(ROLE_CASHIER, max_discount_percent=5, allow_item_discount=1)
		make_rule(ROLE_MANAGER, allow_any_discount=1, allow_item_discount=1, allow_rate_edit=1)
		doc = invoice([row()])

		# Rung up by the cashier, synced by the manager: the cashier's rules.
		frappe.set_user(manager)
		role_rules.clear_role_rules_cache()
		doc.flags.pos_next_rung_up_by = cashier
		self.assertEqual(discount_rules.rules_for_invoice(doc)["max_discount_percent"], 5)
		self.assertEqual(discount_rules.rules_for_invoice(doc)["allow_rate_edit"], 0)

		# A device claiming the manager rang it up cannot give the cashier more.
		frappe.set_user(cashier)
		doc.flags.pos_next_rung_up_by = manager
		self.assertEqual(discount_rules.rules_for_invoice(doc)["max_discount_percent"], 5)
		self.assertEqual(discount_rules.rules_for_invoice(doc)["allow_any_discount"], 0)

		# An unknown user named by the device is ignored.
		doc.flags.pos_next_rung_up_by = "nobody-here@example.com"
		self.assertEqual(discount_rules.rules_for_invoice(doc)["max_discount_percent"], 5)


class TestDirectAPICalls(RoleRulesTestCase):
	"""Real submit_invoice / update_invoice calls: the server refuses, not the screen."""

	def setUp(self):
		super().setUp()
		# update_invoice and submit_invoice write an Error Log before re-raising a refusal.
		# Error Logs are not removed by the test's rollback, so they are mocked here;
		# addCleanup restores frappe.log_error after every test, even a failing one.
		log_error_patcher = patch("frappe.log_error")
		self.log_error = log_error_patcher.start()
		self.addCleanup(log_error_patcher.stop)
		self.profile = self.pick_pos_profile()
		self.item = self.make_item()
		self.customer = self.make_customer()

	def pick_pos_profile(self):
		for name in frappe.get_all("POS Profile", filters={"disabled": 0}, pluck="name"):
			mop = frappe.db.get_value("POS Payment Method", {"parent": name, "default": 1}, "mode_of_payment")
			if mop:
				profile = frappe.get_doc("POS Profile", name)
				profile.default_mop = mop
				return profile
		self.skipTest("No POS Profile with a default payment method on this site")

	def make_item(self):
		group = frappe.db.get_value("Item Group", {"is_group": 0}, "name")
		item = frappe.get_doc({
			"doctype": "Item", "item_code": "_Test POS Rule Item", "item_name": "_Test POS Rule Item",
			"item_group": group, "stock_uom": "Nos", "is_stock_item": 0, "is_sales_item": 1,
		}).insert(ignore_permissions=True)
		return item.name

	def make_customer(self):
		customer = frappe.get_doc({
			"doctype": "Customer", "customer_name": "_Test POS Rule Customer", "customer_type": "Individual",
			"customer_group": frappe.db.get_value("Customer Group", {"is_group": 0}, "name"),
			"territory": frappe.db.get_value("Territory", {"is_group": 0}, "name"),
		}).insert(ignore_permissions=True)
		return customer.name

	def payload(self, rate, discount_percentage=0, **item_values):
		item = {
			"item_code": self.item, "qty": 1, "uom": "Nos", "stock_uom": "Nos", "conversion_factor": 1,
			"price_list_rate": 100, "rate": rate, "discount_percentage": discount_percentage,
		}
		item.update(item_values)
		return {
			"doctype": "Sales Invoice", "customer": self.customer, "company": self.profile.company,
			"is_pos": 1, "pos_profile": self.profile.name, "posting_date": nowdate(),
			"currency": self.profile.currency, "items": [item],
			"payments": [{"mode_of_payment": self.profile.default_mop, "amount": rate}],
		}

	def submit(self, user, payload):
		from pos_next.api.invoices import submit_invoice

		frappe.set_user(user)
		role_rules.clear_role_rules_cache()
		try:
			return submit_invoice(invoice=payload, data={})
		finally:
			frappe.set_user("Administrator")

	def test_discount_above_the_limit_is_refused_by_submit_invoice(self):
		cashier = make_user("pos-rule-cashier@example.com", ROLE_CASHIER)
		make_rule(ROLE_CASHIER, max_discount_percent=10, allow_item_discount=1)
		before = frappe.db.count("Sales Invoice", {"customer": self.customer})
		with self.assertRaisesRegex(frappe.ValidationError, r"Discount 15.0% on .*_Test POS Rule Item.* is above your limit of 10%"):
			self.submit(cashier, self.payload(85, 15))
		self.assertEqual(frappe.db.count("Sales Invoice", {"customer": self.customer}), before)

	def test_discount_within_the_limit_is_submitted(self):
		cashier = make_user("pos-rule-cashier@example.com", ROLE_CASHIER)
		make_rule(ROLE_CASHIER, max_discount_percent=10, allow_item_discount=1)
		result = self.submit(cashier, self.payload(90, 10))
		self.assertEqual(frappe.db.get_value("Sales Invoice", result["name"], "docstatus"), 1)

	def test_a_rate_sent_lower_with_no_discount_fields_is_refused(self):
		cashier = make_user("pos-rule-cashier@example.com", ROLE_CASHIER)
		make_rule(ROLE_CASHIER, max_discount_percent=10, allow_item_discount=1)
		with self.assertRaisesRegex(frappe.ValidationError, "Discount 40.0%"):
			self.submit(cashier, self.payload(60))

	def test_manual_price_change_needs_the_rule(self):
		cashier = make_user("pos-rule-cashier@example.com", ROLE_CASHIER)
		make_rule(ROLE_CASHIER, max_discount_percent=50, allow_item_discount=1, allow_rate_edit=0)
		with self.assertRaisesRegex(frappe.ValidationError, "not allowed to change the price"):
			self.submit(cashier, self.payload(95, is_rate_manually_edited=1, original_rate=100))

	def test_the_manager_may_give_any_discount(self):
		manager = make_user("pos-rule-manager@example.com", ROLE_MANAGER)
		make_rule(ROLE_MANAGER, allow_any_discount=1, allow_item_discount=1)
		result = self.submit(manager, self.payload(40, 60))
		self.assertEqual(frappe.db.get_value("Sales Invoice", result["name"], "docstatus"), 1)

	def test_a_whole_sale_discount_above_the_limit_is_refused_by_submit_invoice(self):
		cashier = make_user("pos-rule-cashier@example.com", ROLE_CASHIER)
		make_rule(ROLE_CASHIER, max_discount_percent=10, allow_additional_discount=1)
		payload = self.payload(100)
		payload.update(additional_discount_percentage=20, apply_discount_on="Net Total")
		payload["payments"][0]["amount"] = 80
		with self.assertRaisesRegex(frappe.ValidationError, "Discount 20.0% on the whole sale is above your limit of 10%"):
			self.submit(cashier, payload)
		self.assertEqual(frappe.db.count("Sales Invoice", {"customer": self.customer}), 0)

	def test_a_direct_draft_save_is_refused_too(self):
		from pos_next.api.invoices import update_invoice

		supervisor = make_user("pos-rule-supervisor@example.com", ROLE_SUPERVISOR)
		make_rule(ROLE_SUPERVISOR, max_discount_percent=10, allow_item_discount=1)
		frappe.set_user(supervisor)
		role_rules.clear_role_rules_cache()
		try:
			with self.assertRaisesRegex(frappe.ValidationError, "above your limit of 10%"):
				update_invoice(self.payload(80, 20))
		finally:
			frappe.set_user("Administrator")

	def test_a_manual_price_change_is_refused_when_the_draft_is_saved(self):
		# update_invoice's own check (validate_manual_rate_edit) refuses it before the
		# validate hook runs: its message names the price change.
		from pos_next.api.invoices import update_invoice

		cashier = make_user("pos-rule-cashier@example.com", ROLE_CASHIER)
		make_rule(ROLE_CASHIER, max_discount_percent=10, allow_item_discount=1, allow_rate_edit=1)
		frappe.set_user(cashier)
		role_rules.clear_role_rules_cache()
		try:
			with self.assertRaisesRegex(frappe.ValidationError, r"Discount 15.0% on .* \(price change\) is above your limit of 10%"):
				update_invoice(self.payload(85, is_rate_manually_edited=1, original_rate=100))
		finally:
			frappe.set_user("Administrator")

	def test_offline_sale_rung_up_by_the_cashier_is_held_to_the_cashiers_limit(self):
		cashier = make_user("pos-rule-cashier@example.com", ROLE_CASHIER)
		manager = make_user("pos-rule-manager@example.com", ROLE_MANAGER)
		make_rule(ROLE_CASHIER, max_discount_percent=10, allow_item_discount=1)
		make_rule(ROLE_MANAGER, allow_any_discount=1, allow_item_discount=1)
		payload = self.payload(80, 20, )
		payload.update(offline_id="pos_offline_test-rule-1", pos_next_rung_up_by=cashier)
		with self.assertRaisesRegex(frappe.ValidationError, "above your limit of 10%"):
			self.submit(manager, payload)
		self.assertEqual(frappe.db.count("Sales Invoice", {"customer": self.customer}), 0)

	def refund_payload(self, item_code, qty=1, rate=100):
		return {
			"doctype": "Sales Invoice", "customer": self.customer, "company": self.profile.company,
			"is_pos": 1, "is_return": 1, "pos_profile": self.profile.name, "posting_date": nowdate(),
			"currency": self.profile.currency, "update_stock": 1,
			"items": [{
				"item_code": item_code, "qty": -qty, "uom": "Nos", "stock_uom": "Nos", "conversion_factor": 1,
				"price_list_rate": rate, "rate": rate, "warehouse": self.profile.warehouse,
			}],
			"payments": [{"mode_of_payment": self.profile.default_mop, "amount": -qty * rate}],
		}

	def test_refund_without_receipt_is_refused_without_the_rule(self):
		cashier = make_user("pos-rule-cashier@example.com", ROLE_CASHIER)
		make_rule(ROLE_CASHIER, allow_return_without_receipt=0)
		with self.assertRaisesRegex(frappe.ValidationError, "not allowed to refund without a receipt"):
			self.submit(cashier, self.refund_payload(self.item))
		self.assertEqual(frappe.db.count("Sales Invoice", {"customer": self.customer}), 0)

	def test_refund_without_receipt_with_the_rule_puts_stock_back(self):
		if not self.profile.warehouse:
			self.skipTest("The POS Profile has no warehouse")
		supervisor = make_user("pos-rule-supervisor@example.com", ROLE_SUPERVISOR)
		make_rule(ROLE_SUPERVISOR, allow_return_without_receipt=1)
		stock_item = frappe.get_doc({
			"doctype": "Item", "item_code": "_Test POS Rule Stock Item", "item_name": "_Test POS Rule Stock Item",
			"item_group": frappe.db.get_value("Item Group", {"is_group": 0}, "name"), "stock_uom": "Nos",
			"is_stock_item": 1, "is_sales_item": 1,
		}).insert(ignore_permissions=True).name
		# Stock the till sold earlier: ERPNext costs the refund at the item's valuation.
		frappe.get_doc({
			"doctype": "Stock Entry", "stock_entry_type": "Material Receipt", "company": self.profile.company,
			"items": [{"item_code": stock_item, "qty": 5, "basic_rate": 40, "t_warehouse": self.profile.warehouse}],
		}).insert(ignore_permissions=True).submit()

		result = self.submit(supervisor, self.refund_payload(stock_item, qty=2, rate=100))

		invoice = frappe.get_doc("Sales Invoice", result["name"])
		self.assertEqual((invoice.docstatus, invoice.is_return, invoice.return_against), (1, 1, None))
		self.assertEqual(invoice.grand_total, -200)
		self.assertEqual(invoice.loyalty_points or 0, 0)  # no points given or reversed
		stock_in = frappe.get_all(
			"Stock Ledger Entry",
			filters={"voucher_no": invoice.name, "item_code": stock_item, "is_cancelled": 0},
			fields=["warehouse", "actual_qty"],
		)
		self.assertEqual([(s.warehouse, s.actual_qty) for s in stock_in], [(self.profile.warehouse, 2)])

	def test_no_rules_on_the_site_keeps_todays_behaviour(self):
		# Without POS Role Rule, the user's powers come from POS Settings, as before.
		cashier = make_user("pos-rule-cashier@example.com", ROLE_CASHIER)
		limit = frappe.db.get_value("POS Settings", {"pos_profile": self.profile.name, "enabled": 1}, "max_discount_allowed")
		if limit:
			self.skipTest("This site's POS Settings set a discount limit; the test needs 'no limit'")
		result = self.submit(cashier, self.payload(50, 50))
		self.assertEqual(frappe.db.get_value("Sales Invoice", result["name"], "docstatus"), 1)
