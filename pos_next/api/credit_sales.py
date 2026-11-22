# Copyright (c) 2025, BrainWise and contributors
# For license information, please see license.txt

"""
Credit Sales API
Handles credit sale operations including:
- Getting available customer credit
- Credit redemption and allocation
- Journal Entry creation for GL posting
"""

import frappe
from frappe import _
from frappe.utils import flt, nowdate, today, cint, get_datetime


def _wallet_credited_amounts(return_invoices):
	"""Amount of each return already refunded to the customer wallet.

	A return refunded to the wallet keeps its negative outstanding, so without
	this the same refund could be redeemed again as customer credit.
	"""
	if not return_invoices:
		return {}
	rows = frappe.get_all(
		"Wallet Transaction",
		filters={
			"docstatus": 1,
			"transaction_type": "Credit",
			"source_type": "Refund",
			"reference_doctype": "Sales Invoice",
			"reference_name": ["in", list(return_invoices)],
		},
		fields=["reference_name", "amount"],
	)
	credited = {}
	for row in rows:
		credited[row.reference_name] = flt(credited.get(row.reference_name)) + flt(row.amount)
	return credited


def return_credit_available(outstanding_amount, update_outstanding_for_self, wallet_credited=0):
	"""Spendable customer credit held by one return. The one rule every credit reader uses.

	Only a return that holds its own credit (update_outstanding_for_self = 1) counts.
	Its outstanding comes from the ledger, so after the return settled what was still
	owed on the original (submit_invoice), it is already just the excess. A return
	booked against its original (update_outstanding_for_self = 0) only reduced that
	invoice's debt; its own outstanding figure is not credit and is never offered.
	Anything an older version refunded to the wallet for the return is subtracted.
	"""
	if not cint(update_outstanding_for_self):
		return 0.0
	return max(0.0, -flt(outstanding_amount) - flt(wallet_credited))


def original_credit_available(outstanding_amount, wallet_credited=0):
	"""Spendable customer credit held on a sale invoice itself (negative outstanding).

	A return booked against its original (update_outstanding_for_self = 0), such as a
	partial cash refund, leaves the unrefunded part on the ORIGINAL as negative
	outstanding. That is where the ledger holds the credit, so it is counted there,
	once, and never on the return. Anything an older version moved from that
	original's returns to the wallet is subtracted.
	"""
	return max(0.0, -flt(outstanding_amount) - flt(wallet_credited))


def _wallet_credited_by_original(original_invoices):
	"""Wallet refunds of the returns made against each original invoice. {original: amount}."""
	if not original_invoices:
		return {}
	rows = frappe.db.sql(
		"""
		select r.return_against as original, sum(wt.amount) as amount
		from `tabWallet Transaction` wt
		join `tabSales Invoice` r on r.name = wt.reference_name
		where wt.docstatus = 1 and wt.transaction_type = 'Credit' and wt.source_type = 'Refund'
			and wt.reference_doctype = 'Sales Invoice' and r.return_against in %(originals)s
		group by r.return_against
		""",
		{"originals": tuple(original_invoices)},
		as_dict=True,
	)
	return {row.original: flt(row.amount) for row in rows}


def split_return_credit(original_outstanding, credit_amount, precision=2):
	"""How a return credited to the customer is booked. Returns (hold_on_return, apply_to_original).

	`original_outstanding` is the original invoice's current outstanding amount and
	`credit_amount` the money part of the return. What the customer still owes on the
	original is settled first; only the excess becomes credit they can spend.

	* Nothing owed on the original: the whole return is credit, held on the return.
	* The return does not exceed what is owed: it only reduces the debt, booked
	  against the original as before, and no credit is created.
	* The return exceeds what is owed: the credit is held on the return, and the owed
	  part is then applied to the original, so only the excess remains as credit.
	"""
	owed = max(0.0, flt(original_outstanding, precision))
	credit = flt(credit_amount, precision)
	if credit <= 0:
		return False, 0.0
	if owed <= 0:
		return True, 0.0
	if credit <= owed:
		return False, 0.0
	return True, owed


def get_return_credit_settlement_remark(return_name):
	"""Remark of the journal entry applying a return's credit to its original invoice."""
	return f"POS Next return credit applied to original invoice for return {return_name}"


