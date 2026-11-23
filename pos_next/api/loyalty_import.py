"""One-off import of customers' opening loyalty point balances (moving from another system).

ERPNext has no way to create Loyalty Point Entries by hand or by Data Import. This reads
a CSV with the columns `customer` (Customer ID) and `points`, checks every row, and only
then creates one "Opening balance import" entry per customer. The points are ordinary
ERPNext points, spent at the till through Redeem Points. Nothing is posted to the
accounts when they are imported; ERPNext posts the loyalty expense when they are redeemed.

Always run the check first (dry_run). The import is all or nothing and refuses a file
that has any problem. A customer who already has an opening entry is skipped, so running
the same file again imports nothing twice.
"""

import csv
import io

import frappe
from frappe import _
from frappe.utils import add_days, cint, today

IMPORT_ROLES = ("System Manager",)
OPENING_REASON = "Opening balance import"
MAX_LISTED_PROBLEMS = 200


@frappe.whitelist()
def import_opening_points(loyalty_program, file_url, dry_run=1, enroll_missing=0):
	"""Check (dry_run=1) or import (dry_run=0) an opening balance CSV for one Loyalty Program."""
	if not set(IMPORT_ROLES) & set(frappe.get_roles()):
		frappe.throw(_("Only a System Manager can import opening loyalty points."), frappe.PermissionError)
	if not file_url:
		frappe.throw(_("Attach the CSV file first."))
	content = frappe.get_doc("File", {"file_url": file_url}).get_content()
	if isinstance(content, bytes):
		content = content.decode("utf-8-sig")
	return import_rows(loyalty_program, parse_csv(content), cint(dry_run), cint(enroll_missing))


def parse_csv(text):
	"""Rows of the CSV as dicts with lower-case keys. Requires the columns `customer` and `points`."""
	reader = csv.DictReader(io.StringIO((text or "").lstrip("﻿")))
	columns = {(name or "").strip().lower() for name in (reader.fieldnames or [])}
	missing = {"customer", "points"} - columns
	if missing:
		frappe.throw(
			_("The file needs the columns {0}. Found: {1}").format(
				", ".join(sorted(missing)), ", ".join(sorted(c for c in columns if c)) or _("none")
			)
		)
	return [{(k or "").strip().lower(): (v or "").strip() for k, v in row.items()} for row in reader]


def import_rows(loyalty_program, rows, dry_run=True, enroll_missing=False):
	"""Check every row; when not a dry run and nothing is wrong, create the entries.

	Returns a summary: to_import (customers and points), to_enroll, skipped, problems.
	"""
	program = frappe.db.get_value(
		"Loyalty Program", loyalty_program, ["name", "company", "expiry_duration"], as_dict=True
	)
	if not program:
		frappe.throw(_("Loyalty Program {0} not found").format(loyalty_program))
	if cint(program.expiry_duration) <= 0:
		frappe.throw(
			_("Set an Expiry Duration on Loyalty Program {0} first, or the points would expire today.").format(program.name)
		)

	already = set(
		frappe.get_all(
			"Loyalty Point Entry",
			filters={"loyalty_program": program.name, "discretionary_reason": OPENING_REASON},
			pluck="customer",
		)
	)

	problems, plan, skipped, to_enroll, seen = [], [], [], [], {}
	for number, row in enumerate(rows, start=2):  # row 1 is the header
		customer, raw_points = row.get("customer"), row.get("points")
		if not customer and not raw_points:
			continue  # blank line
		if not customer:
			problems.append((number, "", _("Customer is missing")))
			continue
		info = frappe.db.get_value("Customer", customer, ["name", "loyalty_program", "loyalty_program_tier"], as_dict=True)
		if not info:
			problems.append((number, customer, _("Customer not found")))
			continue
		# The Customer ID must match exactly, capital letters included. The database
		# lookup ignores case, so a different spelling is refused here.
		if info.name != customer:
			problems.append(
				(number, customer, _("Customer ID must match exactly, including capital letters: {0}").format(info.name))
			)
			continue
		if customer in seen:
			problems.append((number, customer, _("Also on row {0}").format(seen[customer])))
			continue
		seen[customer] = number

		try:
			# float(), not flt(): flt() turns text such as "abc" into 0 without a word.
			value = float((raw_points or "").replace(",", ""))
		except ValueError:
			value = None
		if value is None or value != cint(value) or value < 0:
			problems.append((number, customer, _("Points must be a whole number of 0 or more (got {0!r})").format(raw_points)))
			continue
		points = cint(value)

		if info.loyalty_program and info.loyalty_program != program.name:
			problems.append((number, customer, _("Enrolled in another Loyalty Program ({0})").format(info.loyalty_program)))
			continue
		if not info.loyalty_program and not enroll_missing:
			problems.append((number, customer, _("Not enrolled in a Loyalty Program (tick 'Enrol customers' to enrol them)")))
			continue

		if customer in already:
			skipped.append((number, customer, _("Already imported")))
			continue
		if points == 0:
			skipped.append((number, customer, _("Zero points")))
			continue
		if not info.loyalty_program:
			to_enroll.append(customer)
		plan.append((customer, points, info.loyalty_program_tier))

	summary = {
		"to_import": len(plan),
		"points": sum(p for _c, p, _t in plan),
		"to_enroll": len(to_enroll),
		"skipped": len(skipped),
		"problems": len(problems),
		"problem_rows": [{"row": n, "customer": c, "problem": m} for n, c, m in problems[:MAX_LISTED_PROBLEMS]],
		"skipped_rows": [{"row": n, "customer": c, "reason": m} for n, c, m in skipped[:MAX_LISTED_PROBLEMS]],
		"imported": False,
	}
	if dry_run:
		return summary
	if problems:
		frappe.throw(_("The file has {0} problem(s). Run Check File and correct them first.").format(len(problems)))

	posting_date = today()
	expiry_date = add_days(posting_date, cint(program.expiry_duration))
	for customer in to_enroll:
		frappe.db.set_value("Customer", customer, "loyalty_program", program.name)
	for customer, points, tier in plan:
		entry = frappe.get_doc({
			"doctype": "Loyalty Point Entry",
			"company": program.company,
			"loyalty_program": program.name,
			"loyalty_program_tier": tier,
			"customer": customer,
			# A sales entry with no invoice: redeemable at the till, and never deleted
			# when an invoice is cancelled or returned.
			"invoice_type": "Sales Invoice",
			"loyalty_points": points,
			"purchase_amount": 0,
			"posting_date": posting_date,
			"expiry_date": expiry_date,
			"discretionary_reason": OPENING_REASON,
		})
		entry.flags.ignore_permissions = True
		entry.insert()
		# Shown in the customer's Comments, like a manual award.
		frappe.get_doc("Customer", customer).add_comment(
			"Comment",
			_("{0} opening loyalty points imported into {1} by {2}.").format(points, program.name, frappe.session.user),
		)

	summary["imported"] = True
	return summary
