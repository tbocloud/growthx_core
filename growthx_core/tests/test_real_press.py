"""The tbocloud.in Press client and the job's Press rules, with Press mocked (no network)."""

import json
from datetime import timedelta
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import now_datetime
from frappe.utils.password import get_decrypted_password

from growthx_core.provisioning import press, runner
from growthx_core.provisioning.press import PressError, RealPressClient

PRODUCT = "gx-press-test"
EMAIL = "gx-press-test@example.com"
GROUP = "bench-0017"
CONF = {
	"mode": "press",
	"url": "https://press.example",
	"team": "team1",
	"api_key": "k",
	"api_secret": "s",
	"domain": "tbogrowthx.com",
	"test_sites": 1,
}


def _response(status=200, message=None, body=None):
	r = MagicMock(status_code=status)
	r.json.return_value = body if body is not None else {"message": message}
	r.text = json.dumps(r.json.return_value)
	return r


class FakePress:
	"""Answers Press API calls by method name; records every call."""

	def __init__(self):
		self.calls = []
		self.taken = set()
		self.status = "Pending"
		self.group = ["frappe", "erpnext", "growthx_theme", "solar"]
		self.installed = ["frappe", "erpnext", "growthx_theme", "solar"]

	def __call__(self, url, **kwargs):
		method = url.rsplit("/", 1)[-1]
		params = kwargs.get("params") or kwargs.get("json") or {}
		self.calls.append((method, params))
		if method == "press.api.site.exists":
			return _response(message=params["subdomain"] in self.taken)
		if method == "press.api.bench.apps":
			return _response(message=[{"name": app} for app in self.group])
		if method == "press.api.site.new":
			self.taken.add(params["site"]["name"])
			return _response(message={"site": f"{params['site']['name']}.tbogrowthx.com", "job": "j1"})
		if method == "press.api.site.get":
			if params["name"].split(".")[0] not in self.taken:
				return _response(403, body={"exc_type": "PermissionError"})
			return _response(message={"name": params["name"], "status": self.status})
		if method == "press.api.site.installed_apps":
			return _response(message=[{"app": app, "name": f"{app}-src"} for app in self.installed])
		if method == "press.api.site.login":
			return _response(message={"sid": "sid-1", "site": params["name"]})
		return _response(message=None)

	def methods(self):
		return [method for method, _ in self.calls]


