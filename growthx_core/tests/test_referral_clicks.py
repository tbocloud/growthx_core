"""referral.record_click (from the website's /r/CODE page) and clicks in partner.referral_stats."""

import json
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from growthx_core import accounts
from growthx_core.api.v1 import partner as partner_api
from growthx_core.api.v1 import referral

WEB_KEY = "test-web-key"


class TestReferralClicks(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.conf = patch.dict(frappe.conf, {"growthx_web_key": WEB_KEY})
		self.conf.start()
		self.user = accounts.create_partner_account(
			email="gx-click-partner@example.com", full_name="Click Partner", password="Long-Enough-Pass-91"
		)
		doc = frappe.get_doc("GX Partner Profile", {"user": self.user})
		doc.status = "active"
		doc.save()
		self.partner, self.code = doc.name, doc.referral_code

	def tearDown(self):
		self.conf.stop()
		frappe.cache.delete_keys("growthx:referral_click:")
		frappe.set_user("Administrator")
		frappe.local.gx_token = None
		frappe.db.rollback()

	def click(self, code=None, ip="203.0.113.7", key=WEB_KEY):
		headers = {"X-GrowthX-Web-Key": key, "X-GrowthX-Client-IP": ip}
		frappe.local.request = MagicMock()
		frappe.local.request_ip = "10.0.0.1"
		frappe.local.form_dict = frappe._dict({"code": code or self.code, "landingPath": "/r/x"})
		with patch(
			"frappe.get_request_header", side_effect=lambda name, default=None: headers.get(name, default)
		):
			response = referral.record_click()
		return response.status_code, json.loads(response.get_data())

	def _count(self):
		return frappe.db.count("GX Referral Click", {"partner": self.partner})

	def test_counted_once_per_visitor_every_30_minutes(self):
		self.assertEqual(self.click(), (202, {"counted": True}))
		self.assertEqual(self.click(), (202, {"counted": False}))  # same IP and code
		self.assertEqual(self.click(ip="203.0.113.8"), (202, {"counted": True}))
		self.assertEqual(self.click(code=self.code.lower(), ip="203.0.113.9"), (202, {"counted": True}))
		self.assertEqual(self._count(), 3)
		hashes = frappe.get_all("GX Referral Click", {"partner": self.partner}, pluck="visitor_hash")
		self.assertFalse(any("203.0.113" in h for h in hashes))  # never the IP itself

	def test_unknown_or_inactive_code_looks_the_same(self):
		self.assertEqual(self.click(code="NOPE-123"), (202, {"counted": False}))
		frappe.db.set_value("GX Partner Profile", self.partner, "status", "suspended")
		self.assertEqual(self.click(), (202, {"counted": False}))
		self.assertEqual(self._count(), 0)

	def test_needs_the_web_key(self):
		status, body = self.click(key="wrong")
		self.assertEqual((status, body["error"]["code"]), (403, "forbidden"))
		self.assertEqual(self._count(), 0)

	def test_referral_stats_shows_clicks(self):
		self.click()
		self.click(ip="203.0.113.8")
		frappe.set_user(self.user)
		frappe.local.gx_token = "test-token"
		frappe.local.form_dict = frappe._dict({"from": "2000-01-01", "to": "2100-01-01"})
		body = json.loads(partner_api.referral_stats().get_data())
		self.assertEqual(body["clicks"], 2)
