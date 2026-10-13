import json
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from growthx_core.api.v1 import tenant as tenant_api
from growthx_core.provisioning import backfill, runner, site_login
from growthx_core.provisioning.press import FakePressClient, PressError

OWNER = "gx-sso-owner@example.com"
PRODUCT = "test-sso-vertical"


class RecordingPress(FakePressClient):
	def __init__(self):
		super().__init__()
		self.configured = []
		self.installed = []

	def configure_site(self, site_id, settings):
		self.configured.append((site_id, settings))

	def install_app(self, site_id, app):
		self.installed.append((site_id, app))


class TenantSSOTestCase(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		if not frappe.db.exists("GX Product", PRODUCT):
			frappe.get_doc(
				{
					"doctype": "GX Product",
					"slug": PRODUCT,
					"product_name": "SSO Test",
					"availability": "available",
				}
			).insert()
		if not frappe.db.exists("User", OWNER):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": OWNER,
					"first_name": "Sso",
					"last_name": "Owner",
					"send_welcome_email": 0,
				}
			).insert()
		if not frappe.db.exists("GX Account", {"user": OWNER}):
			frappe.get_doc(
				{
					"doctype": "GX Account",
					"account_name": "SSO Test Co",
					"user": OWNER,
					"contact_email": OWNER,
				}
			).insert()
		frappe.db.commit()
		cls.account = frappe.db.get_value("GX Account", {"user": OWNER})

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		tenants = frappe.get_all("GX Tenant", {"account": cls.account}, pluck="name")
		if tenants:
			frappe.db.delete(
				"GX Audit Log", {"reference_doctype": "GX Tenant", "reference_name": ("in", tenants)}
			)
		for name in tenants:
			frappe.delete_doc("GX Tenant", name, force=True)
		frappe.db.delete("GX Audit Log", {"actor": OWNER})
		frappe.db.delete("GX Account", {"user": OWNER})
		frappe.delete_doc("User", OWNER, force=True, ignore_missing=True)
		frappe.delete_doc("GX Product", PRODUCT, force=True, ignore_missing=True)
		frappe.db.commit()
		super().tearDownClass()

	def make_tenant(self, status="ready"):
		tenant = frappe.get_doc(
			{
				"doctype": "GX Tenant",
				"account": self.account,
				"product": PRODUCT,
				"display_name": "SSO Test Co",
				"subdomain": runner.make_subdomain("sso-test"),
				"status": status,
				"press_site_id": "fake-sso-test",
				"site_url": "https://sso-test.example",
			}
		).insert()
		return tenant


class TestRedeemLogin(TenantSSOTestCase):
	def setUp(self):
		frappe.set_user("Guest")

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def call(self, token, tenant_key):
		"""Call tenant.redeem_login as a tenant would; returns (status, body)."""
		frappe.local.form_dict = frappe._dict(token=token)
		frappe.local.request = None
		headers = {site_login.TENANT_KEY_HEADER: tenant_key}
		with patch(
			"frappe.get_request_header", side_effect=lambda key, default=None: headers.get(key, default)
		):
			response = tenant_api.redeem_login()
		return response.status_code, json.loads(response.get_data())

	def setup_tenant(self, status="ready"):
		frappe.set_user("Administrator")
		tenant = self.make_tenant(status)
		key = site_login.new_tenant_key(tenant)
		# The endpoint rolls back on errors, so the test data must be committed (cleaned up in tearDownClass).
		frappe.db.commit()
		frappe.set_user("Guest")
		return tenant, key

	def test_valid_token_returns_the_owner(self):
		tenant, key = self.setup_tenant()
		token = site_login.issue(tenant.name, OWNER)
		status, body = self.call(token, key)
		self.assertEqual(status, 200)
		self.assertEqual(body, {"email": OWNER, "fullName": "Sso Owner"})
		log = frappe.get_last_doc(
			"GX Audit Log", {"action": "tenant_login_redeemed", "reference_name": tenant.name}
		)
		self.assertEqual(log.actor, OWNER)
		self.assertNotIn(token, log.details or "")

	def test_reused_token_is_rejected(self):
		tenant, key = self.setup_tenant()
		token = site_login.issue(tenant.name, OWNER)
		self.assertEqual(self.call(token, key)[0], 200)
		status, body = self.call(token, key)
		self.assertEqual(status, 404)
		self.assertEqual(body["error"]["code"], "invalid_login_token")

	def test_expired_token_is_rejected(self):
		tenant, key = self.setup_tenant()
		token = site_login.issue(tenant.name, OWNER)
		frappe.cache.delete(site_login._key(token))  # what Redis does after 60 s
		self.assertEqual(self.call(token, key)[0], 404)

	def test_token_ttl_is_60_seconds(self):
		token = site_login.issue("GXT-X", OWNER)
		self.assertLessEqual(frappe.cache.ttl(site_login._key(token)), 60)

	def test_wrong_or_missing_tenant_key_is_rejected(self):
		tenant, _key = self.setup_tenant()
		token = site_login.issue(tenant.name, OWNER)
		for bad in ("gxt_wrong", "", None, "not-a-tenant-key"):
			status, body = self.call(token, bad)
			self.assertEqual(status, 401)
			self.assertEqual(body["error"]["code"], "invalid_tenant_key")
		# A bad key doesn't burn the customer's link
		self.assertEqual(self.call(token, _key)[0], 200)

	def test_token_for_tenant_a_does_not_work_on_tenant_b(self):
		tenant_a, key_a = self.setup_tenant()
		tenant_b, key_b = self.setup_tenant()
		token = site_login.issue(tenant_a.name, OWNER)
		status, _ = self.call(token, key_b)
		self.assertEqual(status, 404)
		self.assertTrue(
			frappe.db.exists(
				"GX Audit Log", {"action": "tenant_login_rejected", "reference_name": tenant_b.name}
			)
		)
		# ...and the token is spent, so it can't be retried on A either
		self.assertEqual(self.call(token, key_a)[0], 404)

	def test_new_key_replaces_the_old_one(self):
		tenant, old_key = self.setup_tenant()
		frappe.set_user("Administrator")
		new_key = site_login.new_tenant_key(tenant)
		frappe.db.commit()
		frappe.set_user("Guest")
		self.assertIsNone(site_login.tenant_for_key(old_key))
		self.assertEqual(site_login.tenant_for_key(new_key), tenant.name)

	def test_only_the_key_hash_is_stored(self):
		tenant, key = self.setup_tenant()
		stored = frappe.db.get_value("GX Tenant", tenant.name, "tenant_key_hash")
		self.assertNotEqual(stored, key)
		self.assertNotIn(key, stored)

	def test_disabled_user_or_not_ready_tenant_is_rejected(self):
		tenant, key = self.setup_tenant(status="suspended")
		self.assertEqual(self.call(site_login.issue(tenant.name, OWNER), key)[0], 404)

		tenant, key = self.setup_tenant()
		frappe.db.set_value("User", OWNER, "enabled", 0)
		try:
			self.assertEqual(self.call(site_login.issue(tenant.name, OWNER), key)[0], 404)
		finally:
			frappe.db.set_value("User", OWNER, "enabled", 1)

	def test_token_for_someone_else_is_rejected(self):
		tenant, key = self.setup_tenant()
		self.assertEqual(self.call(site_login.issue(tenant.name, "Administrator"), key)[0], 404)

	def test_rate_limited_per_tenant(self):
		tenant, key = self.setup_tenant()
		frappe.flags.gx_enforce_rate_limits = True
		try:
			statuses = [self.call("unknown-token", key)[0] for _ in range(31)]
		finally:
			frappe.flags.gx_enforce_rate_limits = False
			frappe.cache.delete_keys("gx:rl:")
		self.assertEqual(statuses[:30], [404] * 30)
		self.assertEqual(statuses[30], 429)


