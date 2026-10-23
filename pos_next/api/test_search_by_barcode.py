# Copyright (c) 2025, BrainWise and contributors
# For license information, please see license.txt

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import frappe

from pos_next.api import items


@patch("pos_next.services.barcode.resolve_barcode", return_value=None)
class TestSearchByBarcode(unittest.TestCase):
	@patch("pos_next.api.items.frappe.log_error")
	@patch("pos_next.api.items.frappe.db.get_value", return_value=None)
	def test_unknown_barcode_raises_not_found_without_error_log(self, _get_value, log_error, _resolve):
		with self.assertRaises(items.BarcodeNotFoundError) as ctx:
			items.search_by_barcode("0000NOPE", "Any Profile")

		self.assertIn("0000NOPE", str(ctx.exception))
		log_error.assert_not_called()

	def test_not_found_is_a_validation_error(self, _resolve):
		# Callers that catch ValidationError keep working
		self.assertTrue(issubclass(items.BarcodeNotFoundError, frappe.ValidationError))

	@patch("pos_next.api.items.frappe.log_error")
	@patch("pos_next.api.items.frappe.get_cached_doc")
	@patch("pos_next.api.items.frappe.db.get_value")
	def test_other_failures_keep_their_reason(self, get_value, get_cached_doc, log_error, _resolve):
		get_value.return_value = frappe._dict(parent="ITEM-NOT-FOR-SALE", uom=None)
		profile = SimpleNamespace(warehouse="Stores", selling_price_list="Standard Selling", company="Co")
		item = SimpleNamespace(is_sales_item=0)
		get_cached_doc.side_effect = lambda doctype, name: profile if doctype == "POS Profile" else item

		with self.assertRaises(frappe.ValidationError) as ctx:
			items.search_by_barcode("123", "Any Profile")

		self.assertNotIsInstance(ctx.exception, items.BarcodeNotFoundError)
		self.assertIn("ITEM-NOT-FOR-SALE", str(ctx.exception))
		log_error.assert_called_once()