def apply_return_credit_to_original(return_doc, amount):
	"""Settle the original invoice's unpaid amount from the return's credit.

	Journal entry: Dr Debtors against the return / Cr Debtors against the original.
	The return keeps only the excess as customer credit. Idempotent per return.
	"""
	amount = flt(amount, return_doc.precision("outstanding_amount"))
	if amount <= 0:
		return None

	remark = get_return_credit_settlement_remark(return_doc.name)
	posted = frappe.db.get_value("Journal Entry", {"user_remark": remark, "docstatus": 1}, "name")
	if posted:
		return posted

	cost_center = return_doc.get("cost_center") or frappe.get_cached_value(
		"Company", return_doc.company, "cost_center"
	)
	jv_doc = frappe.get_doc({
		"doctype": "Journal Entry",
		"voucher_type": "Journal Entry",
		"posting_date": return_doc.posting_date,
		"company": return_doc.company,
		"user_remark": remark,
		"accounts": [
			{
				"account": return_doc.debit_to,
				"party_type": "Customer",
				"party": return_doc.customer,
				"reference_type": "Sales Invoice",
				"reference_name": return_doc.name,
				"debit_in_account_currency": amount,
				"credit_in_account_currency": 0,
				"cost_center": cost_center,
			},
			{
				"account": return_doc.debit_to,
				"party_type": "Customer",
				"party": return_doc.customer,
				"reference_type": "Sales Invoice",
				"reference_name": return_doc.return_against,
				"debit_in_account_currency": 0,
				"credit_in_account_currency": amount,
				"cost_center": cost_center,
			},
		],
	})
	jv_doc.flags.ignore_permissions = True
	jv_doc.insert()
	jv_doc.submit()
	return jv_doc.name


def cancel_return_credit_settlement(return_name):
	"""Cancel the journal entry that applied a return's credit to its original invoice."""
	for name in frappe.get_all(
		"Journal Entry",
		filters={"docstatus": 1, "user_remark": get_return_credit_settlement_remark(return_name)},
		pluck="name",
	):
		jv = frappe.get_doc("Journal Entry", name)
		jv.flags.ignore_permissions = True
		jv.cancel()


