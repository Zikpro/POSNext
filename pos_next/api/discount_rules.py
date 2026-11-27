"""Server-side enforcement of a till user's discount and price-change powers (POS Role Rule).

enforce_till_rules(doc) runs from the Sales Invoice validate hook for POS invoices
(is_pos = 1 with a POS Profile), so it covers the till's update_invoice and
submit_invoice and any direct API call. Desk invoices without is_pos are left alone.
Returns carry the original sale's discounts and are not checked for them, but a return
without its original sale (a refund without a receipt) needs "Refund Without Receipt".

What is checked
* Each item: the discount actually given, as a percent of the item's original price,
  whether it came from a discount percent, a discount amount, a manual price change or
  a rate simply sent lower than the price. A manual price change needs "Price Change at
  the Till"; any other discount needs "Item Discount".
* The whole sale: the extra discount, as a percent of the total before it; it needs
  "Discount on the Whole Sale".
* Either must stay within the user's limit (POS Role Rule, or POS Settings when no rule
  applies).

Discounts the system gives are not the cashier's: promotional offers and coupons are
recalculated here on the server (pos_next.api.invoices.apply_offers and
pos_coupon.apply_coupon_discount), and only what goes beyond them counts against the
user. They are only recalculated when a discount is above the user's own allowance, so
ordinary sales cost nothing extra.

Offline sales: the till sends who rang the sale up. Because that value comes from the
device, it can only make the rules stricter: the sale is checked against whichever of
that user and the user syncing allows less.
"""

import math

import frappe
from frappe import _
from frappe.utils import cint, flt

from pos_next.api.role_rules import RULE_FLAGS, discount_limit, get_effective_rules

# Limits are compared in money, exactly: the lowest price a limit allows is rounded UP
# to the currency and the highest discount amount DOWN, so no discount goes even a
# fraction over the limit (10% off 4.49 allows 4.05, not 4.04 = 10.02%). The till
# rounds the same way. EPSILON only absorbs float error, never money.
DEFAULT_PRECISION = 2
EPSILON = 1e-9


def _scaled(value, precision):
	return round(flt(value) * 10 ** _digits(precision), 6)


def _digits(precision):
	return cint(precision if precision is not None else DEFAULT_PRECISION)


def ceil_money(value, precision=None):
	"""Round UP to the currency precision (float error removed first)."""
	return math.ceil(_scaled(value, precision)) / 10 ** _digits(precision)


def floor_money(value, precision=None):
	"""Round DOWN to the currency precision (float error removed first)."""
	return math.floor(_scaled(value, precision)) / 10 ** _digits(precision)


def _precision(doc_or_row, fieldname):
	try:
		return doc_or_row.precision(fieldname)
	except Exception:
		return DEFAULT_PRECISION


def enforce_till_rules(doc, method=None):
	"""Refuse a POS sale whose discounts or price changes are beyond the user's powers."""
	if doc.doctype != "Sales Invoice" or not cint(doc.get("is_pos")) or not doc.get("pos_profile"):
		return
	if cint(doc.get("is_return")):
		# A refund without the original sale needs "Refund Without Receipt".
		if not doc.get("return_against") and not cint(rules_for_invoice(doc)["allow_return_without_receipt"]):
			frappe.throw(
				_("You are not allowed to refund without a receipt."), title=_("Refund Not Allowed")
			)
		return

	rules = rules_for_invoice(doc)
	problems = find_problems(doc, rules)
	if problems:
		frappe.throw("<br>".join(problems), title=_("Discount Not Allowed"))


def rules_for_invoice(doc):
	"""The rules the sale is checked against (see the module notes on offline sales)."""
	user = frappe.session.user
	rules = get_effective_rules(user, doc.pos_profile)
	rung_up_by = doc.flags.get("pos_next_rung_up_by")
	if rung_up_by and rung_up_by != user and frappe.db.exists("User", rung_up_by):
		rules = stricter(rules, get_effective_rules(rung_up_by, doc.pos_profile))
	return rules


def stricter(a, b):
	"""The rules that allow less: both must allow a power, the lower limit applies."""
	any_discount = 1 if cint(a["allow_any_discount"]) and cint(b["allow_any_discount"]) else 0
	limits = [flt(r["max_discount_percent"]) for r in (a, b) if not cint(r["allow_any_discount"])]
	combined = {
		"allow_any_discount": any_discount,
		"max_discount_percent": min(limits) if limits else 100.0,
		"source": "stricter",
	}
	for flag in RULE_FLAGS:
		combined[flag] = 1 if cint(a[flag]) and cint(b[flag]) else 0
	return combined


