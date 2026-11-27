"""What a till user may do, worked out from POS Role Rule.

get_effective_rules(user, pos_profile) is the one place that decides a user's till
powers: discount limit, item and whole-sale discounts, price change, refund without
a receipt and bonus loyalty points. The till receives the result with its settings
and the server enforces it. No role names or limits are written here; they are set
in POS Role Rule.

How rules combine:
* The user's roles are collected, and every enabled rule for those roles is used. A
  rule for this POS Profile replaces the every-till rule for the same role.
* Several roles: the most generous wins (any tick set, highest percent, "any
  discount" beats a percent).
* No rule matches the user: today's POS Settings values are used, so a site without
  rules behaves exactly as before.
* System Manager (Frappe's administrator role) may do everything once the site uses
  POS Role Rule. On a site without rules it keeps today's POS Settings values, and
  may still award bonus loyalty points as before.
"""

import frappe
from frappe.utils import cint, flt

# Tick boxes of POS Role Rule, combined with "any of the user's rules allows it".
RULE_FLAGS = (
	"allow_item_discount",
	"allow_additional_discount",
	"allow_rate_edit",
	"allow_return_without_receipt",
	"allow_award_loyalty_points",
)
RULE_KEYS = ("allow_any_discount", "max_discount_percent") + RULE_FLAGS

# Frappe's own administrator role, not a client role name.
FULL_ACCESS_ROLE = "System Manager"

SOURCE_RULES = "rules"
SOURCE_FULL_ACCESS = "full_access"
SOURCE_POS_SETTINGS = "pos_settings"

_CACHE_ATTR = "pos_next_role_rules"


def get_effective_rules(user=None, pos_profile=None):
	"""The till powers of `user` (default: the session user) at `pos_profile`.

	Returns a dict with RULE_KEYS plus `source` (rules, full_access or pos_settings).
	`max_discount_percent` only applies when `allow_any_discount` is 0; 0 then means
	no discount at all. Cached for the request.
	"""
	user = user or frappe.session.user
	cache = _cache()
	key = (user, pos_profile or "")
	if key not in cache["users"]:
		cache["users"][key] = _work_out(user, pos_profile, cache)
	return dict(cache["users"][key])


def discount_limit(rules):
	"""Highest discount percent the rules allow, or None when any discount is allowed."""
	if cint(rules.get("allow_any_discount")):
		return None
	return flt(rules.get("max_discount_percent"))


def apply_rules_to_settings(settings, pos_profile, user=None):
	"""Add the user's rules to the till settings dict, in place, and return it.

	The rule keys are always added. When the user's powers come from POS Role Rule
	(not from POS Settings), the matching POS Settings keys the till already reads are
	overlaid too, so existing screens follow the rules. Without rules nothing changes.
	"""
	rules = get_effective_rules(user, pos_profile)
	for key in RULE_KEYS:
		settings[key] = rules[key]
	settings["role_rules_source"] = rules["source"]

	if rules["source"] != SOURCE_POS_SETTINGS:
		any_discount = cint(rules["allow_any_discount"])
		can_discount = any_discount or flt(rules["max_discount_percent"]) > 0
		settings["allow_user_to_edit_item_discount"] = 1 if (rules["allow_item_discount"] and can_discount) else 0
		settings["allow_user_to_edit_additional_discount"] = (
			1 if (rules["allow_additional_discount"] and can_discount) else 0
		)
		settings["allow_user_to_edit_rate"] = rules["allow_rate_edit"]
		settings["allow_return_without_invoice"] = rules["allow_return_without_receipt"]
		# POS Settings uses 0 for "no limit"; a rule's limit of 0 (no discount) is
		# carried by the two discount switches above instead.
		settings["max_discount_allowed"] = 0 if any_discount else flt(rules["max_discount_percent"])
	return settings


def can_award_loyalty_points(user=None):
	"""Whether the user may award bonus loyalty points.

	Points are awarded on the Customer form, not at a till, so an enabled rule for any
	till counts: the user needs a role whose rule ticks "Award Loyalty Points". System
	Manager always may.
	"""
	roles = set(frappe.get_roles(user or frappe.session.user))
	if FULL_ACCESS_ROLE in roles:
		return True
	return any(
		rule.role in roles and cint(rule.allow_award_loyalty_points) for rule in _site_rules(_cache())
	)


