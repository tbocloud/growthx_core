"""Works on a site where ERPNext's setup wizard never ran (a fresh Core site, or CI)."""

import json
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from growthx_core import accounts, signup_codes
from growthx_core.api.v1 import _core, auth
from growthx_core.setup import defaults

EMAIL = "gx-signup-test@example.com"


class TestDefaultsAndSignup(IntegrationTestCase):
	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def test_default_link_always_returns_an_existing_leaf(self):
		for doctype in ("Territory", "Customer Group"):
			value = defaults.default_link(doctype)
			self.assertEqual(frappe.db.get_value(doctype, value, "is_group"), 0, doctype)

	def test_site_where_the_setup_wizard_never_ran(self):
		# Like core-test: no Customer Group or Territory at all (rolled back in tearDown).
		for doctype in ("Customer Group", "Territory"):
			frappe.db.delete(doctype)
		defaults.ensure_all()
		self.assertEqual(frappe.db.get_value("Customer Group", "GrowthX Customers", "is_group"), 0)
		self.assertEqual(frappe.db.get_value("Territory", "India", "parent_territory"), "All Territories")
		self.assertEqual(defaults.default_link("Territory"), "India")  # and no second leaf
		self.assertEqual(frappe.db.count("Territory"), 2)

	def test_erpnext_failure_is_a_500_with_a_reference(self):
		frappe.set_user("Guest")
		frappe.local.form_dict = frappe._dict(
			{
				"accountType": "customer",
				"name": "Signup Test",
				"email": EMAIL,
				"password": "Long-Enough-Pass-91",
				"emailCode": "123456",
				"company": "Signup Test Solar",
			}
		)
		frappe.local.request = None
		frappe.local.request_ip = "127.0.0.1"
		error = frappe.LinkValidationError("Could not find Territory: All Territories")
		with (
			patch.object(_core, "_idempotency_key", return_value=None),
			patch.object(signup_codes, "check"),
			patch.object(accounts, "create_customer_account", side_effect=error),
		):
			response = auth.signup()
		body = json.loads(response.get_data())
		self.assertEqual((response.status_code, body["error"]["code"]), (500, "internal_error"))
		self.assertNotIn("Territory", body["error"]["message"])
		self.assertTrue(frappe.db.exists("Error Log", body["error"]["reference"]))

	def test_ensure_root_is_idempotent(self):
		self.assertEqual(defaults.ensure_root("Territory"), "All Territories")
		self.assertEqual(defaults.ensure_root("Territory"), "All Territories")
		self.assertEqual(frappe.db.count("Territory", {"name": "All Territories"}), 1)

	def test_customer_signup_creates_erpnext_customer_and_account(self):
		frappe.set_user("Administrator")
		user, tenant = accounts.create_customer_account(
			email=EMAIL,
			full_name="Signup Test",
			company="Signup Test Solar",
			password="Long-Enough-Pass-91",
		)
		self.assertIsNone(tenant)  # no product chosen
		account = frappe.db.get_value("GX Account", {"user": user}, ["customer", "status"], as_dict=True)
		self.assertEqual(account.status, "trial")
		customer = frappe.db.get_value(
			"Customer", account.customer, ["customer_group", "territory"], as_dict=True
		)
		self.assertTrue(customer.customer_group and customer.territory)

	def test_approving_a_partner_creates_the_sales_partner(self):
		profile = frappe.get_doc(
			{
				"doctype": "GX Partner Profile",
				"partner_name": "Defaults Tester",
				"user": "Administrator",
				"referral_code": f"DEF-{frappe.generate_hash(length=6).upper()}",
				"status": "active",
			}
		).insert(ignore_permissions=True)
		self.assertTrue(frappe.db.exists("Sales Partner", profile.sales_partner))
