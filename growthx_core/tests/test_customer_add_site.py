"""customer.add_site: an existing customer adds another app (a second site) to their account."""

import json
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from growthx_core.api.v1 import _core, customer

PRODUCT = "test-add-site"
PLAN = "test-add-site-lifetime"
EMAIL = "gx-add-site@example.com"


def _call(fn, data=None):
	frappe.local.form_dict = frappe._dict(data or {})
	frappe.local.request = None
	frappe.local.request_ip = "127.0.0.1"
	with patch.object(_core, "_idempotency_key", return_value=None):  # no request headers here
		response = fn()
	return response.status_code, json.loads(response.get_data())


class TestCustomerAddSite(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.product = frappe.get_doc(
			{"doctype": "GX Product", "slug": PRODUCT, "product_name": "Add Site", "availability": "available"}
		).insert()
		frappe.get_doc(
			{
				"doctype": "GX Plan",
				"plan_id": PLAN,
				"product": PRODUCT,
				"plan_name": "Lifetime",
				"billing_interval": "one_time",
				"price": 150000,
				"trial_days": 7,
				"is_active": 1,
			}
		).insert()
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": EMAIL,
				"first_name": "Add",
				"send_welcome_email": 0,
				"roles": [{"role": "GrowthX Customer"}],
			}
		).insert(ignore_permissions=True)
		self.account = frappe.get_doc(
			{"doctype": "GX Account", "account_name": "Add Site", "user": user.name, "contact_email": EMAIL}
		).insert(ignore_permissions=True)
		frappe.set_user(user.name)

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def _add(self, **data):
		return _call(customer.add_site, {"productSlug": PRODUCT, **data})

	def _own_site(self):
		frappe.get_doc(
			{
				"doctype": "GX Tenant",
				"account": self.account.name,
				"product": PRODUCT,
				"display_name": "Add Site",
				"subdomain": f"add-site-{frappe.generate_hash(length=6)}",
				"status": "ready",
			}
		).insert(ignore_mandatory=True)

	def test_an_existing_customer_adds_another_app(self):
		status, body = self._add(subdomain="add-site-orders")
		self.assertEqual(status, 201)
		self.assertEqual(body["site"]["productSlug"], PRODUCT)
		self.assertEqual(body["site"]["status"], "requested")

		tenant = frappe.get_doc("GX Tenant", body["site"]["id"])
		self.assertEqual(tenant.account, self.account.name)
		self.assertEqual(tenant.subdomain, "add-site-orders")
		self.assertEqual(frappe.db.get_value("GX Subscription", tenant.subscription, "status"), "trialing")

	def test_the_same_app_cannot_be_added_twice(self):
		self._own_site()
		status, body = self._add()
		self.assertEqual(status, 409)
		self.assertEqual(body["error"]["code"], "already_added")

	def test_an_app_that_isnt_on_sale_is_refused(self):
		frappe.db.set_value("GX Product", PRODUCT, "availability", "coming_soon")
		status, body = self._add()
		self.assertEqual(status, 422)
		self.assertIn("productSlug", body["error"]["fields"])

	def test_a_taken_subdomain_is_refused(self):
		frappe.get_doc(
			{
				"doctype": "GX Tenant",
				"product": PRODUCT,
				"subdomain": "add-site-taken",
				"status": "archived",
			}
		).insert(ignore_mandatory=True)
		status, body = self._add(subdomain="add-site-taken")
		self.assertEqual(status, 409)
		self.assertEqual(body["error"]["code"], "subdomain_taken")