def clear_role_rules_cache(doc=None, method=None):
	"""Forget cached rules (called when a POS Role Rule is saved or deleted)."""
	if hasattr(frappe.local, _CACHE_ATTR):
		delattr(frappe.local, _CACHE_ATTR)


# ---------------------------------------------------------------- internals


def _cache():
	cache = getattr(frappe.local, _CACHE_ATTR, None)
	if cache is None:
		cache = {"site_rules": None, "users": {}}
		setattr(frappe.local, _CACHE_ATTR, cache)
	return cache


def _site_rules(cache):
	"""Every enabled POS Role Rule, read once per request."""
	if cache["site_rules"] is None:
		try:
			cache["site_rules"] = frappe.get_all(
				"POS Role Rule",
				filters={"enabled": 1},
				fields=["role", "pos_profile", *RULE_KEYS],
			)
		except Exception as e:
			# Before the first migrate the table does not exist yet: no rules.
			if not frappe.db.is_table_missing(e):
				raise
			cache["site_rules"] = []
	return cache["site_rules"]


def _work_out(user, pos_profile, cache):
	roles = set(frappe.get_roles(user))
	site_rules = _site_rules(cache)

	if site_rules and FULL_ACCESS_ROLE in roles:
		return _full_access()

	matching = _rules_for_roles(roles, pos_profile, site_rules)
	if matching:
		return _combine(matching)

	rules = _from_pos_settings(pos_profile)
	if FULL_ACCESS_ROLE in roles:
		# Before POS Role Rule, System Manager could already award bonus points.
		rules["allow_award_loyalty_points"] = 1
	return rules


def _rules_for_roles(roles, pos_profile, site_rules):
	"""One rule per role: this POS Profile's rule if there is one, else the every-till rule."""
	chosen = {}
	for rule in site_rules:
		if rule.role not in roles:
			continue
		if rule.pos_profile and rule.pos_profile != pos_profile:
			continue
		current = chosen.get(rule.role)
		if current is None or (rule.pos_profile and not current.pos_profile):
			chosen[rule.role] = rule
	return list(chosen.values())


def _combine(rules):
	combined = {
		"allow_any_discount": 1 if any(cint(r.allow_any_discount) for r in rules) else 0,
		"max_discount_percent": max(flt(r.max_discount_percent) for r in rules),
		"source": SOURCE_RULES,
	}
	for flag in RULE_FLAGS:
		combined[flag] = 1 if any(cint(r.get(flag)) for r in rules) else 0
	return combined


def _full_access():
	rules = {flag: 1 for flag in RULE_FLAGS}
	rules.update(allow_any_discount=1, max_discount_percent=100.0, source=SOURCE_FULL_ACCESS)
	return rules


def _from_pos_settings(pos_profile):
	"""Today's powers from POS Settings, for users no rule covers."""
	from pos_next.api.constants import DEFAULT_POS_SETTINGS

	fields = [
		"max_discount_allowed",
		"allow_user_to_edit_item_discount",
		"allow_user_to_edit_additional_discount",
		"allow_user_to_edit_rate",
		"allow_return_without_invoice",
	]
	values = None
	if pos_profile:
		values = frappe.db.get_value(
			"POS Settings", {"pos_profile": pos_profile, "enabled": 1}, fields, as_dict=True
		)
	values = values or frappe._dict({field: DEFAULT_POS_SETTINGS.get(field, 0) for field in fields})

	max_allowed = flt(values.max_discount_allowed)
	return {
		# POS Settings: 0 means no limit.
		"allow_any_discount": 1 if max_allowed <= 0 else 0,
		"max_discount_percent": max_allowed,
		"allow_item_discount": cint(values.allow_user_to_edit_item_discount),
		"allow_additional_discount": cint(values.allow_user_to_edit_additional_discount),
		"allow_rate_edit": cint(values.allow_user_to_edit_rate),
		"allow_return_without_receipt": cint(values.allow_return_without_invoice),
		"allow_award_loyalty_points": 0,
		"source": SOURCE_POS_SETTINGS,
	}