@frappe.whitelist()
def get_customer_balance(customer, company=None):
	"""
	Get customer balance from Sales Invoices.

	Calculates the net balance from:
	- Regular invoices: Only positive outstanding_amount (what customer owes)
	- Return invoices: Only negative outstanding_amount (credit added to customer balance)

	Credit is counted where the ledger holds it, each amount once:
	- A return holding its own credit (update_outstanding_for_self = 1, set by
	  "Add to Customer Credit Balance"): its negative outstanding, which after
	  settling what was still owed on the original is just the excess
	  (return_credit_available).
	- A return booked against its original (update_outstanding_for_self = 0, e.g. a
	  partial cash refund): the credit sits on the ORIGINAL invoice as negative
	  outstanding, so it is counted there and never on the return
	  (original_credit_available). A return that only reduced a debt leaves the
	  original at zero or above, so it adds nothing.
	- Cash refund given in full: nothing is left anywhere, nothing is counted.
	Anything an older version refunded to the customer wallet is subtracted.

	Args:
		customer: Customer ID
		company: Company (optional filter)

	Returns:
		dict: {
			'total_outstanding': float (positive = customer owes),
			'total_credit': float (positive = customer has credit from returns),
			'net_balance': float (positive = customer owes, negative = customer has credit)
		}
	"""
	if not customer:
		frappe.throw(_("Customer is required"))

	try:
		from frappe.query_builder import DocType
		from frappe.query_builder.functions import Sum, Coalesce
		from pypika import Case

		SalesInvoice = DocType("Sales Invoice")

		# Build base filters
		base_filters = (
			(SalesInvoice.customer == customer) &
			(SalesInvoice.docstatus == 1)
		)
		if company:
			base_filters = base_filters & (SalesInvoice.company == company)

		# Query for regular invoices (non-returns)
		# Only count positive outstanding (what customer owes). Negative outstanding
		# on a regular invoice is credit and is read by original_query below.
		regular_query = (
			frappe.qb.from_(SalesInvoice)
			.select(
				Coalesce(
					Sum(
						Case()
						.when(SalesInvoice.outstanding_amount > 0, SalesInvoice.outstanding_amount)
						.else_(0)
					),
					0
				).as_("total_outstanding")
			)
			.where(base_filters & (SalesInvoice.is_return == 0))
		)

		# Query for return invoices that hold customer credit (see return_credit_available)
		return_query = (
			frappe.qb.from_(SalesInvoice)
			.select(SalesInvoice.name, SalesInvoice.outstanding_amount, SalesInvoice.update_outstanding_for_self)
			.where(
				base_filters &
				(SalesInvoice.is_return == 1) &
				(SalesInvoice.update_outstanding_for_self == 1) &
				(SalesInvoice.outstanding_amount < 0)
			)
		)

		# Query for sale invoices holding credit left by returns booked against them
		# (see original_credit_available)
		original_query = (
			frappe.qb.from_(SalesInvoice)
			.select(SalesInvoice.name, SalesInvoice.outstanding_amount)
			.where(
				base_filters &
				(SalesInvoice.is_return == 0) &
				(SalesInvoice.outstanding_amount < 0)
			)
		)

		# Execute queries
		regular_result = regular_query.run(as_dict=True)
		return_result = return_query.run(as_dict=True)
		original_result = original_query.run(as_dict=True)

		# Calculate totals
		total_outstanding = flt(regular_result[0].total_outstanding) if regular_result else 0.0
		# Credit is counted where the ledger holds it, less anything an older version
		# refunded to the customer wallet.
		wallet_credited = _wallet_credited_amounts([row.name for row in return_result])
		wallet_by_original = _wallet_credited_by_original([row.name for row in original_result])
		total_credit = sum(
			return_credit_available(
				row.outstanding_amount, row.update_outstanding_for_self, wallet_credited.get(row.name)
			)
			for row in return_result
		) + sum(
			original_credit_available(row.outstanding_amount, wallet_by_original.get(row.name))
			for row in original_result
		)

		# Net balance: positive = owes, negative = has credit
		net_balance = total_outstanding - total_credit

		return {
			"total_outstanding": total_outstanding,
			"total_credit": total_credit,
			"net_balance": net_balance
		}

	except Exception as e:
		frappe.log_error(
			title="Customer Balance Error",
			message=f"Customer: {customer}, Company: {company}, Error: {str(e)}\n{frappe.get_traceback()}"
		)
		return {
			"total_outstanding": 0.0,
			"total_credit": 0.0,
			"net_balance": 0.0
		}


def check_credit_sale_enabled(pos_profile):
	"""
	Check if credit sale is enabled for the POS Profile.

	Args:
		pos_profile: POS Profile name

	Returns:
		bool: True if credit sale is enabled
	"""
	if not pos_profile:
		return False

	# Get POS Settings for the profile
	pos_settings = frappe.db.get_value(
		"POS Settings",
		{"pos_profile": pos_profile},
		"allow_credit_sale",
		as_dict=False
	)

	return bool(pos_settings)