class TestProvisioningSSO(TenantSSOTestCase):
	def tearDown(self):
		frappe.db.rollback()

	def test_configure_step_writes_core_url_and_a_fresh_tenant_key(self):
		tenant = self.make_tenant(status="provisioning")
		press = RecordingPress()
		with patch.dict(frappe.local.conf, {"growthx_core_url": "http://core.test:8002/"}):
			runner._configure(tenant, None, press)

		[(site_id, settings)] = press.configured
		self.assertEqual(site_id, "fake-sso-test")
		self.assertEqual(settings["core_url"], "http://core.test:8002")
		self.assertTrue(settings["tenant_key"].startswith("gxt_"))
		self.assertEqual(site_login.tenant_for_key(settings["tenant_key"]), tenant.name)
		self.assertEqual(settings["owner_email"], OWNER)
		self.assertEqual(settings["owner_name"], "Sso Owner")
		self.assertFalse(settings["reset_owner_password"])
		self.assertNotIn("password", {k for k in settings if k != "reset_owner_password"})

	def test_new_sites_get_growthx_theme(self):
		tenant = self.make_tenant(status="provisioning")
		tenant.press_site_id = None
		press = RecordingPress()
		with patch.object(press, "create_site", wraps=press.create_site) as create_site:
			runner._create_site(tenant, None, press)
		self.assertIn("growthx_theme", create_site.call_args.args[2])

	def test_install_apps_step_fails_without_growthx_theme(self):
		tenant = self.make_tenant(status="provisioning")
		press = RecordingPress()
		with patch.object(press, "get_site", return_value=press.get_site("x")) as get_site:
			get_site.return_value.apps = ["frappe", "erpnext"]
			with self.assertRaises(PressError) as raised:
				runner._install_apps(tenant, None, press)
		self.assertIn("growthx_theme", str(raised.exception))

	def test_backfill_installs_theme_rotates_key_and_resets_password(self):
		tenant = self.make_tenant()
		old_key = site_login.new_tenant_key(tenant)
		press = RecordingPress()
		with (
			patch.object(backfill, "get_client", return_value=press),
			patch.object(backfill, "update_site_config") as update_site_config,
			patch.dict(frappe.local.conf, {"growthx_tenant_login_path": "/login#login"}),
			patch.object(frappe.db, "commit"),
		):
			result = backfill.enable_sso(tenant=tenant.name)

		self.assertEqual(press.installed, [("fake-sso-test", "growthx_theme")])
		[(_, settings)] = press.configured
		self.assertTrue(settings["reset_owner_password"])
		self.assertIsNone(site_login.tenant_for_key(old_key))
		self.assertEqual(site_login.tenant_for_key(settings["tenant_key"]), tenant.name)
		update_site_config.assert_called_once_with("growthx_tenant_login_path", "None")
		self.assertEqual(len(result["done"]), 1)
