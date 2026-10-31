"""'Sign in with GrowthX' on a site: auth.site_info, auth.site_login, and auth.google signInOnly."""

import json
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from growthx_core import accounts, google_signin
from growthx_core.api.v1 import _core, auth
from growthx_core.provisioning import site_login

PRODUCT = "test-site-signin"
OWNER = "gx-site-signin@example.com"
OTHER = "gx-site-signin-other@example.com"
PASSWORD = "Long-Enough-Pass-91"


class TestSiteSignin(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		frappe.get_doc(
			{"doctype": "GX Product", "slug": PRODUCT, "product_name": "Test", "availability": "available"}
		).insert()
		self.owner, _ = accounts.create_customer_account(
			email=OWNER, full_name="Abc Owner", company="Abc Solar", password=PASSWORD
		)
		self.other, _ = accounts.create_customer_account(
			email=OTHER, full_name="Someone Else", company="Other Co", password=PASSWORD
		)
		self.site = frappe.get_doc(
			{
				"doctype": "GX Tenant",
				"account": frappe.db.get_value("GX Account", {"user": self.owner}),
				"product": PRODUCT,
				"display_name": "Abc Solar",
				"subdomain": "abc-signin",
				"site_url": "https://abc-signin.tbogrowthx.com",
				"press_site_id": "abc-signin.tbogrowthx.com",
				"status": "ready",
				"country": "India",
			}
		).insert(ignore_permissions=True)

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def call(self, fn, data=None, user=None):
		frappe.set_user(user or "Guest")
		frappe.local.form_dict = frappe._dict(data or {})
		frappe.local.request = None
		frappe.local.request_ip = "127.0.0.1"
		with (
			patch.object(_core, "_idempotency_key", return_value=None),
			patch.object(_core, "_require_auth"),
			patch.object(frappe.db, "rollback"),  # an error response would roll back this test's data
		):
			response = fn()
		frappe.set_user("Administrator")
		return response.status_code, json.loads(response.get_data())

	def test_site_info(self):
		expected = {
			"siteName": "Abc Solar",
			"siteUrl": "https://abc-signin.tbogrowthx.com",
			"loginUrl": "https://abc-signin.tbogrowthx.com/login",
		}
		for site in (
			"abc-signin.tbogrowthx.com",
			"ABC-signin.tbogrowthx.com.",
			"https://abc-signin.tbogrowthx.com/login",
		):
			self.assertEqual(self.call(auth.site_info, {"site": site}), (200, expected), site)
		for site in (
			"abc-signin.tbogrowthx.co",
			"signin.tbogrowthx.com",
			"evil.com/abc-signin.tbogrowthx.com",
			"a b",
		):
			status, body = self.call(auth.site_info, {"site": site})
			self.assertEqual((status, body["error"]["code"]), (404, "site_not_found"), site)
		for status in ("archived", "failed", "provisioning"):
			frappe.db.set_value("GX Tenant", self.site.name, "status", status)
			self.assertEqual(self.call(auth.site_info, {"site": "abc-signin.tbogrowthx.com"})[0], 404, status)

	def test_custom_domain_counts_only_once_active(self):
		frappe.db.set_value(
			"GX Tenant",
			self.site.name,
			{"custom_domain": "app.abcsolar.com", "custom_domain_status": "verifying"},
		)
		self.assertEqual(self.call(auth.site_info, {"site": "app.abcsolar.com"})[0], 404)
		frappe.db.set_value("GX Tenant", self.site.name, "custom_domain_status", "active")
		for site in ("app.abcsolar.com", "abc-signin.tbogrowthx.com"):
			status, body = self.call(auth.site_info, {"site": site})
			self.assertEqual((status, body["siteUrl"]), (200, "https://app.abcsolar.com"), site)
		status, body = self.call(auth.site_login, {"site": "abc-signin.tbogrowthx.com"}, self.owner)
		self.assertTrue(body["redirectUrl"].startswith("https://app.abcsolar.com/"), body)

	def test_site_login_for_the_owner(self):
		status, body = self.call(auth.site_login, {"site": "abc-signin.tbogrowthx.com"}, self.owner)
		self.assertEqual(status, 200, body)
		url = body["redirectUrl"]
		self.assertTrue(
			url.startswith("https://abc-signin.tbogrowthx.com/api/method/growthx_theme.sso.login?token=")
		)
		token = url.split("token=", 1)[1]
		self.assertEqual(site_login.redeem(token), {"tenant": self.site.name, "user": self.owner})
		self.assertTrue(
			frappe.db.exists("GX Audit Log", {"action": "site_login", "reference_name": self.site.name})
		)

	def test_not_linked_and_unavailable(self):
		status, body = self.call(auth.site_login, {"site": "abc-signin.tbogrowthx.com"}, self.other)
		self.assertEqual((status, body["error"]["code"]), (403, "not_linked"))
		self.assertEqual(body["error"]["loginUrl"], "https://abc-signin.tbogrowthx.com/login")
		self.assertIn("Abc Solar", body["error"]["message"])
		frappe.db.set_value("GX Tenant", self.site.name, "status", "suspended")
		status, body = self.call(auth.site_login, {"site": "abc-signin.tbogrowthx.com"}, self.owner)
		self.assertEqual((status, body["error"]["code"]), (409, "site_unavailable"))
		self.assertEqual(self.call(auth.site_info, {"site": "abc-signin.tbogrowthx.com"})[0], 200)

	def test_google_sign_in_only(self):
		identity = {"email": "gx-new-google@example.com", "sub": "g-1", "name": "New Person"}
		with patch.object(google_signin, "verified_identity", return_value=identity):
			status, body = self.call(auth.google, {"code": "c", "signInOnly": True})
			self.assertEqual((status, body["error"]["code"]), (404, "no_account"))
			self.assertEqual(self.call(auth.google, {"code": "c"})[0], 202)
		with patch.object(google_signin, "verified_identity", return_value={**identity, "email": OWNER}):
			status, body = self.call(auth.google, {"code": "c", "signInOnly": True})
		self.assertEqual(status, 200, body)
