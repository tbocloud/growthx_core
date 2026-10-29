"""Archiving a failed site (admin.archive_tenant and the GX Tenant form's button), so a failed test
sign-up stops keeping health.provisioning at 503."""

import json
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from growthx_core.api.v1 import _core, admin
from growthx_core.provisioning import runner


def _call(fn, data):
	frappe.local.form_dict = frappe._dict(data)
	frappe.local.request = None
	frappe.local.request_ip = "127.0.0.1"
	with (
		patch.object(_core, "_idempotency_key", return_value=None),
		patch.object(_core, "_require_auth"),  # the caller is an admin
		patch.object(frappe.db, "rollback"),  # an error response would roll back this test's tenants
	):
		response = fn()
	return response.status_code, json.loads(response.get_data())


class TestArchiveFailed(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.press = MagicMock()
		self.client = patch.object(runner, "get_client", return_value=self.press)
		self.client.start()
		self.commit = patch.object(frappe.db, "commit")  # runner.archive commits; keep it rollback-able
		self.commit.start()
		frappe.get_doc(
			{
				"doctype": "GX Product",
				"slug": "test-archive",
				"product_name": "Test",
				"availability": "available",
			}
		).insert()
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": "gx-archive-test@example.com",
				"first_name": "Archive",
				"send_welcome_email": 0,
			}
		).insert()
		self.account = (
			frappe.get_doc(
				{
					"doctype": "GX Account",
					"account_name": "Archive Test",
					"user": user.name,
					"contact_email": user.name,
				}
			)
			.insert()
			.name
		)

	def tearDown(self):
		self.commit.stop()
		self.client.stop()
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def _tenant(self, status, press_site_id=None):
		return frappe.get_doc(
			{
				"doctype": "GX Tenant",
				"account": self.account,
				"product": "test-archive",
				"status": status,
				"subdomain": frappe.generate_hash(length=10),
				"press_site_id": press_site_id,
			}
		).insert()

	def test_failed_site_is_archived_on_press_too(self):
		tenant = self._tenant("failed", press_site_id="test-noor.tbogrowthx.com")
		status, body = _call(admin.archive_tenant, {"id": tenant.name})
		self.assertEqual(status, 200, body)
		self.assertEqual(frappe.db.get_value("GX Tenant", tenant.name, "status"), "archived")
		self.press.archive_site.assert_called_once_with("test-noor.tbogrowthx.com")
		self.assertTrue(
			frappe.db.exists("GX Audit Log", {"action": "archive_tenant", "reference_name": tenant.name})
		)

	def test_failed_before_press_had_a_site(self):
		tenant = self._tenant("failed")
		self.assertTrue(runner.archive_failed(tenant.name))
		self.press.archive_site.assert_not_called()

	def test_only_failed_sites(self):
		for site_status in ("ready", "provisioning", "suspended"):
			tenant = self._tenant(site_status)
			status, body = _call(admin.archive_tenant, {"id": tenant.name})
			self.assertEqual((status, body["error"]["code"]), (409, "not_archivable"))
			self.assertEqual(frappe.db.get_value("GX Tenant", tenant.name, "status"), site_status)
		self.press.archive_site.assert_not_called()

	def test_form_button(self):
		tenant = self._tenant("failed")
		tenant.archive_failed_site()
		self.assertEqual(frappe.db.get_value("GX Tenant", tenant.name, "status"), "archived")

		ready = self._tenant("ready")
		with self.assertRaises(frappe.ValidationError):
			ready.archive_failed_site()
