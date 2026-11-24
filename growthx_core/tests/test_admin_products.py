# Copyright (c) 2026, Team Back Office and contributors
# For license information, please see license.txt
"""admin.products: the admin panel's app list, hidden apps (ERPNext Direct) included."""

import json
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from growthx_core.api.v1 import _core, admin, catalogue

HIDDEN = "test-products-hidden"
SHOWN = "test-products-shown"


def _call(fn, user="Administrator", token="test-token"):
	frappe.set_user(user)
	frappe.local.gx_token = token
	frappe.local.form_dict = frappe._dict()
	frappe.local.request = None
	frappe.local.request_ip = "127.0.0.1"
	try:
		with patch.object(frappe.db, "rollback"):  # an error response would roll back this test's data
			response = fn()
	finally:
		frappe.local.gx_token = None
		frappe.set_user("Administrator")
	return response.status_code, json.loads(response.get_data())


class TestAdminProducts(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		for slug, name, availability in ((HIDDEN, "Hidden App", "hidden"), (SHOWN, "Shown App", "available")):
			if not frappe.db.exists("GX Product", slug):
				frappe.get_doc(
					{
						"doctype": "GX Product",
						"slug": slug,
						"product_name": name,
						"availability": availability,
					}
				).insert()

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def _user(self, role):
		email = f"products-{frappe.generate_hash(length=6)}@example.com"
		user = frappe.get_doc(
			{"doctype": "User", "email": email, "first_name": "Products", "send_welcome_email": 0}
		).insert()
		user.add_roles(role)
		return user.name

	def test_admin_sees_hidden_apps_the_catalogue_leaves_out(self):
		admin_user = self._user("GrowthX Admin")
		status, body = _call(admin.products, user=admin_user)
		self.assertEqual(status, 200)
		by_slug = {p["slug"]: p for p in body["items"]}
		self.assertEqual(by_slug[HIDDEN]["availability"], "hidden")
		self.assertEqual(by_slug[HIDDEN]["name"], "Hidden App")
		self.assertIn(SHOWN, by_slug)

		with patch.object(_core, "_check_rate_limit"):
			status, body = _call(catalogue.products, token=None)
		self.assertEqual(status, 200)
		public = {p["slug"] for p in body["items"]}
		self.assertIn(SHOWN, public)
		self.assertNotIn(HIDDEN, public)

	def test_only_admins(self):
		status, body = _call(admin.products, user=self._user("GrowthX Customer"))
		self.assertEqual(status, 403)
		status, body = _call(admin.products, token=None)
		self.assertEqual(status, 401)