@frappe.whitelist()
def get_available_credit(customer, company, pos_profile=None):
	"""
	Get list of available credit sources for a customer.
	Includes:
	1. Outstanding invoices with negative outstanding (overpaid/returns)
	2. Unallocated advance payment entries

	Returns fresh data with modified timestamp for optimistic locking.
	The frontend should re-fetch before redemption to ensure data is current.

	Args:
		customer: Customer ID
		company: Company
		pos_profile: POS Profile (optional, for checking if feature is enabled)

	Returns:
		list: Available credit sources with amounts and modified timestamps
	"""
	if not customer:
		frappe.throw(_("Customer is required"))

	if not company:
		frappe.throw(_("Company is required"))

	total_credit = []

	# Credit is offered where the ledger holds it, each amount once (the same rule
	# as get_customer_balance):
	# 1. Returns holding their own credit (update_outstanding_for_self=1).
	# 2. Sale invoices holding credit left by returns booked against them
	#    (update_outstanding_for_self=0, e.g. a partial cash refund). Such a return's
	#    own outstanding figure is never offered; the credit is redeemed from the
	#    original, so the redemption journal points where the credit really is.
	outstanding_invoices = frappe.get_all(
		"Sales Invoice",
		filters={
			"outstanding_amount": ["<", 0],
			"docstatus": 1,
			"customer": customer,
			"company": company,
			"is_return": 1,
			"update_outstanding_for_self": 1,
		},
		fields=[
			"name", "outstanding_amount", "is_return", "update_outstanding_for_self",
			"posting_date", "grand_total", "modified",
		],
		order_by="posting_date desc"
	)

	wallet_credited = _wallet_credited_amounts([row.name for row in outstanding_invoices])
	for row in outstanding_invoices:
		# Outstanding is negative, so make it positive for display. Whatever was
		# refunded to the wallet for this return is not available again here.
		available_credit = return_credit_available(
			row.outstanding_amount, row.update_outstanding_for_self, wallet_credited.get(row.name)
		)

		if available_credit > 0:
			total_credit.append({
				"type": "Invoice",
				"credit_origin": row.name,
				"total_credit": available_credit,
				"available_credit": available_credit,
				"source_type": "Sales Return",
				"posting_date": row.posting_date,
				"reference_amount": row.grand_total,
				"credit_to_redeem": 0,  # User will set this
				"modified": row.modified,  # For optimistic locking
			})

	original_invoices = frappe.get_all(
		"Sales Invoice",
		filters={
			"outstanding_amount": ["<", 0],
			"docstatus": 1,
			"customer": customer,
			"company": company,
			"is_return": 0,
		},
		fields=["name", "outstanding_amount", "posting_date", "grand_total", "modified"],
		order_by="posting_date desc"
	)

	wallet_by_original = _wallet_credited_by_original([row.name for row in original_invoices])
	for row in original_invoices:
		available_credit = original_credit_available(row.outstanding_amount, wallet_by_original.get(row.name))

		if available_credit > 0:
			total_credit.append({
				"type": "Invoice",
				"credit_origin": row.name,
				"total_credit": available_credit,
				"available_credit": available_credit,
				"source_type": "Sales Invoice",
				"posting_date": row.posting_date,
				"reference_amount": row.grand_total,
				"credit_to_redeem": 0,  # User will set this
				"modified": row.modified,  # For optimistic locking
			})

	# Get unallocated advance payments
	advances = frappe.get_all(
		"Payment Entry",
		filters={
			"unallocated_amount": [">", 0],
			"party": customer,
			"company": company,
			"docstatus": 1,
			"payment_type": "Receive",
		},
		fields=["name", "unallocated_amount", "posting_date", "paid_amount", "mode_of_payment", "modified"],
		order_by="posting_date desc"
	)

	for row in advances:
		total_credit.append({
			"type": "Advance",
			"credit_origin": row.name,
			"total_credit": flt(row.unallocated_amount),
			"available_credit": flt(row.unallocated_amount),
			"source_type": "Payment Entry",
			"posting_date": row.posting_date,
			"reference_amount": row.paid_amount,
			"mode_of_payment": row.mode_of_payment,
			"credit_to_redeem": 0,  # User will set this
			"modified": row.modified,  # For optimistic locking
		})

	return total_credit


def assert_credit_fully_allocated(redeemed_customer_credit, customer_credit_dict, precision=2):
	"""Refuse a sale whose credit sources do not add up to the credit applied as payment.

	The till pays with `redeemed_customer_credit` and spreads it over credit sources
	(`credit_to_redeem`). If they fall short, the sale would be saved partly unpaid
	without anyone noticing, so it is refused before anything is saved.
	"""
	import json

	if isinstance(customer_credit_dict, str):
		customer_credit_dict = json.loads(customer_credit_dict or "[]")
	redeemed = flt(redeemed_customer_credit, precision)
	if redeemed <= 0:
		return
	allocated = flt(
		sum(flt(row.get("credit_to_redeem")) for row in (customer_credit_dict or [])), precision
	)
	if abs(allocated - redeemed) > 0.5 / (10 ** precision):
		frappe.throw(
			_("Customer credit of {0} was applied, but only {1} could be allocated to credit sources. Refresh the customer's credit and try again.").format(
				frappe.format_value(redeemed, {"fieldtype": "Currency"}),
				frappe.format_value(allocated, {"fieldtype": "Currency"}),
			),
			title=_("Customer Credit"),
		)


