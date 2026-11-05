"""catalogue.plans: prices only for signed-in callers, unless growthx_public_prices is set."""

import json
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from growthx_core.api.v1 import catalogue

PRODUCT = "gx-cat-test"


class TestCataloguePlans(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		frappe.local.gx_token = None
		if not frappe.db.exists("GX Product", PRODUCT):
			frappe.get_doc(
				{"doctype": "GX Product", "slug": PRODUCT, "product_name": "Catalogue Test"}
			).insert(ignore_permissions=True)
		frappe.get_doc(
			{
				"doctype": "GX Plan",
				"plan_id": f"{PRODUCT}-lifetime",
				"product": PRODUCT,
				"plan_name": "Lifetime",
				"price": 150000,
				"gst_included": 1,
				"billing_interval": "one_time",
				"is_active": 1,
			}
		).insert(ignore_permissions=True)

	def tearDown(self):
		frappe.local.gx_token = None
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def _plans(self):
		frappe.local.form_dict = frappe._dict({"product": PRODUCT})
		response = catalogue.plans()
		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.headers["Cache-Control"], "no-store")
		items = json.loads(response.get_data())["items"]
		self.assertEqual(len(items), 1)
		return items[0]

	def test_guests_get_the_plan_without_a_price(self):
		frappe.set_user("Guest")
		plan = self._plans()
		self.assertIsNone(plan["price"])
		self.assertEqual((plan["name"], plan["interval"]), ("Lifetime", "one_time"))

	def test_signed_in_callers_get_the_price(self):
		frappe.local.gx_token = "test-token"
		self.assertEqual(self._plans()["price"], {"amount": 15000000, "currency": "INR", "gstIncluded": True})

	def test_flag_makes_prices_public_again(self):
		frappe.set_user("Guest")
		with patch.dict(frappe.conf, {"growthx_public_prices": 1}):
			self.assertEqual(self._plans()["price"]["amount"], 15000000)