def find_problems(doc, rules):
	"""Messages for every discount or price change the rules do not allow."""
	limit = discount_limit(rules)
	own_limit = 100.0 if limit is None else limit
	system = _SystemDiscounts(doc)
	problems = []

	for row in doc.get("items") or []:
		problem = _check_item(row, rules, own_limit, limit, system)
		if problem:
			problems.append(problem)

	problem = _check_whole_sale(doc, rules, own_limit, limit, system)
	if problem:
		problems.append(problem)
	return problems


# ---------------------------------------------------------------- items


def _check_item(row, rules, own_limit, limit, system):
	manual_edit = cint(row.get("is_rate_manually_edited"))
	original = flt(row.get("original_rate")) if manual_edit else 0
	original = original or flt(row.get("price_list_rate")) or flt(row.get("rate"))
	rate = flt(row.get("rate"))
	item = row.get("item_name") or row.get("item_code")

	if manual_edit and abs(rate - original) > 0.005 and not cint(rules["allow_rate_edit"]):
		return _("You are not allowed to change the price of {0} at the till.").format(frappe.bold(item))

	precision = _precision(row, "rate")
	if original <= 0 or rate >= original - EPSILON:
		return None
	given = round((original - rate) / original * 100, 2)

	def within(allowed_percent):
		lowest_price = ceil_money(original * (1 - min(allowed_percent, 100.0) / 100), precision)
		return rate >= lowest_price - EPSILON

	has_discount_fields = flt(row.get("discount_percentage")) > 0 or flt(row.get("discount_amount")) > 0
	# A discount that is not a flagged price change counts as an item discount, including
	# a rate sent lower than the price with no discount fields at all.
	needs_item_discount = has_discount_fields or not manual_edit
	permitted = (not needs_item_discount or cint(rules["allow_item_discount"])) and (
		not manual_edit or cint(rules["allow_rate_edit"])
	)
	allowance = own_limit if permitted else 0.0
	if within(allowance):
		return None

	from_system = system.item_percent(row, original)
	if from_system and within(from_system + allowance):
		return None

	if not permitted:
		if from_system:
			return _("Discount {0}% on {1} is more than its offer gives ({2}%), and you are not allowed to give item discounts.").format(
				given, frappe.bold(item), round(from_system, 2)
			)
		return _("Discount {0}% on {1} is not allowed: you cannot give item discounts.").format(given, frappe.bold(item))
	if from_system:
		return _("Discount {0}% on {1} is above your limit of {2}% on top of its offer ({3}%).").format(
			given, frappe.bold(item), _fmt(limit), round(from_system, 2)
		)
	return _("Discount {0}% on {1} is above your limit of {2}%.").format(given, frappe.bold(item), _fmt(limit))


# ---------------------------------------------------------------- whole sale


def _check_whole_sale(doc, rules, own_limit, limit, system):
	amount = flt(doc.get("discount_amount"))
	percent = flt(doc.get("additional_discount_percentage"))
	if amount <= 0 and percent <= 0:
		return None

	base = _total_before_discount(doc)
	if base <= 0:
		return None
	if amount <= 0:
		amount = base * percent / 100
	given = round(amount / base * 100, 2)
	precision = _precision(doc, "discount_amount")

	def within(allowed_percent):
		highest_amount = floor_money(base * min(allowed_percent, 100.0) / 100, precision)
		return amount <= highest_amount + EPSILON

	permitted = cint(rules["allow_additional_discount"])
	allowance = own_limit if permitted else 0.0
	if within(allowance):
		return None

	from_system = system.whole_sale_percent(base)
	if from_system and within(from_system + allowance):
		return None

	if not permitted:
		if from_system:
			return _("Discount {0}% on the whole sale is more than its offer or coupon gives ({1}%), and you are not allowed to give a discount on the whole sale.").format(
				given, round(from_system, 2)
			)
		return _("Discount {0}% on the whole sale is not allowed: you cannot give a discount on the whole sale.").format(given)
	if from_system:
		return _("Discount {0}% on the whole sale is above your limit of {1}% on top of its offer or coupon ({2}%).").format(
			given, _fmt(limit), round(from_system, 2)
		)
	return _("Discount {0}% on the whole sale is above your limit of {1}%.").format(given, _fmt(limit))