@frappe.whitelist()
def redeem_customer_credit(invoice_name, customer_credit_dict):
	"""
	Redeem customer credit by creating Journal Entries.
	This allocates credit from previous invoices/advances to the new invoice.

	Uses row-level locking (SELECT FOR UPDATE) to prevent race conditions
	when multiple users try to redeem the same credit simultaneously.

	Args:
		invoice_name: Sales Invoice name
		customer_credit_dict: List of credit redemption entries

	Returns:
		list: Created journal entry names
	"""
	import json

	if isinstance(customer_credit_dict, str):
		customer_credit_dict = json.loads(customer_credit_dict)

	if not invoice_name:
		frappe.throw(_("Invoice name is required"))

	if not customer_credit_dict:
		return []

	# Get invoice document
	invoice_doc = frappe.get_doc("Sales Invoice", invoice_name)

	if invoice_doc.docstatus != 1:
		frappe.throw(_("Invoice must be submitted to redeem credit"))

	created_journal_entries = []

	# Process each credit source with locking to prevent race conditions
	for credit_row in customer_credit_dict:
		credit_to_redeem = flt(credit_row.get("credit_to_redeem", 0))

		if credit_to_redeem <= 0:
			continue

		credit_type = credit_row.get("type")
		credit_origin = credit_row.get("credit_origin")

		if credit_type == "Invoice":
			# Validate and lock the credit source before creating JE
			_validate_and_lock_invoice_credit(
				credit_origin,
				credit_to_redeem,
				invoice_doc.customer,
				invoice_doc.company,
			)

			# Create JE to allocate credit from original invoice to new invoice
			je_name = _create_credit_allocation_journal_entry(
				invoice_doc,
				credit_origin,
				credit_to_redeem
			)
			created_journal_entries.append(je_name)

		elif credit_type == "Advance":
			# Validate and lock the advance payment before allocation
			_validate_and_lock_advance_credit(
				credit_origin,
				credit_to_redeem,
				invoice_doc.customer,
				invoice_doc.company,
			)

			# Create Payment Entry to allocate advance payment
			pe_name = _create_payment_entry_from_advance(
				invoice_doc,
				credit_origin,
				credit_to_redeem
			)
			created_journal_entries.append(pe_name)

	return created_journal_entries


def _validate_credit_source_ownership(source_name, source_customer, source_company, customer, company):
	"""Ensure a credit source belongs to the same customer and company as the target invoice."""
	if source_customer != customer:
		frappe.throw(
			_("Credit source {0} does not belong to customer {1}").format(source_name, customer)
		)

	if source_company != company:
		frappe.throw(
			_("Credit source {0} does not belong to company {1}").format(source_name, company)
		)


def _validate_and_lock_invoice_credit(invoice_name, amount_to_redeem, customer, company):
	"""
	Validate and lock invoice credit using SELECT FOR UPDATE.
	This prevents race conditions when multiple users try to use the same credit.

	Args:
		invoice_name: Source invoice name with credit
		amount_to_redeem: Amount being redeemed
		customer: Target invoice customer
		company: Target invoice company

	Raises:
		frappe.ValidationError: If insufficient credit available
	"""
	from frappe.query_builder import DocType

	SalesInvoice = DocType("Sales Invoice")

	# Use SELECT FOR UPDATE to lock the row
	# This blocks other transactions from reading/modifying until we commit
	query = (
		frappe.qb.from_(SalesInvoice)
		.select(
			SalesInvoice.name,
			SalesInvoice.outstanding_amount,
			SalesInvoice.customer,
			SalesInvoice.company,
			SalesInvoice.is_return,
			SalesInvoice.update_outstanding_for_self,
		)
		.where(
			(SalesInvoice.name == invoice_name) &
			(SalesInvoice.docstatus == 1)
		)
		.for_update()
	)

	result = query.run(as_dict=True)

	if not result:
		frappe.throw(_("Credit source invoice {0} not found or not submitted").format(invoice_name))

	_validate_credit_source_ownership(
		invoice_name,
		result[0].customer,
		result[0].company,
		customer,
		company,
	)

	# Credit is spent from where the ledger holds it; the same rules as the balance
	# shown to the cashier. A return booked against its original holds none itself:
	# its credit, if any, is on the original and is redeemed from there.
	source = result[0]
	if cint(source.is_return):
		if not cint(source.update_outstanding_for_self):
			frappe.throw(
				_("{0} does not hold customer credit that can be redeemed").format(invoice_name)
			)
		available_credit = return_credit_available(
			source.outstanding_amount,
			source.update_outstanding_for_self,
			_wallet_credited_amounts([invoice_name]).get(invoice_name),
		)
	else:
		available_credit = original_credit_available(
			source.outstanding_amount,
			_wallet_credited_by_original([invoice_name]).get(invoice_name),
		)

	if available_credit < amount_to_redeem:
		frappe.throw(
			_("Insufficient credit available from {0}. Available: {1}, Requested: {2}").format(
				invoice_name,
				frappe.format_value(available_credit, {"fieldtype": "Currency"}),
				frappe.format_value(amount_to_redeem, {"fieldtype": "Currency"})
			)
		)


