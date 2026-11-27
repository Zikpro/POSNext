# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

"""What staff with a role may do at the till.

One record per role, optionally for one POS Profile (blank = every till). The till
reads the user's combined rules from pos_next.api.role_rules, and the server enforces
them. No role names or limits are written in code: they are all set here.
"""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt


class POSRoleRule(Document):
	def validate(self):
		self.validate_max_discount_percent()
		self.validate_one_enabled_rule()

	def validate_max_discount_percent(self):
		value = flt(self.max_discount_percent)
		if value < 0 or value > 100:
			frappe.throw(
				_("{0} must be between 0 and 100, not {1}.").format(
					frappe.bold(self.meta.get_label("max_discount_percent")), value
				),
				title=_("Invalid Discount Limit"),
			)

	def validate_one_enabled_rule(self):
		"""Only one enabled rule per role and POS Profile (blank POS Profile = every till)."""
		if not self.enabled:
			return
		filters = {
			"role": self.role,
			"enabled": 1,
			"name": ("!=", self.name),
			"pos_profile": self.pos_profile if self.pos_profile else ("is", "not set"),
		}
		existing = frappe.db.get_value("POS Role Rule", filters, "name")
		if existing:
			frappe.throw(
				_("Role {0} already has an enabled rule for {1}: {2}. Edit that rule, or disable it first.").format(
					frappe.bold(self.role),
					frappe.bold(self.pos_profile) if self.pos_profile else _("every till"),
					frappe.get_desk_link("POS Role Rule", existing),
				),
				title=_("Duplicate Rule"),
			)

	def on_update(self):
		from pos_next.api.role_rules import clear_role_rules_cache

		clear_role_rules_cache()

	def on_trash(self):
		from pos_next.api.role_rules import clear_role_rules_cache

		clear_role_rules_cache()
