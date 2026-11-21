"""Admin portal Press inventory: admin.press_inventory."""

import json
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from growthx_core.api.v1 import _core, admin
from growthx_core.provisioning import press

PRODUCT = "test-inventory"
OWNER = "inventory-owner@example.com"
MANAGED = "managed-site.tbogrowthx.com"
DIRECT = "direct-site.tbogrowthx.com"


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


def _press_row(name, bench="bench-0024-000002-f1", group="bench-0024"):
	return {
		"name": name,
		"url": f"https://{name}",
		"status": "active",
		"press_status": "Active",
		"bench": bench,
		"release_group": group,
		"release_group_title": "Laundry",
		"plan": "GrowthX Demo",
		"created_at": "2026-11-21 14:58:00.000000",
	}


class TestAdminPressInventory(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		frappe.cache.delete_value(admin.PRESS_SITES_CACHE)
		if not frappe.db.exists("User", OWNER):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": OWNER,
					"first_name": "Inventory",
					"user_type": "Website User",
					"send_welcome_email": 0,
				}
			).insert()
		if not frappe.db.exists("GX Product", PRODUCT):
			frappe.get_doc({"doctype": "GX Product", "slug": PRODUCT, "product_name": "Inventory"}).insert()
		if not frappe.db.exists("GX Account", {"user": OWNER}):
			frappe.get_doc(
				{
					"doctype": "GX Account",
					"account_name": "Managed Co",
					"user": OWNER,
					"contact_email": OWNER,
				}
			).insert()
		self.account = frappe.db.get_value("GX Account", {"user": OWNER})
		for name in frappe.get_all("GX Tenant", {"press_site_id": MANAGED}, pluck="name"):
			frappe.delete_doc("GX Tenant", name, force=True)
		self.tenant = frappe.get_doc(
			{
				"doctype": "GX Tenant",
				"account": self.account,
				"product": PRODUCT,
				"display_name": "Managed Co",
				"press_site_id": MANAGED,
				"site_url": f"https://{MANAGED}",
				"subdomain": "managed-site",
				"status": "ready",
			}
		).insert()

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.cache.delete_value(admin.PRESS_SITES_CACHE)
		frappe.db.rollback()

	def _client(self, rows):
		client = press.FakePressClient()
		client.press_sites = rows
		return patch.object(press, "get_client", return_value=client)

	def test_a_site_core_created_is_marked_managed_and_the_rest_are_not(self):
		with self._client([_press_row(MANAGED), _press_row(DIRECT, bench="bench-0019-000004-f1")]):
			status, body = _call(admin.press_inventory)
		self.assertEqual(status, 200)
		self.assertEqual(body["total"], 2)
		by_name = {item["name"]: item for item in body["items"]}
		self.assertTrue(by_name[MANAGED]["managed"])
		self.assertEqual(by_name[MANAGED]["tenantId"], self.tenant.name)
		self.assertEqual(by_name[MANAGED]["origin"], "provisioned")
		self.assertEqual(by_name[MANAGED]["customerName"], "Managed Co")
		self.assertFalse(by_name[DIRECT]["managed"])
		self.assertIsNone(by_name[DIRECT]["tenantId"])
		self.assertIsNone(by_name[DIRECT]["customerName"])
		self.assertEqual(by_name[DIRECT]["bench"], "bench-0019-000004-f1")

	def test_q_searches_the_site_name_and_the_customer_name(self):
		with self._client([_press_row(MANAGED), _press_row(DIRECT)]):
			status, body = _call(admin.press_inventory, {"q": "direct-site"})
			self.assertEqual((status, body["total"]), (200, 1))
			self.assertEqual(body["items"][0]["name"], DIRECT)
			status, body = _call(admin.press_inventory, {"q": "managed co"})
			self.assertEqual((status, body["total"]), (200, 1))
			self.assertEqual(body["items"][0]["name"], MANAGED)

	def test_a_site_listed_once_is_reused_from_the_cache(self):
		client = press.FakePressClient()
		client.press_sites = [_press_row(MANAGED)]
		with patch.object(press, "get_client", return_value=client) as get_client:
			_call(admin.press_inventory)
			_call(admin.press_inventory)
		self.assertEqual(get_client.call_count, 1)

	def test_press_being_unreachable_is_a_502(self):
		with patch.object(press, "get_client", side_effect=press.PressError("Press unreachable")):
			status, body = _call(admin.press_inventory)
		self.assertEqual(status, 502)
		self.assertEqual(body["error"]["code"], "press_unavailable")
