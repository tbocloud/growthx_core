import json
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

import growthx_core
from growthx_core.api.v1 import _core
from growthx_core.api.v1.health import ping


def _json(response):
	return json.loads(response.get_data())


class TestApiCore(IntegrationTestCase):
	def test_ping_is_plain_json(self):
		response = ping()
		self.assertEqual(response.status_code, 200)
		self.assertEqual(_json(response), {"status": "ok", "version": growthx_core.__version__})
		self.assertEqual(response.headers["Cache-Control"], "no-store")

	def test_money_is_integer_paise(self):
		self.assertEqual(_core.money(1499), {"amount": 149900, "currency": "INR"})
		self.assertEqual(_core.money("0.105"), {"amount": 11, "currency": "INR"})
		self.assertEqual(_core.money(None), {"amount": 0, "currency": "INR"})

	def test_iso_converts_site_time_to_utc(self):
		with patch.object(_core, "get_system_timezone", return_value="Asia/Kolkata"):
			self.assertEqual(_core.iso("2026-10-12 05:30:00"), "2026-10-12T00:00:00Z")
		self.assertIsNone(_core.iso(None))

	def test_gst_and_monthly_helpers(self):
		self.assertAlmostEqual(_core.net_of_gst(118, 1, 18), 100)
		self.assertEqual(_core.net_of_gst(100, 0, 18), 100)
		self.assertEqual(_core.monthly(12000, "year"), 1000)

	def test_unhandled_error_hides_traceback(self):
		@_core.endpoint(auth=False)
		def boom():
			raise RuntimeError("secret internals")

		response = boom()
		body = _json(response)
		self.assertEqual(response.status_code, 500)
		self.assertEqual(body["error"]["code"], "internal_error")
		self.assertNotIn("secret internals", response.get_data(as_text=True))

	def test_auth_required_without_token(self):
		@_core.endpoint()
		def private():
			return {"ok": True}

		frappe.local.gx_token = None
		response = private()
		self.assertEqual(response.status_code, 401)
		self.assertEqual(_json(response)["error"]["code"], "unauthenticated")


class TestClientIp(IntegrationTestCase):
	"""The website calls Core from its server and forwards the visitor's IP with the shared key."""

	def _ip(self, headers, conf_key="web-key-for-tests"):
		from werkzeug.test import EnvironBuilder
		from werkzeug.wrappers import Request

		request = Request(EnvironBuilder(headers=headers).get_environ())
		with (
			patch.object(frappe.local, "request", request, create=True),
			patch.object(frappe.local, "request_ip", "10.0.0.5", create=True),
			patch.dict(frappe.local.conf, {"growthx_web_key": conf_key}),
		):
			return _core.client_ip()

	def test_visitor_ip_is_used_with_the_right_key(self):
		headers = {_core.WEB_KEY_HEADER: "web-key-for-tests", _core.CLIENT_IP_HEADER: "203.0.113.7"}
		self.assertEqual(self._ip(headers), "203.0.113.7")
		headers[_core.CLIENT_IP_HEADER] = "2001:db8::1"
		self.assertEqual(self._ip(headers), "2001:db8::1")

	def test_forwarded_ip_is_ignored_without_the_right_key(self):
		self.assertEqual(self._ip({_core.CLIENT_IP_HEADER: "203.0.113.7"}), "10.0.0.5")
		wrong = {_core.WEB_KEY_HEADER: "guess", _core.CLIENT_IP_HEADER: "203.0.113.7"}
		self.assertEqual(self._ip(wrong), "10.0.0.5")
		right = {_core.WEB_KEY_HEADER: "web-key-for-tests", _core.CLIENT_IP_HEADER: "203.0.113.7"}
		self.assertEqual(self._ip(right, conf_key=None), "10.0.0.5")  # Core has no key set

	def test_unknown_or_junk_ip_falls_back_to_the_connection(self):
		for value in ("unknown", "203.0.113.7, 1.2.3.4", ""):
			headers = {_core.WEB_KEY_HEADER: "web-key-for-tests", _core.CLIENT_IP_HEADER: value}
			self.assertEqual(self._ip(headers), "10.0.0.5")
