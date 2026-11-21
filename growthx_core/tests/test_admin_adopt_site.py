"""Admin portal Press inventory: admin.adopt_site (an ERPNext site sold directly, imported to bill it)."""

import json
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_months, getdate, nowdate

from growthx_core.api.v1 import _core, admin
from growthx_core.provisioning import press

PRODUCT = "test-import"
PLAN = "test-import-month"
LIFETIME = "test-import-lifetime"
SITE = "direct-import.tbogrowthx.com"
EMAIL = "direct-import@example.com"


def _call(fn, data=None):
	frappe.local.form_dict = frappe._dict(data or {})
	frappe.local.request = None
	frappe.local.request_ip = "127.0.0.1"
	with (
		patch.object(_core, "_idempotency_key", return_value=None),
		patch.object(_core, "_require_auth"),  # the caller is an admin
		patch.object(frappe.db, "rollback"),  # an error response would roll back this test's data
	):
		response = fn()
	return response.status_code, json.loads(response.get_data())


def _press_row(name=SITE, status="active"):
	return {
		"name": name,
		"url": f"https://{name}",
		"status": status,
		"press_status": "Active",
		"bench": "bench-0019-000004-f1",
		"release_group": "bench-0019",
		"release_group_title": "ERPNext",
		"plan": "ERPNext Direct",
		"created_at": "2026-11-21 14:58:00.000000",
	}


