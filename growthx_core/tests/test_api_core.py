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