def _validate_and_lock_advance_credit(payment_entry_name, amount_to_redeem, customer, company):
	"""
	Validate and lock advance payment using SELECT FOR UPDATE.
	This prevents race conditions when multiple users try to use the same advance.

	Args:
		payment_entry_name: Payment Entry name with unallocated amount
		amount_to_redeem: Amount being allocated
		customer: Target invoice customer
		company: Target invoice company

	Raises:
		frappe.ValidationError: If insufficient unallocated amount
	"""
	from frappe.query_builder import DocType

	PaymentEntry = DocType("Payment Entry")

	# Use SELECT FOR UPDATE to lock the row
	query = (
		frappe.qb.from_(PaymentEntry)
		.select(
			PaymentEntry.name,
			PaymentEntry.unallocated_amount,
			PaymentEntry.party,
			PaymentEntry.company,
			PaymentEntry.party_type,
			PaymentEntry.payment_type,
		)
		.where(
			(PaymentEntry.name == payment_entry_name) &
			(PaymentEntry.docstatus == 1)
		)
		.for_update()
	)

	result = query.run(as_dict=True)

	if not result:
		frappe.throw(_("Payment Entry {0} not found or not submitted").format(payment_entry_name))

	if result[0].party_type != "Customer" or result[0].payment_type != "Receive":
		frappe.throw(
			_("Payment Entry {0} is not a valid customer advance").format(payment_entry_name)
		)

	_validate_credit_source_ownership(
		payment_entry_name,
		result[0].party,
		result[0].company,
		customer,
		company,
	)

	available_amount = flt(result[0].unallocated_amount)

	if available_amount < amount_to_redeem:
		frappe.throw(
			_("Insufficient unallocated amount in {0}. Available: {1}, Requested: {2}").format(
				payment_entry_name,
				frappe.format_value(available_amount, {"fieldtype": "Currency"}),
				frappe.format_value(amount_to_redeem, {"fieldtype": "Currency"})
			)
		)


