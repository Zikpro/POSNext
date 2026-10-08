# Copyright (c) 2025, BrainWise and contributors
# For license information, please see license.txt

"""
Sales Invoice Override
Handles wallet payments that require party information for Receivable accounts.

"""

import frappe
from frappe.utils import cint, flt
from erpnext.accounts.doctype.sales_invoice.sales_invoice import SalesInvoice
from erpnext.accounts.utils import get_account_currency


def _find_paid_bundle_row_for_free(si_doc, free_row):
	"""Pick the paid SI item row whose bundle qty should absorb this free bundle's packed items."""
	candidates = []
	for row in si_doc.get("items"):
		if row.name == free_row.name or cint(row.is_free_item):
			continue
		if row.item_code != free_row.item_code:
			continue
		if (row.warehouse or "") != (free_row.warehouse or ""):
			continue
		candidates.append(row)
	if not candidates:
		return None
	free_idx = free_row.idx or 0
	before = [r for r in candidates if (r.idx or 0) < free_idx]
	if before:
		return max(before, key=lambda r: r.idx or 0)
	return candidates[0]


def _find_matching_packed_item_for_merge(si_doc, paid_row, component_item_code, warehouse):
	"""Match a packed item on the paid bundle line; prefer same warehouse."""
	w = warehouse or ""
	matches = []
	for pi in si_doc.get("packed_items"):
		if pi.parent_detail_docname != paid_row.name:
			continue
		if pi.parent_item != paid_row.item_code:
			continue
		if pi.item_code != component_item_code:
			continue
		matches.append(pi)
	if not matches:
		return None
	for pi in matches:
		if (pi.warehouse or "") == w:
			return pi
	return matches[0]


def _get_post_change_gl_entries_setting():
	"""
	Get post_change_gl_entries setting compatible with ERPNext v15 and v16.

	- ERPNext v15: Field is in 'Accounts Settings'
	- ERPNext v16: Field moved to ERPNext's 'POS Settings' (singleton)

	Since pos_next has its own 'POS Settings' doctype (non-singleton) that overrides
	ERPNext's, we read directly from the Singles table for v16 compatibility.

	Returns:
		int: 1 if post_change_gl_entries is enabled, 0 otherwise (default: 0)
	"""
	# Check if field exists in Accounts Settings schema (v15)
	meta = frappe.get_meta("Accounts Settings")
	if meta.has_field("post_change_gl_entries"):
		value = frappe.db.get_single_value("Accounts Settings", "post_change_gl_entries")
		return cint(value) if value is not None else 0

	# For v16, read directly from Singles table using Query Builder to avoid ORM issues
	# ERPNext's POS Settings is a singleton, data stored in Singles table
	Singles = frappe.qb.DocType("Singles")
	result = (
		frappe.qb.from_(Singles)
		.select(Singles.value)
		.where(Singles.doctype == "POS Settings")
		.where(Singles.field == "post_change_gl_entries")
		.limit(1)
		.run()
	)
	return cint(result[0][0]) if result else 0


