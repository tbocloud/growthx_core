import json
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from growthx_core.api.v1 import _core, partner


def _call(data):
	frappe.local.form_dict = frappe._dict(data)
	frappe.local.request_ip = "127.0.0.1"
	response = partner.referrer()
	return response.status_code, json.loads(response.get_data())


class TestReferrer(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		p = patch.object(_core, "_check_rate_limit")
		p.start()
		self.addCleanup(p.stop)
		self.code = f"WELCOME-{frappe.generate_hash(length=6).upper()}"
		self.profile = frappe.get_doc(
			{
				"doctype": "GX Partner Profile",
				"partner_name": "Welcome Tester",
				"company": "Tester Consulting",
				"referral_code": self.code,
				"status": "active",
			}
		).insert(ignore_permissions=True, ignore_mandatory=True, ignore_links=True)

	def tearDown(self):
		frappe.db.delete("GX Partner Profile", {"referral_code": ("like", "WELCOME-%")})
		frappe.db.commit()

	def test_active_partner_is_named(self):
		status, body = _call({"code": self.code.lower()})
		self.assertEqual(status, 200)
		self.assertEqual(body, {"code": self.code, "name": "Welcome Tester", "company": "Tester Consulting"})

	def test_unknown_or_inactive_code_is_404(self):
		frappe.db.set_value("GX Partner Profile", self.profile.name, "status", "pending_approval")
		self.assertEqual(_call({"code": self.code})[0], 404)
		self.assertEqual(_call({"code": "NOBODY-123"})[0], 404)