def _total_before_discount(doc):
	"""The amount the whole-sale discount is taken from (ERPNext's apply_discount_on)."""
	if (doc.get("apply_discount_on") or "Grand Total") == "Net Total":
		return flt(doc.get("total"))
	return flt(doc.get("grand_total")) + flt(doc.get("discount_amount"))


def _fmt(limit):
	return 0 if limit is None else (int(limit) if flt(limit).is_integer() else round(limit, 2))


# ---------------------------------------------------------------- system discounts


class _SystemDiscounts:
	"""What the promotional offers and the coupon give this sale, worked out on the server.

	Calculated at most once per check, and only when asked.
	"""

	def __init__(self, doc):
		self.doc = doc
		self._offers = None

	def item_percent(self, row, original):
		"""Percent of `original` that the offers take off this row (100 for an offer's free item)."""
		offers = self._get_offers()
		if not offers:
			return 0.0
		if cint(row.get("is_free_item")):
			return 100.0 if offers["free_qty"].get(row.get("item_code"), 0) >= flt(row.get("qty")) else 0.0
		result = offers["rows"].get(row.idx)
		if not result:
			return 0.0
		offer_price = flt(result.get("price_list_rate")) * (1 - flt(result.get("discount_percentage")) / 100)
		if original <= 0 or offer_price >= original:
			return 0.0
		return (original - offer_price) / original * 100

	def whole_sale_percent(self, base):
		"""Percent of `base` that transaction offers and the coupon take off the whole sale."""
		offers = self._get_offers() or {}
		amount = flt(offers.get("header_amount"))
		if not amount and flt(offers.get("header_percent")):
			amount = base * flt(offers.get("header_percent")) / 100
		amount += self._coupon_amount()
		return min(100.0, amount / base * 100) if base > 0 else 0.0

	def _get_offers(self):
		if self._offers is None:
			self._offers = self._calculate_offers()
		return self._offers

	def _calculate_offers(self):
		from pos_next.api.invoices import apply_offers

		doc = self.doc
		rows, items = [], []
		for row in doc.get("items") or []:
			if cint(row.get("is_free_item")):
				continue
			original = (flt(row.get("original_rate")) if cint(row.get("is_rate_manually_edited")) else 0) or flt(
				row.get("price_list_rate")
			)
			rows.append(row.idx)
			items.append({
				"item_code": row.item_code,
				"item_name": row.get("item_name"),
				"qty": flt(row.qty),
				"uom": row.get("uom"),
				"stock_uom": row.get("stock_uom"),
				"conversion_factor": flt(row.get("conversion_factor")) or 1,
				"price_list_rate": original,
				"rate": original,
				"warehouse": row.get("warehouse"),
			})
		if not items:
			return {}
		invoice_data = {
			"doctype": doc.doctype,
			"pos_profile": doc.pos_profile,
			"customer": doc.get("customer"),
			"company": doc.get("company"),
			"posting_date": doc.get("posting_date"),
			"currency": doc.get("currency"),
			"selling_price_list": doc.get("selling_price_list"),
			"items": items,
		}
		try:
			result = apply_offers(invoice_data)
		except Exception:
			# Offers could not be worked out: nothing is credited to them, so the
			# sale is judged on the user's own limit (never more generous).
			frappe.clear_last_message()
			frappe.log_error(frappe.get_traceback(), "POS Role Rule: offers check failed")
			return {}

		by_idx = {idx: item for idx, item in zip(rows, result.get("items") or [])}
		free_qty = {}
		for free in result.get("free_items") or []:
			free_qty[free.get("item_code")] = free_qty.get(free.get("item_code"), 0) + flt(free.get("qty"))
		return {
			"rows": by_idx,
			"free_qty": free_qty,
			"header_amount": flt(result.get("discount_amount")),
			"header_percent": flt(result.get("additional_discount_percentage")),
		}

	def _coupon_amount(self):
		code = self.doc.get("coupon_code")
		if not code or not frappe.db.table_exists("POS Coupon"):
			return 0.0
		from pos_next.pos_next.doctype.pos_coupon.pos_coupon import apply_coupon_discount, check_coupon_code

		checked = check_coupon_code(code, customer=self.doc.get("customer"), company=self.doc.get("company"))
		if not checked or not checked.get("valid"):
			return 0.0
		base = _total_before_discount(self.doc)
		result = apply_coupon_discount(checked["coupon"], cart_total=base, net_total=flt(self.doc.get("total")))
		return flt(result.get("discount")) if result.get("valid") else 0.0