class TestAdminAdoptSite(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		frappe.cache.delete_value(admin.PRESS_SITES_CACHE)
		if not frappe.db.exists("GX Product", PRODUCT):
			frappe.get_doc({"doctype": "GX Product", "slug": PRODUCT, "product_name": "Imported"}).insert()
		if not frappe.db.exists("GX Plan", PLAN):
			frappe.get_doc(
				{
					"doctype": "GX Plan",
					"plan_id": PLAN,
					"product": PRODUCT,
					"plan_name": "Imported Monthly",
					"billing_interval": "month",
					"price": 999,
					"gst_included": 0,
					"gst_rate": 18,
					"trial_days": 7,
					"is_active": 1,
				}
			).insert()
		if not frappe.db.exists("GX Plan", LIFETIME):
			frappe.get_doc(
				{
					"doctype": "GX Plan",
					"plan_id": LIFETIME,
					"product": PRODUCT,
					"plan_name": "Imported Lifetime",
					"billing_interval": "one_time",
					"price": 150000,
					"gst_included": 1,
					"gst_rate": 18,
					"is_active": 1,
				}
			).insert()

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.cache.delete_value(admin.PRESS_SITES_CACHE)
		frappe.db.rollback()

	def _client(self, rows=None):
		client = press.FakePressClient()
		client.press_sites = rows if rows is not None else [_press_row()]
		# Nothing may be created or installed on the imported site.
		client.create_site = _refuse
		client.install_app = _refuse
		return patch.object(press, "get_client", return_value=client)

	def _adopt(self, rows=None, **body):
		data = {"site": SITE, "plan": PLAN, "email": EMAIL, "accountName": "Direct Co", **body}
		# Press's list is cached for 60 s, and each call here has its own list.
		frappe.cache.delete_value(admin.PRESS_SITES_CACHE)
		with self._client(rows):
			return _call(admin.adopt_site, data)

	def _tenant(self):
		return frappe.get_doc("GX Tenant", {"press_site_id": SITE})

	def test_a_direct_site_becomes_an_imported_tenant_with_an_active_subscription(self):
		status, body = self._adopt()
		self.assertEqual(status, 200)
		tenant = self._tenant()
		self.assertEqual(body["id"], tenant.name)
		self.assertEqual(tenant.origin, "imported")
		self.assertEqual(tenant.status, "ready")
		self.assertEqual(tenant.product, PRODUCT)
		self.assertEqual(tenant.site_url, f"https://{SITE}")
		self.assertEqual(tenant.subdomain, "direct-import")
		self.assertEqual(tenant.display_name, "Direct Co")
		self.assertEqual(len(tenant.steps), 0)  # nothing to provision
		self.assertEqual(
			len(tenant.apps), 0
		)  # and nothing installed, even though the product wants the theme

		account = frappe.get_doc("GX Account", tenant.account)
		self.assertEqual(account.contact_email, EMAIL)
		self.assertEqual(account.status, "active")
		self.assertTrue(account.customer)

		sub = frappe.get_doc("GX Subscription", tenant.subscription)
		self.assertEqual(sub.plan, PLAN)
		self.assertEqual(sub.product, PRODUCT)
		self.assertEqual(sub.status, "active")
		self.assertIsNone(sub.trial_ends_at)  # a direct site is billed, not tried
		self.assertEqual(sub.tenant, tenant.name)
		self.assertEqual(getdate(sub.current_period_end), getdate(add_months(nowdate(), 1)))

		self.assertTrue(
			frappe.db.exists("GX Audit Log", {"action": "adopt_site", "reference_name": tenant.name})
		)

	def test_a_lifetime_plan_has_no_period_end(self):
		status, _body = self._adopt(plan=LIFETIME)
		self.assertEqual(status, 200)
		sub = frappe.get_doc("GX Subscription", self._tenant().subscription)
		self.assertIsNone(sub.current_period_end)

	def test_the_email_of_an_existing_customer_reuses_that_account(self):
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": EMAIL,
				"first_name": "Direct",
				"user_type": "Website User",
				"send_welcome_email": 0,
			}
		).insert()
		account = frappe.get_doc(
			{
				"doctype": "GX Account",
				"account_name": "Existing Co",
				"user": user.name,
				"contact_email": EMAIL,
			}
		).insert()
		status, _body = self._adopt()
		self.assertEqual(status, 200)
		self.assertEqual(self._tenant().account, account.name)
		self.assertEqual(frappe.get_all("GX Account", {"contact_email": EMAIL}, pluck="name"), [account.name])

	def test_a_site_growthx_already_manages_is_refused(self):
		self._adopt()
		status, body = self._adopt()
		self.assertEqual(status, 409)
		self.assertEqual(body["error"]["code"], "already_managed")
		self.assertIn("GrowthX already manages this site", body["error"]["message"])

	def test_a_customer_who_already_has_this_product_is_refused(self):
		self._adopt()
		site = "second-import.tbogrowthx.com"
		status, body = self._adopt([_press_row(site)], site=site)
		self.assertEqual(status, 409)
		self.assertEqual(body["error"]["code"], "already_added")

	def test_a_site_press_does_not_list_is_a_validation_error(self):
		status, body = self._adopt([_press_row("somewhere-else.tbogrowthx.com")])
		self.assertEqual(status, 422)
		self.assertEqual(body["error"]["code"], "validation_failed")

	def test_a_site_that_is_not_active_cannot_be_imported(self):
		status, body = self._adopt([_press_row(status="broken")])
		self.assertEqual(status, 409)
		self.assertEqual(body["error"]["code"], "not_importable")

	def test_a_plan_that_is_not_on_sale_is_refused(self):
		frappe.db.set_value("GX Plan", PLAN, "is_active", 0)
		status, _body = self._adopt()
		self.assertEqual(status, 422)
		self.assertFalse(frappe.db.exists("GX Tenant", {"press_site_id": SITE}))

	def test_press_being_unreachable_is_a_502(self):
		with patch.object(press, "get_client", side_effect=press.PressError("Press unreachable")):
			status, body = _call(admin.adopt_site, {"site": SITE, "plan": PLAN, "email": EMAIL})
		self.assertEqual(status, 502)
		self.assertEqual(body["error"]["code"], "press_unavailable")


def _refuse(*args, **kwargs):
	raise AssertionError("an imported site must not be created or changed on Press")
