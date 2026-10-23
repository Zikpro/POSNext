# Copyright (c) 2025, BrainWise and contributors
# For license information, please see license.txt

import json
import os
import unittest
from unittest.mock import patch

import frappe

from pos_next.pos_next.doctype.wallet_transaction import wallet_transaction


def _raise_permission_error(*args, **kwargs):
	raise frappe.PermissionError


def _raise_runtime_error(message, *args, **kwargs):
	raise RuntimeError(str(message))


class TestLoyaltyToWalletSetting(unittest.TestCase):
	"""Native loyalty redemption must not be combined with the points-to-wallet bridge by default."""

	def _field(self, fieldname):
		path = os.path.join(
			os.path.dirname(frappe.get_module_path("pos_next")),
			"pos_next", "doctype", "pos_settings", "pos_settings.json",
		)
		with open(path) as f:
			meta = json.load(f)
		return next(df for df in meta["fields"] if df["fieldname"] == fieldname)

	def test_loyalty_to_wallet_defaults_off(self):
		self.assertEqual(self._field("loyalty_to_wallet").get("default"), "0")

	def test_loyalty_to_wallet_is_editable_when_loyalty_enabled(self):
		self.assertFalse(self._field("loyalty_to_wallet").get("read_only_depends_on"))


class TestWalletCreditApiPermissions(unittest.TestCase):
	"""Whitelisted wallet credit endpoints insert with ignore_permissions, so they must check first."""

	@patch.object(wallet_transaction, "_create_wallet_credit")
	@patch.object(wallet_transaction.frappe, "has_permission", side_effect=_raise_permission_error)
	def test_create_wallet_credit_requires_permission(self, _has_permission, create):
		with self.assertRaises(frappe.PermissionError):
			wallet_transaction.create_wallet_credit("WALLET-1", 500)
		create.assert_not_called()

	@patch.object(wallet_transaction, "_create_wallet_credit")
	@patch.object(wallet_transaction.frappe, "has_permission", return_value=True)
	def test_create_wallet_credit_delegates_when_permitted(self, has_permission, create):
		wallet_transaction.create_wallet_credit("WALLET-1", 500, remarks="r")
		has_permission.assert_called_once_with("Wallet Transaction", "create", throw=True)
		create.assert_called_once()

	@patch.object(wallet_transaction, "_create_wallet_credit")
	@patch.object(wallet_transaction.frappe, "has_permission", side_effect=_raise_permission_error)
	def test_credit_loyalty_points_to_wallet_requires_permission(self, _has_permission, create):
		with self.assertRaises(frappe.PermissionError):
			wallet_transaction.credit_loyalty_points_to_wallet("CUST-1", "Company", 1000)
		create.assert_not_called()


class _FakeRow(frappe._dict):
	pass


class _FakeReturn:
	"""Minimal stand-in for a return Sales Invoice used by _apply_loyalty_share_to_return."""

	def __init__(self, grand_total, payments, write_off_amount=0):
		self.is_return = 1
		self.return_against = "SINV-ORIG"
		self.name = "SINV-RET"
		self.grand_total = grand_total
		self.conversion_rate = 1
		self.company = "Company"
		self.cost_center = "Main"
		self.write_off_amount = write_off_amount
		self.write_off_account = "Write Off"
		self.write_off_cost_center = "Main"
		self.payments = [_FakeRow(mode_of_payment=m, amount=a) for m, a in payments]

	def get(self, key, default=None):
		return getattr(self, key, default)

	def precision(self, fieldname):
		return 2

	def remove(self, row):
		self.payments.remove(row)


ORIGINAL = frappe._dict(
	name="SINV-ORIG", grand_total=31.43, paid_amount=31.43, change_amount=0, loyalty_amount=20,
	loyalty_points=2000, redeem_loyalty_points=1, loyalty_program="Rewards",
	loyalty_redemption_account="Loyalty Expense", loyalty_redemption_cost_center="Main",
)


