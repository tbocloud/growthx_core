"""Which apps go on a customer's site: product defaults, per-site changes, and the provisioning job
installing exactly that list."""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from growthx_core.provisioning import runner
from growthx_core.provisioning.apps import site_apps
from growthx_core.provisioning.press import FakePressClient, PressError, PressSite

PRODUCT = "gx-apps-test"
EMAIL = "gx-apps-test@example.com"


class RecordingPress(FakePressClient):
	"""Remembers the app list it was asked to install, and reports only those as installed."""

	def __init__(self):
		super().__init__()
		self.created_with = None

	def create_site(self, subdomain, release_group, apps):
		self.created_with = list(apps)
		return PressSite(id=f"fake-{subdomain}", status="active", apps=list(apps))

	def get_site(self, site_id):
		return PressSite(id=site_id, status="active", apps=list(self.created_with or []))


class TestProductApps(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.product = frappe.get_doc(
			{
				"doctype": "GX Product",
				"slug": PRODUCT,
				"product_name": "Apps Test",
				"availability": "available",
				"apps": [
					{"app_name": "growthx_theme"},
					{"app_name": "solar", "repository": "https://github.com/tbocloud/solar_operations.git"},
				],
			}
		).insert()
		user = frappe.get_doc(
			{"doctype": "User", "email": EMAIL, "first_name": "Apps", "send_welcome_email": 0}
		).insert()
		self.account = frappe.get_doc(
			{"doctype": "GX Account", "account_name": "Apps Test", "user": user.name, "contact_email": EMAIL}
		).insert()

	def tearDown(self):
		frappe.db.rollback()

	def _tenant(self, **extra):
		return frappe.get_doc(
			{
				"doctype": "GX Tenant",
				"account": self.account.name,
				"product": PRODUCT,
				"display_name": "Apps Test",
				"subdomain": runner.make_subdomain("Apps Test"),
				**extra,
			}
		).insert()

	def _apps(self, doc):
		return [row.app_name for row in doc.apps]

	# ------------------------------------------------------------ product defaults (admin)

	def test_a_new_product_gets_growthx_theme(self):
		product = frappe.get_doc(
			{
				"doctype": "GX Product",
				"slug": "gx-apps-empty",
				"product_name": "Empty",
				"availability": "coming_soon",
			}
		).insert()
		self.assertEqual(self._apps(product), ["growthx_theme"])

	def test_admin_can_add_and_remove_apps(self):
		self.product.append("apps", {"app_name": "hrms"})
		self.product.save()
		self.assertEqual(self._apps(self.product), ["growthx_theme", "solar", "hrms"])
		self.product.apps = [row for row in self.product.apps if row.app_name != "solar"]
		self.product.save()
		self.assertEqual(self._apps(self.product), ["growthx_theme", "hrms"])

	def test_growthx_theme_cannot_be_removed(self):
		self.product.apps = [row for row in self.product.apps if row.app_name != "growthx_theme"]
		with self.assertRaisesRegex(frappe.ValidationError, "growthx_theme"):
			self.product.save()

	def test_bad_rows_are_refused(self):
		for bad in ("erpnext", "frappe", "solar", "https://github.com/tbocloud/solar_operations.git", ""):
			product = frappe.get_doc("GX Product", PRODUCT)
			product.append("apps", {"app_name": bad})
			with self.assertRaises(frappe.ValidationError, msg=bad):
				product.save()

	def test_names_are_cleaned(self):
		self.product.append("apps", {"app_name": "  HRMS "})
		self.product.save()
		self.assertEqual(self._apps(self.product)[-1], "hrms")

	# ------------------------------------------------------------ per site

	def test_a_new_site_copies_the_products_apps(self):
		tenant = self._tenant()
		self.assertEqual(self._apps(tenant), ["growthx_theme", "solar"])
		self.assertEqual(site_apps(tenant), ["frappe", "erpnext", "growthx_theme", "solar"])

	def test_changing_the_product_later_does_not_change_existing_sites(self):
		tenant = self._tenant()
		self.product.append("apps", {"app_name": "hrms"})
		self.product.save()
		tenant.reload()
		self.assertEqual(self._apps(tenant), ["growthx_theme", "solar"])
		self.assertEqual(self._apps(self._tenant()), ["growthx_theme", "solar", "hrms"])

	def test_one_site_can_get_different_apps(self):
		tenant = self._tenant(
			apps=[{"app_name": "growthx_theme"}, {"app_name": "solar"}, {"app_name": "hrms"}]
		)
		self.assertEqual(site_apps(tenant), ["frappe", "erpnext", "growthx_theme", "solar", "hrms"])
		tenant.apps = [row for row in tenant.apps if row.app_name != "growthx_theme"]
		with self.assertRaisesRegex(frappe.ValidationError, "growthx_theme"):
			tenant.save()

	def test_older_sites_without_a_list_use_the_products(self):
		tenant = self._tenant()
		frappe.db.delete("GX Product App", {"parenttype": "GX Tenant", "parent": tenant.name})
		tenant.reload()
		self.assertEqual(site_apps(tenant), ["frappe", "erpnext", "growthx_theme", "solar"])

	# ------------------------------------------------------------ provisioning installs that list

	def test_press_is_asked_for_exactly_the_sites_apps(self):
		tenant = self._tenant(
			apps=[{"app_name": "growthx_theme"}, {"app_name": "solar"}, {"app_name": "hrms"}]
		)
		press = RecordingPress()
		with patch.object(frappe.db, "commit"):  # the job commits; keep the test's rollback working
			runner._create_site(tenant, None, press)
		self.assertEqual(press.created_with, ["frappe", "erpnext", "growthx_theme", "solar", "hrms"])
		runner._install_apps(tenant, None, press)  # all present: no error

	def test_install_check_fails_when_an_app_is_missing(self):
		tenant = self._tenant()
		press = RecordingPress()
		with patch.object(frappe.db, "commit"):
			runner._create_site(tenant, None, press)
		press.created_with.remove("solar")
		with self.assertRaisesRegex(PressError, "solar"):
			runner._install_apps(tenant, None, press)
