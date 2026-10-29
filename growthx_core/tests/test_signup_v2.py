"""Sign-up v2, steps 1-2 (docs 09): signup.options, tenant.check_subdomain, and the customer's
subdomain, country and chart of accounts on auth.signup."""

import json
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from growthx_core import signup_codes
from growthx_core.api.v1 import _core, auth, signup, tenant
from growthx_core.provisioning import runner
from growthx_core.provisioning.press import PressError, RealPressClient

PRODUCT = "test-signup-v2"
PLAN = "test-signup-v2-month"
TEST_SITES = {"growthx_press": {"test_sites": 1}}


def _call(fn, data=None):
	frappe.local.form_dict = frappe._dict(data or {})
	frappe.local.request = None
	frappe.local.request_ip = "127.0.0.1"
	with patch.object(_core, "_idempotency_key", return_value=None):  # no request headers here
		response = fn()
	return response.status_code, json.loads(response.get_data())


class TestSignupV2(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		code_check = patch.object(signup_codes, "check")  # email codes: test_signup_email_code
		code_check.start()
		self.addCleanup(code_check.stop)
		frappe.get_doc(
			{"doctype": "GX Product", "slug": PRODUCT, "product_name": "Test", "availability": "available"}
		).insert()
		frappe.get_doc(
			{
				"doctype": "GX Plan",
				"plan_id": PLAN,
				"product": PRODUCT,
				"plan_name": "Test",
				"billing_interval": "month",
				"price": 100,
				"trial_days": 14,
				"is_active": 1,
			}
		).insert()

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def _taken(self, subdomain):
		frappe.get_doc(
			{"doctype": "GX Tenant", "product": PRODUCT, "subdomain": subdomain, "status": "archived"}
		).insert(ignore_mandatory=True)

	def _signup(self, key, **extra):
		frappe.set_user("Guest")
		return _call(
			auth.signup,
			{
				"accountType": "customer",
				"name": "Noor Test",
				"email": f"gx-signup-v2-{key}@example.com",
				"password": "Long-Enough-Pass-91",
				"emailCode": "123456",
				"company": f"Noor Signup {key}",
				"planId": PLAN,
				**extra,
			},
		)

	def _tenant(self, body):
		return frappe.get_doc("GX Tenant", body["site"]["id"])

	# ---- signup.options

	def test_options_lists_india_and_uae_with_erpnext_charts(self):
		status, body = _call(signup.options)
		self.assertEqual(status, 200)
		by_code = {c["code"]: c for c in body["countries"]}
		self.assertEqual(set(by_code), {"IN", "AE"})
		self.assertEqual(
			(by_code["IN"]["currency"], by_code["IN"]["timezone"], by_code["IN"]["fiscalYearStart"]),
			("INR", "Asia/Kolkata", "04-01"),
		)
		self.assertEqual(
			(by_code["AE"]["currency"], by_code["AE"]["timezone"], by_code["AE"]["fiscalYearStart"]),
			("AED", "Asia/Dubai", "01-01"),
		)
		for c in by_code.values():
			self.assertIn(c["defaultChart"], c["charts"])
			self.assertIn("Standard", c["charts"])
		self.assertEqual(body["defaultCountry"], "IN")
		self.assertTrue(body["tenantDomain"])

	# ---- tenant.check_subdomain

	def _check(self, subdomain):
		status, body = _call(tenant.check_subdomain, {"subdomain": subdomain})
		self.assertEqual(status, 200)
		return body

	def test_free_name_is_available(self):
		body = self._check("Noor-Solar-Free")
		self.assertEqual(
			(body["subdomain"], body["available"], body["reason"]), ("noor-solar-free", True, None)
		)
		self.assertTrue(body["address"].startswith("noor-solar-free."))

	def test_invalid_reserved_and_taken(self):
		self.assertEqual(self._check("abcd")["reason"], "invalid")  # under 5 characters
		self.assertEqual(self._check("noor_solar")["reason"], "invalid")
		self.assertEqual(self._check("-noor-solar")["reason"], "invalid")
		self.assertEqual(self._check("support")["reason"], "reserved")
		self.assertEqual(self._check("test-noor")["reason"], "reserved")
		self.assertIsNone(self._check("test-noor")["suggestion"])

		self._taken("noor-solar-taken")
		body = self._check("noor-solar-taken")
		self.assertEqual((body["available"], body["reason"]), (False, "taken"))
		self.assertEqual(body["suggestion"], "noor-solar-taken-2")

	def test_suggestion_skips_names_that_are_also_taken(self):
		self._taken("noor-solar-taken")
		self._taken("noor-solar-taken-2")
		self.assertEqual(self._check("noor-solar-taken")["suggestion"], "noor-solar-taken-3")

	def test_core_test_site_adds_the_prefix_and_allows_27_characters(self):
		with patch.dict(frappe.local.conf, TEST_SITES):
			body = self._check("a" * 27)
			self.assertTrue(body["available"])
			self.assertTrue(body["address"].startswith("test-" + "a" * 27 + "."))
			self.assertEqual(self._check("a" * 28)["reason"], "invalid")
			self._taken("test-noor-solar-ct")
			self.assertEqual(self._check("noor-solar-ct")["reason"], "taken")

	# ---- auth.signup

	def test_signup_stores_the_customers_choices_on_the_site(self):
		status, body = self._signup(
			"uae", subdomain="Noor-UAE-Site", country="AE", chartOfAccounts="U.A.E - Chart of Accounts"
		)
		self.assertEqual(status, 201, body)
		site = self._tenant(body)
		self.assertEqual(
			(site.subdomain, site.country, site.chart_of_accounts),
			("noor-uae-site", "United Arab Emirates", "U.A.E - Chart of Accounts"),
		)
		settings = runner.tenant_settings(site)
		self.assertEqual(
			(settings["country"], settings["currency"], settings["timezone"], settings["fiscal_year_start"]),
			("United Arab Emirates", "AED", "Asia/Dubai", "01-01"),
		)
		self.assertEqual(settings["chart_of_accounts"], "U.A.E - Chart of Accounts")

	def test_signup_without_choices_keeps_todays_behaviour(self):
		status, body = self._signup("plain")
		self.assertEqual(status, 201, body)
		site = self._tenant(body)
		self.assertEqual(site.subdomain, "noor-signup-plain")
		self.assertEqual((site.country, site.chart_of_accounts), ("India", "India - Chart of Accounts"))
		settings = runner.tenant_settings(site)
		self.assertEqual((settings["currency"], settings["fiscal_year_start"]), ("INR", "04-01"))

	def test_signup_refuses_bad_choices(self):
		status, body = self._signup("badc", country="SA")
		self.assertEqual((status, list(body["error"]["fields"])), (422, ["country"]))
		status, body = self._signup("badchart", country="IN", chartOfAccounts="U.A.E - Chart of Accounts")
		self.assertEqual((status, list(body["error"]["fields"])), (422, ["chartOfAccounts"]))
		status, body = self._signup("reserved", subdomain="billing")
		self.assertEqual((status, list(body["error"]["fields"])), (422, ["subdomain"]))

	# An error response rolls back the request's transaction, and this test's plan with it: after
	# one, only calls that fail before the plan is looked up can follow in the same test.

	def test_subdomain_taken_in_core_is_409(self):
		self._taken("noor-taken-core")
		status, body = self._signup("core", subdomain="noor-taken-core")
		self.assertEqual((status, body["error"]["code"]), (409, "subdomain_taken"))
		self.assertIn("subdomain", body["error"]["fields"])

	def test_subdomain_taken_on_press_is_409_and_creates_nothing(self):
		with patch.object(runner, "press_name_taken", return_value=True) as press:
			status, body = self._signup("press", subdomain="noor-taken-press")
		self.assertEqual((status, body["error"]["code"]), (409, "subdomain_taken"))
		press.assert_called_once_with("noor-taken-press")
		self.assertFalse(frappe.db.exists("User", "gx-signup-v2-press@example.com"))

	def test_press_is_not_asked_when_no_subdomain_was_chosen(self):
		with patch.object(runner, "press_name_taken") as press:
			status, _ = self._signup("nopress")
		self.assertEqual(status, 201)
		press.assert_not_called()

	def test_press_check_lets_sign_up_through_when_press_is_down(self):
		client = RealPressClient({"url": "https://press.invalid", "api_key": "k", "api_secret": "s"})
		with (
			patch.object(runner, "get_client", return_value=client),
			patch.object(client, "exists", side_effect=PressError("rate limited")),
		):
			self.assertFalse(runner.press_name_taken("noor-press-down"))
		with (
			patch.object(runner, "get_client", return_value=client),
			patch.object(client, "exists", return_value=True),
		):
			self.assertTrue(runner.press_name_taken("noor-press-taken"))

	# ---- site-ready email (step 3)

	def test_ready_email_has_the_site_address_and_how_to_sign_in(self):
		site = frappe._dict(
			display_name="Noor <Solar>", name="GXT-1", site_url="https://noor-solar.tbogrowthx.com"
		)
		html = runner._ready_email(site, "https://tbogrowthx.com/dashboard")
		self.assertIn('href="https://noor-solar.tbogrowthx.com"', html)
		self.assertIn("Open my app", html)
		self.assertIn("Login with Email Link", html)
		self.assertIn("Noor &lt;Solar&gt;", html)