class CustomSalesInvoice(SalesInvoice):
	"""
	Custom Sales Invoice class that handles wallet payments correctly.

	When a wallet payment is made using a Receivable account, ERPNext requires
	party information in the GL entry. This override adds party_type and party
	for wallet payment methods marked with is_wallet_payment.
	"""

	# ------------------------------------------------------------------
	# Loyalty handling on returns
	#
	# ERPNext reverses loyalty on a return by deleting the ORIGINAL invoice's
	# Loyalty Point Entries and recreating the earned one. Three measured
	# defects are corrected here, all without touching ERPNext core:
	#
	#   1. make_loyalty_point_entry() subtracts the full return grand_total
	#      from a base that already excludes loyalty_amount, so the loyalty
	#      share is counted twice and a spurious negative earn row appears
	#      (measured: -8 on a GBP12 invoice, -40 on a GBP10 one).
	#   2. The redemption row is deleted in full even on a partial return, so
	#      the customer gets back 100% of the points for a 50% return.
	#   3. delete_loyalty_point_entry() throws when the original's earned
	#      points were later redeemed elsewhere, making such a return
	#      impossible.
	#
	# The returned share is DERIVED as a VALUE RATIO of the two grand totals.
	# A ratio on one consistent basis carries no double-count risk, and unlike a
	# quantity ratio it stays correct for mixed-price baskets and discounts.
	# ------------------------------------------------------------------

	def _pos_next_returned_fraction(self):
		"""Fraction of this invoice returned so far, by VALUE (0.0 - 1.0).

		Value-based so that returning the cheap line of a mixed-price basket
		restores only its own share: a quantity ratio would hand back half the
		loyalty for one of two items regardless of their prices. Both totals
		already include discounts and taxes, so the ratio needs no adjustment.

		Cumulative across every submitted return, which keeps multiple partial
		returns correct and makes the callers idempotent.
		"""
		original_total = abs(flt(self.grand_total))
		if not original_total:
			return 0.0

		returned_total = abs(
			flt(
				frappe.db.sql(
					"""select sum(grand_total) from `tabSales Invoice`
					   where docstatus = 1 and is_return = 1 and return_against = %s""",
					self.name,
				)[0][0]
				or 0
			)
		)
		return min(1.0, returned_total / original_total)

	def make_loyalty_point_entry(self):
		"""Recreate the earned entry using a loyalty-aware returned amount.

		Core computes eligible_amount = (grand_total - loyalty_amount) -
		returned_amount, where returned_amount is the full return grand_total.
		That subtracts the loyalty share twice. The eligible base is simply the
		unreturned share of the non-loyalty value.
		"""
		# Core's version assumes a programme is set, but the return path calls
		# this on the ORIGINAL invoice, which may legitimately have none.
		if not self.loyalty_program:
			return

		fraction = self._pos_next_returned_fraction()
		if not fraction:
			return super().make_loyalty_point_entry()

		from erpnext.accounts.doctype.loyalty_program.loyalty_program import (
			get_loyalty_program_details_with_points,
		)
		from frappe.utils import add_days, getdate

		net_of_loyalty = flt(self.grand_total) - flt(self.loyalty_amount)
		eligible_amount = net_of_loyalty * (1.0 - fraction)

		lp_details = get_loyalty_program_details_with_points(
			self.customer,
			company=self.company,
			current_transaction_amount=net_of_loyalty,
			loyalty_program=self.loyalty_program,
			expiry_date=self.posting_date,
			include_expired_entry=True,
		)
		if not (
			lp_details
			and getdate(lp_details.from_date) <= getdate(self.posting_date)
			and (not lp_details.to_date or getdate(lp_details.to_date) >= getdate(self.posting_date))
		):
			return

		collection_factor = lp_details.collection_factor or 1.0
		points_earned = cint(eligible_amount / collection_factor)

		doc = frappe.get_doc(
			{
				"doctype": "Loyalty Point Entry",
				"company": self.company,
				"loyalty_program": lp_details.loyalty_program,
				"loyalty_program_tier": lp_details.tier_name,
				"customer": self.customer,
				"invoice_type": self.doctype,
				"invoice": self.name,
				"loyalty_points": points_earned,
				"purchase_amount": eligible_amount,
				"expiry_date": add_days(self.posting_date, lp_details.expiry_duration),
				"posting_date": self.posting_date,
			}
		)
		doc.flags.ignore_permissions = 1
		doc.save()
		self.set_loyalty_program_tier()

	def delete_loyalty_point_entry(self):
		"""Allow a return even when the earned points were redeemed elsewhere.

		Core throws in that case. Here the dependent redemption rows are
		re-pointed at another earn entry with spare capacity (or detached) so
		the ledger stays consistent. Their amounts are never altered and no
		unrelated entry is deleted, so the audit trail is preserved.

		Only applied while a return is being processed. Plain invoice
		cancellation keeps core's guard untouched.
		"""
		if not frappe.flags.get("pos_next_loyalty_return"):
			return super().delete_loyalty_point_entry()

		own = frappe.get_all(
			"Loyalty Point Entry", filters={"invoice": self.name}, pluck="name"
		)
		if not own:
			return

		orphans = frappe.get_all(
			"Loyalty Point Entry",
			filters={"redeem_against": ["in", own]},
			fields=["name", "loyalty_points"],
		)
		for orphan in orphans:
			donor = self._pos_next_find_donor_entry(
				exclude=own, needed=abs(flt(orphan.loyalty_points))
			)
			# Re-point only. The redemption amount and its invoice are untouched.
			frappe.db.set_value(
				"Loyalty Point Entry", orphan.name, "redeem_against", donor, update_modified=False
			)

		frappe.db.delete("Loyalty Point Entry", {"invoice": self.name})
		self.set_loyalty_program_tier()

	def _pos_next_find_donor_entry(self, exclude, needed):
		"""An earn entry with unredeemed capacity, or None."""
		candidates = frappe.get_all(
			"Loyalty Point Entry",
			filters={
				"customer": self.customer,
				"loyalty_program": self.loyalty_program,
				"company": self.company,
				"loyalty_points": [">", 0],
				"name": ["not in", exclude or [""]],
			},
			fields=["name", "loyalty_points"],
			order_by="creation asc",
		)
		for cand in candidates:
			used = flt(
				frappe.db.sql(
					"""select sum(abs(loyalty_points)) from `tabLoyalty Point Entry`
					   where redeem_against = %s""",
					cand.name,
				)[0][0]
				or 0
			)
			if flt(cand.loyalty_points) - used >= needed:
				return cand.name
		return None

	def _pos_next_sync_retained_redemption(self):
		"""Restore only the returned share of a redemption.

		delete_loyalty_point_entry() removes the original's redemption row in
		full, which over-restores on a partial return. A single compensating
		row holds back the share that has NOT been returned. It is recomputed
		from scratch on every return submit/cancel, so multiple partial returns
		are cumulative and the operation is idempotent.
		"""
		original = frappe.get_doc("Sales Invoice", self.return_against)
		if not original.loyalty_program:
			return
		if not (cint(original.redeem_loyalty_points) and cint(original.loyalty_points)):
			return

		# The row is keyed to the ORIGINAL invoice, not to a return, so that
		# core's delete_loyalty_point_entry() clears it at the start of every
		# return submit/cancel cycle. That makes this naturally idempotent and
		# keeps it correct when the only return is later cancelled.
		fraction = original._pos_next_returned_fraction()
		retained = int(round(cint(original.loyalty_points) * (1.0 - fraction)))
		if retained <= 0:
			return

		existing = frappe.get_all(
			"Loyalty Point Entry",
			filters={"invoice": original.name, "loyalty_points": ["<", 0]},
			fields=["name", "loyalty_points"],
		)
		for row in existing:
			if cint(row.loyalty_points) == -retained:
				return                      # already correct - nothing to do
			frappe.db.delete("Loyalty Point Entry", {"name": row.name})

		doc = frappe.get_doc(
			{
				"doctype": "Loyalty Point Entry",
				"company": original.company,
				"loyalty_program": original.loyalty_program,
				"customer": original.customer,
				"invoice_type": "Sales Invoice",
				"invoice": original.name,
				"loyalty_points": -retained,
				"purchase_amount": 0,
				"expiry_date": original.posting_date,
				"posting_date": original.posting_date,
			}
		)
		doc.flags.ignore_permissions = 1
		doc.save()

	def _pos_next_is_loyalty_return(self):
		return bool(
			self.is_return and self.return_against and not self.is_consolidated and self.loyalty_program
		)

	def on_submit(self):
		is_loyalty_return = self._pos_next_is_loyalty_return()
		if is_loyalty_return:
			frappe.flags.pos_next_loyalty_return = self.name
		try:
			super().on_submit()
		finally:
			if is_loyalty_return:
				frappe.flags.pos_next_loyalty_return = None
		if is_loyalty_return:
			self._pos_next_sync_retained_redemption()

	def on_cancel(self):
		is_loyalty_return = self._pos_next_is_loyalty_return()
		if is_loyalty_return:
			frappe.flags.pos_next_loyalty_return = self.name
		try:
			super().on_cancel()
		finally:
			if is_loyalty_return:
				frappe.flags.pos_next_loyalty_return = None
		if is_loyalty_return:
			self._pos_next_sync_retained_redemption()

	def make_pos_gl_entries(self, gl_entries):
		"""
		Override to add party information for wallet payment accounts.

		The standard ERPNext implementation doesn't set party_type/party for
		payment mode accounts, which causes validation errors for Receivable
		accounts (like wallet accounts).
		"""
		if cint(self.is_pos):
			skip_change_gl_entries = not _get_post_change_gl_entries_setting()

			for payment_mode in self.payments:
				if skip_change_gl_entries and payment_mode.account == self.account_for_change_amount:
					payment_mode.base_amount -= flt(self.change_amount)

				if payment_mode.amount:
					# POS, make payment entries
					# Credit entry to debit_to (customer receivable)
					gl_entries.append(
						self.get_gl_dict(
							{
								"account": self.debit_to,
								"party_type": "Customer",
								"party": self.customer,
								"against": payment_mode.account,
								"credit": payment_mode.base_amount,
								"credit_in_account_currency": payment_mode.base_amount
								if self.party_account_currency == self.company_currency
								else payment_mode.amount,
								"against_voucher": self.return_against
								if cint(self.is_return) and self.return_against
								else self.name,
								"against_voucher_type": self.doctype,
								"cost_center": self.cost_center,
							},
							self.party_account_currency,
							item=self,
						)
					)

					# Debit entry to payment mode account
					payment_mode_account_currency = get_account_currency(payment_mode.account)

					# Get party info for wallet payments
					party_type, party = self.get_party_and_party_type_for_pos_gl_entry(
						payment_mode.mode_of_payment, payment_mode.account
					)

					gl_entries.append(
						self.get_gl_dict(
							{
								"account": payment_mode.account,
								"party_type": party_type,
								"party": party,
								"against": self.customer,
								"debit": payment_mode.base_amount,
								"debit_in_account_currency": payment_mode.base_amount
								if payment_mode_account_currency == self.company_currency
								else payment_mode.amount,
								"cost_center": self.cost_center,
							},
							payment_mode_account_currency,
							item=self,
						)
					)

			if not skip_change_gl_entries:
				if hasattr(self, "get_gle_for_change_amount"):
					# ERPNext v16+: Method renamed and returns a list of GL entries
					# that needs to be extended to the main gl_entries list
					gl_entries.extend(self.get_gle_for_change_amount())
				else:
					# ERPNext v15: Method takes gl_entries as parameter
					# and appends change amount entries directly to it
					self.make_gle_for_change_amount(gl_entries)

	def validate_pos_paid_amount(self):
		"""
		Allow pure customer-credit POS sales to submit without a payment row.

		POSNext redeems customer credit after submit through Journal Entries /
		Payment Entry allocation, so there is no real Mode of Payment row to send.
		Only bypass the core POS payment-row check when submit_invoice has explicitly
		marked the document for customer-credit redemption.
		"""
		if getattr(self.flags, "pos_next_redeemed_customer_credit", 0):
			if len(self.payments) == 0 and cint(self.is_pos) and flt(self.grand_total) > 0:
				return

		super().validate_pos_paid_amount()

	def get_party_and_party_type_for_pos_gl_entry(self, mode_of_payment, account):
		"""
		Get party type and party for wallet payment GL entries.

		For wallet payments (Mode of Payment with is_wallet_payment=1),
		returns Customer as party_type and the invoice customer as party.
		For regular payments, returns empty strings.
		"""
		is_wallet_mode_of_payment = frappe.db.get_value(
			"Mode of Payment", mode_of_payment, "is_wallet_payment"
		)

		party_type, party = "", ""
		if is_wallet_mode_of_payment:
			party_type, party = "Customer", self.customer

		return party_type, party

	def update_packing_list(self):
		super().update_packing_list()
		self._combine_packed_qty_for_free_product_bundles()
		self._set_use_serial_batch_fields_on_packed_items()

	def _set_use_serial_batch_fields_on_packed_items(self):
		"""
		Force packed_items for batch/serial-tracked Items to use legacy fields path.

		ERPNext's auto-SBB creation during SLE.on_submit fails to link the bundle
		because SBB.voucher_detail_no gets remapped to the parent SI Item row name
		(set_serial_and_batch_values) while validation expects either a matching SLE
		or a Packed Item with that name. Routing through use_serial_batch_fields=1
		bypasses the broken auto-creation for the row.
		"""
		if not self.get("packed_items"):
			return
		for pi in self.get("packed_items"):
			if pi.get("serial_and_batch_bundle"):
				continue
			tracking = frappe.get_cached_value(
				"Item",
				pi.item_code,
				["has_batch_no", "has_serial_no"],
				as_dict=True,
			)
			if not tracking:
				continue
			if tracking.has_batch_no or tracking.has_serial_no:
				pi.use_serial_batch_fields = 1

	def _combine_packed_qty_for_free_product_bundles(self):
		"""
		Merge packed_items from free bundle lines into the matching paid bundle line.

		ERPNext builds packed rows per Sales Invoice Item row. For BOGO / pricing-rule
		free rows, the same product bundle often appears twice (paid + is_free_item).
		That duplicates component rows. Stock and picking should follow total bundle
		qty on one set of packed lines tied to the paid row.
		"""
		if self.is_return or not self.get("packed_items"):
			return

		free_bundle_rows = [
			row
			for row in self.get("items")
			if row.item_code and cint(row.is_free_item) and self.has_product_bundle(row.item_code)
		]
		if not free_bundle_rows:
			return

		for free_row in free_bundle_rows:
			paid_row = _find_paid_bundle_row_for_free(self, free_row)
			if not paid_row:
				continue

			to_remove = []
			for pi in list(self.get("packed_items")):
				if pi.parent_detail_docname != free_row.name or pi.parent_item != free_row.item_code:
					continue
				tgt = _find_matching_packed_item_for_merge(self, paid_row, pi.item_code, pi.warehouse)
				if tgt:
					prec = tgt.precision("qty")
					tgt.qty = flt(flt(tgt.qty) + flt(pi.qty), prec)
					to_remove.append(pi)

			for pi in to_remove:
				self.remove(pi)