class TestLoyaltyReturnState(unittest.TestCase):
	"""Points restored per return are read from the return's own record, legacy returns stay proportional."""

	@patch("pos_next.api.invoices.frappe.get_all")
	def test_recorded_and_legacy_returns(self, get_all):
		from pos_next.api.invoices import _loyalty_return_state

		get_all.return_value = [
			frappe._dict(grand_total=-13.47, loyalty_points=-747, loyalty_redemption_account="Loyalty Expense"),
			frappe._dict(grand_total=-4.49, loyalty_points=0, loyalty_redemption_account=None),  # legacy
		]
		state = _loyalty_return_state(ORIGINAL)
		# 747 recorded + round(2000 * 4.49 / 31.43) = 286 legacy
		self.assertEqual(state.restored_points, 747 + 286)
		self.assertEqual(state.restorable_points, 2000 - 1033)
		self.assertAlmostEqual(state.monetary_refundable, round(11.43 - (17.96 - 10.33), 2))

	@patch("pos_next.api.invoices.frappe.get_all", return_value=[])
	def test_non_loyalty_invoice(self, _get_all):
		from pos_next.api.invoices import _loyalty_return_state

		state = _loyalty_return_state(frappe._dict(name="X", grand_total=10, paid_amount=10, change_amount=0))
		self.assertEqual(state.points, 0)
		self.assertEqual(state.monetary_refundable, 10)


def _state(restorable_amount=20, monetary_refundable=11.43):
	return frappe._dict(points=2000, amount=20, conversion=0.01, grand_total=31.43,
	                    restorable_amount=restorable_amount, monetary_refundable=monetary_refundable)


class TestApplyLoyaltyShareToReturn(unittest.TestCase):
	"""The cashier's money decides the loyalty restored; limits are enforced on the server."""

	def _apply(self, doc, state, **kwargs):
		from pos_next.api import invoices

		real_get_value = invoices.frappe.db.get_value

		def get_value(doctype, *args, **kw):
			# Fake only the original invoice lookup; System Settings etc. stay real.
			return ORIGINAL if doctype == "Sales Invoice" else real_get_value(doctype, *args, **kw)

		with patch.object(invoices.frappe.db, "get_value", side_effect=get_value), patch.object(
			invoices, "_loyalty_return_state", return_value=state
		), patch.object(invoices.frappe, "throw", side_effect=_raise_runtime_error):
			return invoices._apply_loyalty_share_to_return(doc, **kwargs)

	def test_default_split_when_refunding_to_wallet(self):
		doc = _FakeReturn(-13.47, [])
		self.assertEqual(self._apply(doc, _state(), add_to_customer_balance=True), 8.57)
		self.assertEqual(doc.loyalty_points, -857)
		self.assertEqual(doc.write_off_amount, -8.57)
		self.assertEqual(doc.loyalty_redemption_account, "Loyalty Expense")
		# Issue 5: the loyalty share reverses the original redemption account
		self.assertEqual(doc.write_off_account, "Loyalty Expense")

	def test_pre_existing_write_off_keeps_its_account(self):
		doc = _FakeReturn(-13.47, [("Cash", -4.90)], write_off_amount=-0.01)
		self._apply(doc, _state())
		self.assertEqual(doc.write_off_amount, -8.58)
		self.assertEqual(doc.write_off_account, "Write Off")

	def test_edited_money_restores_the_gap(self):
		doc = _FakeReturn(-13.47, [("Cash", -6.00), ("Redeem Points", -7.47)])
		self.assertEqual(self._apply(doc, _state()), 7.47)
		self.assertEqual(doc.loyalty_points, -747)
		self.assertEqual([p.mode_of_payment for p in doc.payments], ["Cash"])

	def test_money_above_limit_is_rejected(self):
		doc = _FakeReturn(-31.43, [("Cash", -31.43)])
		with self.assertRaises(RuntimeError):
			self._apply(doc, _state())

	def test_requested_refund_is_validated_not_the_rewritten_row(self):
		# ERPNext rewrites an over-tendered return row (25.00 on 22.45 becomes 2.55);
		# the amount the cashier actually asked for must be what gets validated.
		doc = _FakeReturn(-22.45, [("Cash", -2.55)])
		with self.assertRaises(RuntimeError):
			self._apply(doc, _state(), requested_refund=25.00)

	def test_non_loyalty_return_cannot_refund_more_than_its_value(self):
		doc = _FakeReturn(-22.45, [("Cash", -2.55)])
		with self.assertRaises(RuntimeError):
			self._apply(doc, frappe._dict(points=0), requested_refund=25.00)

	def test_nothing_left_to_restore(self):
		doc = _FakeReturn(-11.43, [("Cash", -11.43)])
		self.assertEqual(self._apply(doc, _state(restorable_amount=0)), 0.0)
		self.assertEqual(doc.loyalty_points, 0)
		self.assertEqual(doc.write_off_amount, 0)