class TestRealPress(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.conf = patch.dict(frappe.local.conf, {"growthx_press": CONF})
		self.conf.start()
		frappe.get_doc(
			{
				"doctype": "GX Product",
				"slug": PRODUCT,
				"product_name": "Press Test",
				"availability": "available",
				"press_release_group": GROUP,
				"apps": [{"app_name": "growthx_theme"}, {"app_name": "solar"}],
			}
		).insert()
		user = frappe.get_doc(
			{"doctype": "User", "email": EMAIL, "first_name": "Press", "send_welcome_email": 0}
		).insert()
		self.account = frappe.get_doc(
			{"doctype": "GX Account", "account_name": "Press Test", "user": user.name, "contact_email": EMAIL}
		).insert()
		self.press = FakePress()
		self.client = RealPressClient(CONF)
		self.client.session = MagicMock(get=self.press, post=self.press, headers={})
		self.commit = patch.object(frappe.db, "commit")  # the job commits; keep the rollback working
		self.commit.start()

	def tearDown(self):
		self.commit.stop()
		self.conf.stop()
		frappe.db.rollback()

	def _tenant(self, name="Acme Solar", **extra):
		tenant = frappe.get_doc(
			{
				"doctype": "GX Tenant",
				"account": self.account.name,
				"product": PRODUCT,
				"display_name": name,
				"subdomain": runner.make_subdomain(name),
				**extra,
			}
		).insert()
		runner.init_steps(tenant)
		tenant.save()
		return tenant

	def _step(self, tenant, key):
		return next(row for row in tenant.steps if row.step_key == key)

	# ------------------------------------------------------------ requests

	def test_sends_the_key_and_team_and_creates_with_the_guide_payload(self):
		client = RealPressClient(CONF)
		self.assertEqual(client.session.headers["Authorization"], "token k:s")
		self.assertEqual(client.session.headers["X-Press-Team"], "team1")

		tenant = self._tenant()
		runner._create_site(tenant, self._step(tenant, "create_site"), self.client)
		self.assertEqual(self.press.methods()[0], "press.api.site.exists")
		payload = dict(self.press.calls)["press.api.site.new"]["site"]
		self.assertEqual(
			payload,
			{
				"name": "test-acme-solar",
				"domain": "tbogrowthx.com",
				"group": GROUP,
				"apps": ["frappe", "erpnext", "growthx_theme", "solar"],
				"plan": "GrowthX Demo",
				"cluster": "Default",
			},
		)
		self.assertEqual(tenant.press_site_id, "test-acme-solar.tbogrowthx.com")
		self.assertEqual(tenant.site_url, "https://test-acme-solar.tbogrowthx.com")

	def test_apps_missing_from_the_release_group_are_refused_before_site_new(self):
		tenant = self._tenant(apps=[{"app_name": "growthx_theme"}, {"app_name": "hrms"}])
		with self.assertRaisesRegex(PressError, "bench-0017: hrms"):
			runner._create_site(tenant, self._step(tenant, "create_site"), self.client)
		self.assertNotIn("press.api.site.new", self.press.methods())

	def test_status_and_installed_apps(self):
		self.press.taken.add("test-acme-solar")
		for press_status, ours in (("Installing", "creating"), ("Broken", "broken"), ("Active", "active")):
			self.press.status = press_status
			self.assertEqual(self.client.get_site("test-acme-solar.tbogrowthx.com").status, ours)
		self.assertIn("solar", self.client.get_site("test-acme-solar.tbogrowthx.com").apps)

	def test_a_name_taken_by_someone_else_is_an_error(self):
		# Press says the name is taken (another team's site, or a blocked name) but won't show it to us
		self.client.session.get = MagicMock(return_value=_response(message=True))
		self.client.session.post = MagicMock(
			return_value=_response(403, body={"exc_type": "PermissionError"})
		)
		with self.assertRaisesRegex(PressError, "isn't GrowthX's"):
			self.client.find_site("test-acme-solar")

	def test_press_errors_never_include_the_key(self):
		self.client.session.post = MagicMock(
			return_value=_response(
				417,
				body={
					"exc_type": "ValidationError",
					"_server_messages": json.dumps([json.dumps({"message": "Bad group"})]),
				},
			)
		)
		with self.assertRaises(PressError) as e:
			self.client.group_apps("nope")
		self.assertIn("Bad group", str(e.exception))
		self.assertNotIn("token k:s", str(e.exception))

	# ------------------------------------------------------------ safety

	def test_test_site_rules(self):
		self.assertEqual(runner.make_subdomain("Acme Solar"), "test-acme-solar")
		with self.assertRaisesRegex(PressError, "only create test-"):
			self.client.check_subdomain("acme-solar")
		production = RealPressClient({**CONF, "test_sites": 0})
		with self.assertRaisesRegex(PressError, "Core test site only"):
			production.check_subdomain("test-acme")
		with patch.dict(frappe.local.conf, {"growthx_press": {**CONF, "test_sites": 0}}):
			self.assertEqual(runner.make_subdomain("Test Acme"), "testacme")
			self.assertEqual(runner.make_subdomain("Abc"), "abc-site")
			self.assertEqual(runner.make_subdomain("Support"), "support-site")
			self.assertLessEqual(len(runner.make_subdomain("x" * 60)), 32)

	def test_refuses_sites_that_are_not_tenants(self):
		for call in (
			lambda: self.client.archive_site("core-test.tbogrowthx.com"),
			lambda: self.client.install_app("core-test.tbogrowthx.com", "solar"),
			lambda: self.client.configure_site("core-test.tbogrowthx.com", {}),
			lambda: self.client.create_site("test-not-a-tenant", GROUP, ["frappe"]),
		):
			with self.assertRaises(PressError):
				call()
		self.assertEqual(self.press.calls, [])

	def test_at_most_five_live_test_sites(self):
		for i in range(5):
			self._tenant(f"Live {i}", press_site_id=f"test-live-{i}.tbogrowthx.com", status="ready")
		tenant = self._tenant()
		with self.assertRaisesRegex(PressError, "archive some first"):
			runner._create_site(tenant, self._step(tenant, "create_site"), self.client)

	def test_at_most_two_sites_building(self):
		for i in range(2):
			other = self._tenant(f"Building {i}", press_site_id=f"test-b-{i}.tbogrowthx.com")
			other.db_set("status", "provisioning")
		tenant = self._tenant()
		with self.assertRaises(runner.StepPending):
			runner._create_site(tenant, self._step(tenant, "create_site"), self.client)
		self.assertNotIn("press.api.site.new", self.press.methods())

	# ------------------------------------------------------------ health check

	def test_health_check_accepts_only_a_direct_200(self):
		with patch.object(press.requests, "get", return_value=_response(307)) as get:
			self.assertFalse(self.client.health_check("https://test-gone.tbogrowthx.com"))
		self.assertFalse(get.call_args.kwargs["allow_redirects"])
		with patch.object(press.requests, "get", return_value=_response(message="pong")):
			self.assertTrue(self.client.health_check("https://test-acme-solar.tbogrowthx.com"))

	# ------------------------------------------------------------ configure

	def test_configure_logs_in_through_press_and_keeps_the_service_key(self):
		tenant = self._tenant(press_site_id="test-acme-solar.tbogrowthx.com")
		tenant_reply = _response(message={"setupComplete": True, "apiKey": "ak", "apiSecret": "as"})
		with patch.object(press.requests, "post", return_value=tenant_reply) as post:
			runner.configure(tenant, self.client)
		self.assertEqual(self.press.methods(), ["press.api.site.login"])
		url = post.call_args.args[0]
		self.assertEqual(
			url, "https://test-acme-solar.tbogrowthx.com/api/method/growthx_theme.setup.configure"
		)
		self.assertEqual(post.call_args.kwargs["cookies"], {"sid": "sid-1"})
		sent = post.call_args.kwargs["json"]["settings"]
		self.assertTrue(sent["tenant_key"].startswith("gxt_"))
		# The tenant key goes only to the tenant, never to Press
		self.assertNotIn(sent["tenant_key"], json.dumps(self.press.calls))
		self.assertEqual(tenant.service_api_key, "ak")
		self.assertEqual(get_decrypted_password("GX Tenant", tenant.name, "service_api_secret"), "as")

	# ------------------------------------------------------------ broken site, archive, retry

	def test_broken_site_is_archived_and_retry_waits_for_the_name(self):
		tenant = self._tenant()
		runner._create_site(tenant, self._step(tenant, "create_site"), self.client)
		self._step(tenant, "create_site").state = "done"
		self.press.status = "Broken"
		with self.assertRaisesRegex(PressError, "being archived"):
			runner._wait_for_site(tenant, self._step(tenant, "wait_for_site"), self.client)
		self.assertIn("press.api.site.archive", self.press.methods())
		tenant.reload()
		self.assertFalse(tenant.press_site_id)
		self.assertEqual(self._step(tenant, "create_site").state, "pending")

		# Still archiving: the name is taken by the broken site, so wait
		step = self._step(tenant, "create_site")
		step.started_at = now_datetime()
		with self.assertRaises(runner.StepPending):
			runner._create_site(tenant, step, self.client)
		# Archive done: the name is free again, so create it
		self.press.taken.clear()
		self.press.calls.clear()
		runner._create_site(tenant, step, self.client)
		self.assertIn("press.api.site.new", self.press.methods())

		# And waiting for an archive gives up after the timeout
		tenant.press_site_id = None
		step.started_at = now_datetime() - timedelta(minutes=31)
		self.press.status = "Broken"
		with self.assertRaisesRegex(PressError, "Timed out"):
			runner._create_site(tenant, step, self.client)

	def test_archive_marks_the_tenant_archived(self):
		tenant = self._tenant(press_site_id="test-acme-solar.tbogrowthx.com", status="ready")
		runner.archive(tenant.name, self.client)
		self.assertEqual(dict(self.press.calls)["press.api.site.archive"]["force"], 1)
		self.assertEqual(frappe.db.get_value("GX Tenant", tenant.name, "status"), "archived")