def _create_credit_allocation_journal_entry(invoice_doc, original_invoice_name, amount):
	"""
	Create Journal Entry to allocate credit from one invoice to another.

	GL Entries Created:
	- Debit: Original Invoice Receivable Account (reduces its outstanding)
	- Credit: New Invoice Receivable Account (reduces its outstanding)

	Args:
		invoice_doc: New Sales Invoice document
		original_invoice_name: Original invoice with credit
		amount: Amount to allocate

	Returns:
		str: Journal Entry name
	"""
	# Get original invoice
	original_invoice = frappe.get_doc("Sales Invoice", original_invoice_name)

	_validate_credit_source_ownership(
		original_invoice.name,
		original_invoice.customer,
		original_invoice.company,
		invoice_doc.customer,
		invoice_doc.company,
	)

	# Get cost center
	cost_center = invoice_doc.get("cost_center") or frappe.get_cached_value(
		"Company", invoice_doc.company, "cost_center"
	)

	# Create Journal Entry
	jv_doc = frappe.get_doc({
		"doctype": "Journal Entry",
		"voucher_type": "Journal Entry",
		"posting_date": today(),
		"company": invoice_doc.company,
		"user_remark": get_credit_redeem_remark(invoice_doc.name),
	})

	# Debit Entry - Original Invoice (reduces outstanding)
	debit_row = jv_doc.append("accounts", {})
	debit_row.update({
		"account": original_invoice.debit_to,
		"party_type": "Customer",
		"party": invoice_doc.customer,
		"reference_type": "Sales Invoice",
		"reference_name": original_invoice.name,
		"debit_in_account_currency": amount,
		"credit_in_account_currency": 0,
		"cost_center": cost_center,
	})

	# Credit Entry - New Invoice (reduces outstanding)
	credit_row = jv_doc.append("accounts", {})
	credit_row.update({
		"account": invoice_doc.debit_to,
		"party_type": "Customer",
		"party": invoice_doc.customer,
		"reference_type": "Sales Invoice",
		"reference_name": invoice_doc.name,
		"debit_in_account_currency": 0,
		"credit_in_account_currency": amount,
		"cost_center": cost_center,
	})

	jv_doc.flags.ignore_permissions = True
	jv_doc.save()
	jv_doc.submit()

	frappe.msgprint(
		_("Journal Entry {0} created for credit redemption").format(jv_doc.name),
		alert=True
	)

	return jv_doc.name


def _create_payment_entry_from_advance(invoice_doc, payment_entry_name, amount):
	"""
	Allocate existing advance Payment Entry to invoice.
	Updates the Payment Entry to add reference to the invoice.

	Args:
		invoice_doc: Sales Invoice document
		payment_entry_name: Payment Entry with unallocated amount
		amount: Amount to allocate

	Returns:
		str: Payment Entry name
	"""
	# Get payment entry
	payment_entry = frappe.get_doc("Payment Entry", payment_entry_name)

	if payment_entry.party_type != "Customer" or payment_entry.payment_type != "Receive":
		frappe.throw(
			_("Payment Entry {0} is not a valid customer advance").format(payment_entry_name)
		)

	_validate_credit_source_ownership(
		payment_entry.name,
		payment_entry.party,
		payment_entry.company,
		invoice_doc.customer,
		invoice_doc.company,
	)

	# Check if already allocated
	if payment_entry.unallocated_amount < amount:
		frappe.throw(
			_("Payment Entry {0} has insufficient unallocated amount").format(
				payment_entry_name
			)
		)

	# Add reference to invoice
	payment_entry.append("references", {
		"reference_doctype": "Sales Invoice",
		"reference_name": invoice_doc.name,
		"total_amount": invoice_doc.grand_total,
		"outstanding_amount": invoice_doc.outstanding_amount,
		"allocated_amount": amount,
	})

	# Recalculate unallocated amount
	payment_entry.set_amounts()

	payment_entry.flags.ignore_permissions = True
	payment_entry.flags.ignore_validate_update_after_submit = True
	payment_entry.save()

	frappe.msgprint(
		_("Payment Entry {0} allocated to invoice").format(payment_entry.name),
		alert=True
	)

	return payment_entry.name


def get_credit_redeem_remark(invoice_name):
	"""Get remark for credit redemption journal entry."""
	return f"POS Next credit redemption for invoice {invoice_name}"


def assert_credit_not_spent(invoice_name):
	"""Refuse to cancel an invoice whose customer credit has been spent on another sale.

	A credit redemption journal debits Debtors against the invoice that held the credit
	(a return, or an original holding credit) and credits the sale it paid. With
	"Unlink Payment on Cancellation of Invoice" on, ERPNext would silently unlink that
	journal on cancel, leaving the paying sale marked paid while the customer owes the
	amount in the ledger. Runs from before_cancel, before ERPNext unlinks anything.
	"""
	prefix = get_credit_redeem_remark("")
	rows = frappe.db.sql(
		"""
		select distinct je.name, je.user_remark
		from `tabJournal Entry` je
		join `tabJournal Entry Account` acc on acc.parent = je.name
		where je.docstatus = 1
			and acc.reference_type = 'Sales Invoice' and acc.reference_name = %s
			and acc.debit_in_account_currency > 0
			and je.user_remark like %s
		""",
		(invoice_name, prefix + "%"),
		as_dict=True,
	)
	if not rows:
		return
	sales = ", ".join(sorted({row.user_remark[len(prefix):] for row in rows}))
	frappe.throw(
		_("The customer credit of {0} has been used to pay {1}. Cancel {1} first, then cancel {0}.").format(
			invoice_name, sales
		),
		title=_("Customer Credit Used"),
	)


