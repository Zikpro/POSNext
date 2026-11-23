"""Manually awarding loyalty points, for goodwill (a complaint, an apology).

ERPNext only creates Loyalty Point Entries from invoices, and no role may create one
by hand. This lets a manager add points with a reason. The points are
ordinary ERPNext points: they count in the customer's balance and are spent at the
till through Redeem Points like earned points. Nothing is posted to the accounts when
points are awarded; ERPNext posts the loyalty expense when they are redeemed.
"""

import frappe
from frappe import _
from frappe.utils import add_days, cint, flt, today

MANAGER_ROLES = ("Nexus POS Manager", "System Manager")
REASON_PREFIX = "Manual award: "
# Loyalty Point Entry.discretionary_reason is a Data field (140 characters).
MAX_REASON_LENGTH = 140 - len(REASON_PREFIX)


def can_award_points(user=None):
	"""Whether the user may award loyalty points."""
	return bool(set(MANAGER_ROLES) & set(frappe.get_roles(user)))


@frappe.whitelist()
def award_loyalty_points(customer, points, reason):
	"""Add loyalty points to a customer's balance.

	Manager only. The customer must be enrolled in a Loyalty Program, the points must
	be a positive whole number, and a reason is required. Points can only be added.
	Returns the customer's new balance.
	"""
	if not can_award_points():
		frappe.throw(_("Only a POS manager can award loyalty points."), frappe.PermissionError)

	if not customer or not frappe.db.exists("Customer", customer):
		frappe.throw(_("Customer {0} not found").format(customer))

	if flt(points) != cint(points) or cint(points) <= 0:
		frappe.throw(_("Enter a whole number of points greater than zero."))
	points = cint(points)

	reason = (reason or "").strip()
	if not reason:
		frappe.throw(_("Enter a reason for awarding the points."))
	if len(reason) > MAX_REASON_LENGTH:
		frappe.throw(_("The reason can be at most {0} characters.").format(MAX_REASON_LENGTH))

	program, tier = frappe.db.get_value("Customer", customer, ["loyalty_program", "loyalty_program_tier"])
	if not program:
		frappe.throw(_("Customer {0} is not enrolled in a Loyalty Program.").format(customer))
	company, expiry_duration = frappe.db.get_value("Loyalty Program", program, ["company", "expiry_duration"])
	if cint(expiry_duration) <= 0:
		frappe.throw(
			_("Set an Expiry Duration on Loyalty Program {0} first, or the points would expire today.").format(program)
		)

	posting_date = today()
	entry = frappe.get_doc({
		"doctype": "Loyalty Point Entry",
		"company": company,
		"loyalty_program": program,
		"loyalty_program_tier": tier,
		"customer": customer,
		# Marked as a sales entry with no invoice: redeemable at the till, and never
		# deleted when an invoice is cancelled or returned.
		"invoice_type": "Sales Invoice",
		"loyalty_points": points,
		"purchase_amount": 0,
		"posting_date": posting_date,
		"expiry_date": add_days(posting_date, cint(expiry_duration)),
		"discretionary_reason": REASON_PREFIX + reason,
	})
	entry.flags.ignore_permissions = True
	entry.insert()

	new_balance = _balance(customer, program, company)
	frappe.get_doc("Customer", customer).add_comment(
		"Comment",
		_("{0} loyalty points awarded by {1}. Reason: {2}. New balance: {3} points.").format(
			points, frappe.session.user, reason, new_balance
		),
	)
	return {"customer": customer, "points": points, "balance": new_balance}


def _balance(customer, program, company):
	from erpnext.accounts.doctype.loyalty_program.loyalty_program import get_loyalty_program_details_with_points

	details = get_loyalty_program_details_with_points(
		customer, loyalty_program=program, expiry_date=today(), company=company
	)
	return cint(details.get("loyalty_points"))