class TestGetOrCreateWalletReturnShape(unittest.TestCase):
	"""Callers subscript the result, so a newly created wallet must look like an existing one."""

	def test_new_wallet_is_returned_as_dict(self):
		from pos_next.api import wallet

		created = frappe._dict(name="CUST-WALLET", customer="CUST", company="Co", account="Wallet - Co", status="Active")
		created.insert = lambda **kw: None
		real_get_value = wallet.frappe.db.get_value

		def get_value(doctype, *args, **kw):
			return None if doctype == "Wallet" else real_get_value(doctype, *args, **kw)

		with patch.object(wallet.frappe.db, "get_value", side_effect=get_value), patch.object(
			wallet.frappe, "get_doc", return_value=created
		):
			result = wallet.get_or_create_wallet(
				"CUST", "Co", pos_settings={"wallet_account": "Wallet - Co", "auto_create_wallet": 1}
			)

		self.assertEqual(result["name"], "CUST-WALLET")
		self.assertEqual(result.name, "CUST-WALLET")
		self.assertNotIn("insert", result)


class TestCustomerCreditExcludesWalletRefunds(unittest.TestCase):
	"""A return refunded to the wallet must not be offered again as customer credit."""

	def test_available_credit_subtracts_wallet_refund(self):
		from pos_next.api import credit_sales

		def get_all(doctype, *args, **kwargs):
			if doctype == "Sales Invoice":
				return [
					frappe._dict(name="RET-WALLET", outstanding_amount=-13.47, is_return=1, posting_date=None, grand_total=-13.47, modified=None),
					frappe._dict(name="RET-CREDIT", outstanding_amount=-6.43, is_return=1, posting_date=None, grand_total=-31.43, modified=None),
				]
			if doctype == "Wallet Transaction":
				return [frappe._dict(reference_name="RET-WALLET", amount=13.47)]
			return []

		with patch.object(credit_sales.frappe, "get_all", side_effect=get_all):
			offered = credit_sales.get_available_credit("CUST", "Company")

		self.assertEqual([(row["credit_origin"], row["available_credit"]) for row in offered], [("RET-CREDIT", 6.43)])


class TestSyncLoyaltyWalletCredit(unittest.TestCase):
	"""The loyalty wallet credit follows the points the invoice still earns; only the difference is posted."""

	def _sync(self, credits, debits, target):
		created = []

		class _Doc(frappe._dict):
			def insert(self, **kw):
				created.append(self)

			def submit(self):
				pass

			def cancel(self):
				created.append(("cancelled", self.name))

		def get_all(doctype, filters=None, **kwargs):
			if doctype == "Wallet Transaction" and filters.get("transaction_type") == "Loyalty Credit":
				return credits
			if doctype == "Wallet Transaction":
				return debits
			return ["RET-1"]

		def get_doc(arg, name=None):
			doc = _Doc(arg) if isinstance(arg, dict) else _Doc(name=name)
			doc.flags = frappe._dict()
			return doc

		original = frappe._dict(name="SINV-1", docstatus=1, customer="C", company="Co", loyalty_program="Rewards")
		with patch.object(wallet_transaction.frappe.db, "get_value", return_value=original), patch.object(
			wallet_transaction.frappe, "get_all", side_effect=get_all
		), patch.object(wallet_transaction, "_loyalty_credit_target", return_value=target), patch.object(
			wallet_transaction.frappe, "get_doc", side_effect=get_doc
		), patch.object(wallet_transaction.frappe, "msgprint"):
			wallet_transaction.sync_loyalty_wallet_credit("SINV-1", reference_invoice="RET-2")
		return created

	def _credit(self, amount, docstatus=1):
		return frappe._dict(name="WT-C", wallet="W", amount=amount, source_account="Loyalty Expense", company="Co", docstatus=docstatus)

	def test_partial_return_posts_only_the_difference(self):
		created = self._sync([self._credit(1.79)], [frappe._dict(name="WT-D1", amount=0.36)], target=0.89)
		self.assertEqual(len(created), 1)
		self.assertEqual((created[0].transaction_type, created[0].amount, created[0].reference_name), ("Debit", 0.54, "RET-2"))

	def test_full_return_without_earlier_reversal_cancels_credit(self):
		self.assertEqual(self._sync([self._credit(0.53)], [], target=0), [("cancelled", "WT-C")])

	def test_full_return_after_partial_debits_does_not_cancel(self):
		created = self._sync([self._credit(1.79)], [frappe._dict(name="WT-D1", amount=0.9)], target=0)
		self.assertEqual((created[0].transaction_type, created[0].amount), ("Debit", 0.89))

	def test_cancelled_return_restores_credit(self):
		created = self._sync([self._credit(0.53, docstatus=2)], [], target=0.53)
		self.assertEqual((created[0].transaction_type, created[0].amount), ("Loyalty Credit", 0.53))

	def test_nothing_to_do_when_in_line(self):
		self.assertEqual(self._sync([self._credit(0.53)], [], target=0.53), [])