@frappe.whitelist()
def cancel_credit_journal_entries(invoice_name):
	"""
	Cancel journal entries created for credit redemption when invoice is cancelled.

	Args:
		invoice_name: Sales Invoice name
	"""
	remark = get_credit_redeem_remark(invoice_name)

	# Find linked journal entries
	linked_journal_entries = frappe.get_all(
		"Journal Entry",
		filters={
			"docstatus": 1,
			"user_remark": remark
		},
		pluck="name"
	)

	cancelled_count = 0
	for journal_entry_name in linked_journal_entries:
		try:
			je_doc = frappe.get_doc("Journal Entry", journal_entry_name)

			# Verify it references this invoice
			has_reference = any(
				d.reference_type == "Sales Invoice" and d.reference_name == invoice_name
				for d in je_doc.accounts
			)

			if not has_reference:
				continue

			je_doc.flags.ignore_permissions = True
			je_doc.cancel()
			cancelled_count += 1
		except Exception as e:
			frappe.log_error(
				f"Failed to cancel Journal Entry {journal_entry_name}: {str(e)}",
				"Credit Sale JE Cancellation"
			)

	if cancelled_count > 0:
		frappe.msgprint(
			_("Cancelled {0} credit redemption journal entries").format(cancelled_count),
			alert=True
		)

	return cancelled_count


@frappe.whitelist()
def get_credit_sale_summary(pos_profile):
	"""
	Get summary of credit sales for a POS Profile.

	Args:
		pos_profile: POS Profile name

	Returns:
		dict: Summary statistics
	"""
	if not pos_profile:
		frappe.throw(_("POS Profile is required"))

	# Get credit sales (outstanding > 0)
	summary = frappe.db.sql("""
		SELECT
			COUNT(*) as count,
			SUM(outstanding_amount) as total_outstanding,
			SUM(grand_total) as total_amount,
			SUM(paid_amount) as total_paid
		FROM
			`tabSales Invoice`
		WHERE
			pos_profile = %(pos_profile)s
			AND docstatus = 1
			AND is_pos = 1
			AND outstanding_amount > 0
			AND is_return = 0
	""", {"pos_profile": pos_profile}, as_dict=True)

	return summary[0] if summary else {
		"count": 0,
		"total_outstanding": 0,
		"total_amount": 0,
		"total_paid": 0
	}


@frappe.whitelist()
def get_credit_invoices(pos_profile, limit=100):
	"""
	Get list of credit sale invoices (with outstanding amount).

	Args:
		pos_profile: POS Profile name
		limit: Maximum number of invoices to return

	Returns:
		list: Credit sale invoices
	"""
	if not pos_profile:
		frappe.throw(_("POS Profile is required"))

	# Check if user has access to this POS Profile
	has_access = frappe.db.exists(
		"POS Profile User",
		{"parent": pos_profile, "user": frappe.session.user}
	)

	if not has_access and not frappe.has_permission("Sales Invoice", "read"):
		frappe.throw(_("You don't have access to this POS Profile"))

	# Query for credit invoices
	invoices = frappe.db.sql("""
		SELECT
			name,
			customer,
			customer_name,
			posting_date,
			posting_time,
			grand_total,
			paid_amount,
			outstanding_amount,
			status,
			docstatus
		FROM
			`tabSales Invoice`
		WHERE
			pos_profile = %(pos_profile)s
			AND docstatus = 1
			AND is_pos = 1
			AND outstanding_amount > 0
			AND is_return = 0
		ORDER BY
			posting_date DESC,
			posting_time DESC
		LIMIT %(limit)s
	""", {
		"pos_profile": pos_profile,
		"limit": limit
	}, as_dict=True)

	return invoices
