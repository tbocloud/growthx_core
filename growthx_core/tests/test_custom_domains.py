"""Custom domains: customer.custom_domain / add / check / remove, the schedule, and Press's refusals."""

import json
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from growthx_core import accounts
from growthx_core.api.v1 import _core, customer
from growthx_core.provisioning import custom_domains, press

PRODUCT = "test-custom-domains"
PLAN = "test-custom-domains-month"
EMAIL = "gx-domain-test@example.com"


class TestCustomDomains(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		frappe.get_doc(
			{"doctype": "GX Product", "slug": PRODUCT, "product_name": "Test", "availability": "available"}
		).insert()
		frappe.get_doc(
			{
				"doctype": "GX Plan",
				"plan_id": PLAN,
				"product": PRODUCT,
				"plan_name": "Test Growth",
				"billing_interval": "month",
				"price": 1000,
				"is_active": 1,
			}
		).insert()
		self.user, _ = accounts.create_customer_account(
			email=EMAIL, full_name="Noor Domain", company="Noor Domain Solar", password="Long-Enough-Pass-91"
		)
		account = frappe.db.get_value("GX Account", {"user": self.user})
		self.sub = frappe.get_doc(
			{
				"doctype": "GX Subscription",
				"account": account,
				"plan": PLAN,
				"status": "active",
				"price": 1000,
			}
		).insert(ignore_permissions=True)
		self.site = frappe.get_doc(
			{
				"doctype": "GX Tenant",
				"account": account,
				"product": PRODUCT,
				"subscription": self.sub.name,
				"subdomain": "noor-domain",
				"site_url": "https://noor-domain.tbogrowthx.com",
				"press_site_id": "noor-domain.tbogrowthx.com",
				"status": "ready",
				"country": "India",
			}
		).insert(ignore_permissions=True)
		self.press = press.FakePressClient()
		self.client = patch.object(press, "get_client", return_value=self.press)
		self.client.start()

	def tearDown(self):
		self.client.stop()
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def call(self, fn, data=None):
		frappe.set_user(self.user)
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

	def add(self, domain="app.noorsolar.com"):
		return self.call(customer.add_custom_domain, {"id": self.site.name, "domain": domain})

	def check(self):
		return self.call(customer.check_custom_domain, {"id": self.site.name})

	# ------------------------------------------------------------ rules

	def test_domain_rules(self):
		for domain in (
			"noorsolar.com",
			"noorsolar.co.in",
			"x.tbogrowthx.com",
			"tbocloud.in",
			"https://app.a.com",
			"1.2.3.4",
			"app_x.a.com",
		):
			status, body = self.add(domain)
			self.assertEqual((status, body["error"]["code"]), (422, "validation_failed"), domain)
			self.assertIn("domain", body["error"]["fields"])
		status, body = self.add(" App.NoorSolar.co.in. ")
		self.assertEqual(status, 201, body)
		self.assertEqual(
			(body["domain"], body["status"], body["records"], body["dnsProblem"]),
			(
				"app.noorsolar.co.in",
				"pending_dns",
				[{"type": "CNAME", "host": "app", "value": "noor-domain.tbogrowthx.com"}],
				None,
			),
		)

	def test_one_per_site_and_no_sharing(self):
		self.assertEqual(self.add()[0], 201)
		self.assertEqual(self.add("erp.noorsolar.com")[1]["error"]["code"], "domain_exists")
		frappe.db.set_value("GX Tenant", self.site.name, "custom_domain", None)
		other = frappe.copy_doc(self.site)
		other.subdomain, other.custom_domain = "noor-other", "app.noorsolar.com"
		other.insert(ignore_permissions=True)
		self.assertEqual(self.add()[1]["error"]["code"], "domain_taken")

	def test_trial_and_not_ready(self):
		frappe.db.set_value("GX Subscription", self.sub.name, "status", "trialing")
		status, body = self.add()
		self.assertEqual((status, body["error"]["code"]), (403, "plan_required"))
		frappe.db.set_value("GX Subscription", self.sub.name, "status", "active")
		frappe.db.set_value("GX Tenant", self.site.name, "status", "provisioning")
		self.assertEqual(self.add()[1]["error"]["code"], "site_not_ready")

	def test_someone_elses_site_is_404(self):
		frappe.db.set_value("GX Tenant", self.site.name, "account", None, update_modified=False)
		status, _ = self.call(customer.custom_domain, {"id": self.site.name})
		self.assertEqual(status, 404)

	# ------------------------------------------------------------ the path to active

	def test_pending_then_verifying_then_active_and_open_my_app_uses_it(self):
		self.add()
		self.press.dns_problem = "wrong_target"
		status, body = self.check()
		self.assertEqual((status, body["status"], body["dnsProblem"]), (200, "pending_dns", "wrong_target"))
		self.assertIn("noor-domain.tbogrowthx.com", body["message"])
		self.assertTrue(body["checkedAt"])

		self.press.dns_problem, self.press.press_domain_status = None, "pending"
		_, body = self.check()
		self.assertEqual((body["status"], body["dnsProblem"], body["message"]), ("verifying", None, None))

		self.press.press_domain_status = "active"
		with patch.object(self.press, "set_host_name") as set_host_name:
			_, body = self.check()
		set_host_name.assert_called_once_with("noor-domain.tbogrowthx.com", "app.noorsolar.com")
		self.assertEqual((body["status"], body["records"]), ("active", []))
		self.assertTrue(body["activeAt"])

		_, body = self.call(customer.open_site, {"id": self.site.name})
		self.assertTrue(body["url"].startswith("https://app.noorsolar.com/"), body)

	def test_each_dns_refusal_is_explained(self):
		self.add()
		for problem in ("not_found", "multiple_records", "aaaa_record", "caa_blocks", "cloudflare_proxied"):
			self.press.dns_problem = problem
			_, body = self.check()
			self.assertEqual((body["status"], body["dnsProblem"]), ("pending_dns", problem))
			self.assertTrue(body["message"])
		self.press.dns_problem = "domain_taken"
		_, body = self.check()
		self.assertEqual((body["status"], body["dnsProblem"]), ("failed", None))
		self.assertIn("another site", body["message"])

	def test_broken_certificate_fails_and_press_down_keeps_the_state(self):
		self.add()
		with patch.object(self.press, "check_dns", side_effect=press.PressError("HTTP 502")):
			_, body = self.check()
		self.assertEqual(body["status"], "pending_dns")
		self.assertIn("try again", body["message"])
		self.press.press_domain_status = "broken"
		_, body = self.check()
		self.assertEqual(body["status"], "failed")

	def test_remove(self):
		self.assertEqual(self.call(customer.remove_custom_domain, {"id": self.site.name})[0], 404)
		self.add()
		self.check()
		with (
			patch.object(self.press, "set_host_name") as set_host_name,
			patch.object(self.press, "remove_domain") as remove,
		):
			status, body = self.call(customer.remove_custom_domain, {"id": self.site.name})
		self.assertEqual((status, body), (200, {"ok": True}))
		set_host_name.assert_called_once_with("noor-domain.tbogrowthx.com", "noor-domain.tbogrowthx.com")
		remove.assert_called_once()
		self.assertEqual(self.call(customer.custom_domain, {"id": self.site.name})[1], {"customDomain": None})
		_, body = self.call(customer.open_site, {"id": self.site.name})
		self.assertTrue(body["url"].startswith("https://noor-domain.tbogrowthx.com/"))

	def test_schedule_checks_waiting_domains(self):
		self.add()
		self.press.press_domain_status = "pending"
		with patch.object(frappe.db, "commit"):
			custom_domains.check_pending()
		self.assertEqual(
			frappe.db.get_value("GX Tenant", self.site.name, "custom_domain_status"), "verifying"
		)


class TestRealPressDomains(IntegrationTestCase):
	"""RealPressClient: Press's refusals become DnsProblems."""

	def setUp(self):
		self.client = press.RealPressClient(
			{"url": "https://press.example", "api_key": "k", "api_secret": "s"}
		)
		self.own = patch.object(self.client, "_own")
		self.own.start()

	def tearDown(self):
		self.own.stop()

	def _reply(self, status=200, message=None, exc_type=None, text=""):
		r = MagicMock(status_code=status)
		body = (
			{"message": message}
			if status == 200
			else {"exc_type": exc_type, "_server_messages": json.dumps([json.dumps({"message": text})])}
		)
		r.json.return_value = body
		return r

	def _problem(self, reply, method="check_dns"):
		with patch.object(self.client.session, "post", return_value=reply):
			try:
				getattr(self.client, method)("noor.tbogrowthx.com", "app.noorsolar.com")
			except press.DnsProblem as e:
				self.assertNotIn("appears", str(e))  # Press's text isn't kept
				return e.problem
			except press.PressError:
				return None
		return "matched"

	def test_check_dns_answers(self):
		self.assertEqual(self._problem(self._reply(message={"matched": True})), "matched")
		self.assertEqual(
			self._problem(self._reply(message={"matched": False, "CNAME": {"exists": True}})), "wrong_target"
		)
		self.assertEqual(
			self._problem(self._reply(message={"matched": False, "CNAME": {}, "A": {}})), "not_found"
		)

	def test_refusals(self):
		cases = {
			("DNSValidationError", "Unable to connect to the domain"): "not_found",
			("DomainProxied", "Domain appears to be proxied (server: nginx)"): "wrong_target",
			("DomainProxied", "Domain appears to be proxied (server: cloudflare)"): "cloudflare_proxied",
			(
				"ConflictingDNSRecord",
				"correct A record, but also a CNAME to an incorrect domain",
			): "wrong_target",
			("MultipleARecords", "Domain has multiple A records"): "multiple_records",
			("AAAARecordExists", "Domain has an AAAA record"): "aaaa_record",
			("ConflictingCAARecord", "CAA record blocks letsencrypt"): "caa_blocks",
			("DuplicateEntryError", "Site Domain app.noorsolar.com already exists"): "domain_taken",
			# Press's text alone never decides it
			("ValidationError", "Domain is reserved, see https://docs.frappe.io/x"): None,
		}
		for (exc_type, text), problem in cases.items():
			self.assertEqual(
				self._problem(self._reply(417, exc_type=exc_type, text=text), "add_domain"), problem, text
			)
		with patch.object(
			self.client.session, "post", return_value=self._reply(500, exc_type="Exception", text="boom")
		):
			with self.assertRaises(press.PressError) as e:
				self.client.add_domain("noor.tbogrowthx.com", "app.noorsolar.com")
		self.assertNotIsInstance(e.exception, press.DnsProblem)

	def test_domain_status(self):
		rows = [
			{"domain": "noor.tbogrowthx.com", "status": "Active"},
			{"domain": "app.noorsolar.com", "status": "In Progress"},
		]
		with patch.object(self.client.session, "post", return_value=self._reply(message=rows)):
			self.assertEqual(self.client.domain_status("noor.tbogrowthx.com", "app.noorsolar.com"), "pending")
			self.assertIsNone(self.client.domain_status("noor.tbogrowthx.com", "x.noorsolar.com"))
