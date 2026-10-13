import os
import tempfile

import frappe
from frappe.tests import IntegrationTestCase

from growthx_core.provisioning import press
from growthx_core.provisioning.press import FakePressClient, LocalPressClient


class TestLocalPress(IntegrationTestCase):
	def setUp(self):
		self.tmp = tempfile.mkdtemp()
		os.makedirs(os.path.join(self.tmp, "sites"))
		self.client = LocalPressClient({"bench_path": self.tmp, "domain": "localhost", "port": 8123})

	def test_site_url_uses_local_domain_and_port(self):
		self.assertEqual(self.client.site_url("noor-solar"), "http://noor-solar.localhost:8123")

	def test_default_site_url_is_https_on_tenant_domain(self):
		self.assertEqual(FakePressClient().site_url("noor-solar"), f"https://noor-solar.{press.tenant_domain()}")

	def test_get_client_selects_local_mode(self):
		frappe.conf["growthx_press"] = {"mode": "local", "bench_path": self.tmp}
		try:
			self.assertIsInstance(press.get_client(), LocalPressClient)
		finally:
			frappe.conf.pop("growthx_press", None)

	def test_site_is_creating_until_status_file_exists(self):
		self.assertEqual(self.client.get_site("x.localhost").status, "creating")

	def test_failed_new_site_reports_broken(self):
		os.makedirs(self.client.state_dir, exist_ok=True)
		with open(self.client._status_file("x.localhost"), "w") as f:
			f.write("1\n")
		self.assertEqual(self.client.get_site("x.localhost").status, "broken")

	def test_find_site_is_none_for_unknown_subdomain(self):
		self.assertIsNone(self.client.find_site("does-not-exist"))

	def test_health_check_false_when_nothing_listens(self):
		client = LocalPressClient({"bench_path": self.tmp, "port": 1})
		self.assertFalse(client.health_check("http://x.localhost:1"))
