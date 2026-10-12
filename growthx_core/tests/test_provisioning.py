import frappe
from frappe.tests import IntegrationTestCase

from growthx_core.provisioning import runner
from growthx_core.provisioning.press import FakePressClient, PressSite


class CountingPress(FakePressClient):
	def __init__(self, fail_step=None):
		super().__init__(fail_step)
		self.created = 0

	def create_site(self, subdomain, release_group, apps):
		self.created += 1
		return super().create_site(subdomain, release_group, apps)


class PendingPress(FakePressClient):
	def get_site(self, site_id):
		return PressSite(id=site_id, status="creating")


class TestProvisioning(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		if not frappe.db.exists("GX Product", "test-vertical"):
			frappe.get_doc(
				{
					"doctype": "GX Product",
					"slug": "test-vertical",
					"product_name": "Test",
					"availability": "available",
				}
			).insert()
		user = (
			frappe.db.get_value("User", {"email": "gx-prov-test@example.com"})
			or frappe.get_doc(
				{
					"doctype": "User",
					"email": "gx-prov-test@example.com",
					"first_name": "Prov",
					"send_welcome_email": 0,
				}
			)
			.insert()
			.name
		)
		self.account = (
			frappe.db.get_value("GX Account", {"user": user})
			or frappe.get_doc(
				{
					"doctype": "GX Account",
					"account_name": "Prov Test",
					"user": user,
					"contact_email": "gx-prov-test@example.com",
				}
			)
			.insert()
			.name
		)

	def _tenant(self):
		tenant = frappe.get_doc(
			{
				"doctype": "GX Tenant",
				"account": self.account,
				"product": "test-vertical",
				"display_name": "Prov Test",
				"subdomain": runner.make_subdomain("Prov Test"),
			}
		)
		runner.init_steps(tenant)
		return tenant.insert().name

	def tearDown(self):
		for name in frappe.get_all("GX Tenant", {"account": self.account}, pluck="name"):
			frappe.delete_doc("GX Tenant", name, force=True)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		# The runner commits, so remove everything this test created from the real catalogue.
		frappe.set_user("Administrator")
		frappe.db.delete("GX Account", {"contact_email": "gx-prov-test@example.com"})
		frappe.delete_doc("User", "gx-prov-test@example.com", force=True, ignore_missing=True)
		frappe.delete_doc("GX Product", "test-vertical", force=True, ignore_missing=True)
		frappe.db.commit()
		super().tearDownClass()

	def test_happy_path(self):
		name = self._tenant()
		runner.run(name, CountingPress())
		tenant = frappe.get_doc("GX Tenant", name)
		self.assertEqual(tenant.status, "ready")
		self.assertTrue(tenant.ready_at)
		self.assertEqual({s.state for s in tenant.steps}, {"done"})

	def test_failure_then_retry_resumes_from_failed_step(self):
		name = self._tenant()
		press = CountingPress(fail_step="configure")
		runner.run(name, press)
		tenant = frappe.get_doc("GX Tenant", name)
		self.assertEqual(tenant.status, "failed")
		self.assertIn("configure", tenant.last_error)
		self.assertNotIn("configure", tenant.status_message)  # customer-safe message
		self.assertEqual([s.state for s in tenant.steps], ["done", "done", "done", "failed", "pending"])

		self.assertTrue(runner.retry(name))
		press.fail_step = None
		runner.run(name, press)
		tenant = frappe.get_doc("GX Tenant", name)
		self.assertEqual(tenant.status, "ready")
		self.assertEqual(tenant.attempts, 2)
		self.assertEqual(press.created, 1, "a retry must not create a second site")

	def test_running_twice_does_not_create_two_sites(self):
		name = self._tenant()
		press = CountingPress()
		runner.run(name, press)
		frappe.db.set_value("GX Tenant", name, "status", "provisioning")
		runner.run(name, press)
		self.assertEqual(press.created, 1)

	def test_waiting_on_press_leaves_step_running(self):
		name = self._tenant()
		runner.run(name, PendingPress())
		tenant = frappe.get_doc("GX Tenant", name)
		self.assertEqual(tenant.status, "provisioning")
		self.assertEqual(tenant.steps[1].state, "running")
		runner.run(name, FakePressClient())  # the scheduler resumes it
		self.assertEqual(frappe.db.get_value("GX Tenant", name, "status"), "ready")

	def test_retry_only_from_failed(self):
		name = self._tenant()
		self.assertFalse(runner.retry(name))

	def test_subdomain_is_unique_and_safe(self):
		self.assertEqual(runner.make_subdomain("Noor Solar & Sons!"), "noor-solar-sons")
		self.assertEqual(runner.make_subdomain("www"), "www-site")
