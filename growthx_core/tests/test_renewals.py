"""Renewals: reminders, grace and suspension for GrowthX and ERPNext Direct subscriptions.
RenewalsCase is the shared base: provisioned (GrowthX) and imported (Direct) customers, a recording
Press client and the billing settings switched on."""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, add_months, now_datetime, nowdate

from growthx_core.provisioning import press, runner

PRODUCT = "test-renewals"
PLAN = "test-renewals-month"
YEARLY = "test-renewals-year"


class RecordingPressClient(press.FakePressClient):
	"""FakePressClient that remembers the lifecycle calls made to it."""

	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)
		self.calls: list[tuple[str, str]] = []

	def suspend_site(self, site_id):
		self.calls.append(("suspend_site", site_id))

	def resume_site(self, site_id):
		self.calls.append(("resume_site", site_id))

	def archive_site(self, site_id):
		self.calls.append(("archive_site", site_id))


class TestSettingsDefaults(IntegrationTestCase):
	def test_settings_defaults_are_safe(self):
		s = frappe.get_single("GX Billing Settings")
		self.assertEqual((s.enabled, s.dry_run, s.send_emails, s.suspend_sites), (0, 1, 0, 0))
		self.assertEqual(s.online_payments, 0)
		self.assertEqual(s.paid_reminder_days, "7,3,1")
		self.assertEqual(s.grace_warning_days, "1,3,5")
		self.assertEqual(s.suspend_after_days, 7)
		self.assertEqual(s.trial_archive_after_days, 30)


class RenewalsCase(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self._product_and_plans()
		self.mail = patch.object(frappe, "sendmail")
		self.sendmail = self.mail.start()
		self.press = RecordingPressClient()
		self.press_patch = patch.object(runner, "get_client", return_value=self.press)
		self.press_patch.start()
		settings = frappe.get_single("GX Billing Settings")
		settings.update({"enabled": 1, "send_emails": 1, "suspend_sites": 1, "dry_run": 0})
		settings.save()

	def tearDown(self):
		self.press_patch.stop()
		self.mail.stop()
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def _product_and_plans(self):
		if not frappe.db.exists("GX Product", PRODUCT):
			frappe.get_doc({"doctype": "GX Product", "slug": PRODUCT, "product_name": "Renewals"}).insert()
		for plan_id, name, interval, price in (
			(PLAN, "Monthly", "month", 999),
			(YEARLY, "Yearly", "year", 9999),
		):
			if not frappe.db.exists("GX Plan", plan_id):
				frappe.get_doc(
					{
						"doctype": "GX Plan",
						"plan_id": plan_id,
						"product": PRODUCT,
						"plan_name": name,
						"billing_interval": interval,
						"price": price,
						"gst_included": 0,
						"gst_rate": 18,
						"trial_days": 7,
						"is_active": 1,
					}
				).insert()

	def _account(self):
		email = f"renewals-{frappe.generate_hash(length=6)}@example.com"
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": "Renewals",
				"user_type": "Website User",
				"send_welcome_email": 0,
			}
		).insert()
		return frappe.get_doc(
			{
				"doctype": "GX Account",
				"account_name": "Renewals Co",
				"user": user.name,
				"contact_email": email,
				"status": "active",
			}
		).insert()

	def _customer(self, origin, status, trial_ends_at=None, current_period_end=None, plan=PLAN):
		account = self._account()
		plan_doc = frappe.get_doc("GX Plan", plan)
		sub = frappe.get_doc(
			{
				"doctype": "GX Subscription",
				"account": account.name,
				"plan": plan_doc.name,
				"product": PRODUCT,
				"status": status,
				"billing_interval": plan_doc.billing_interval,
				"price": plan_doc.price,
				"gst_included": plan_doc.gst_included,
				"gst_rate": plan_doc.gst_rate,
				"trial_ends_at": trial_ends_at,
				"current_period_end": current_period_end,
			}
		).insert()
		site = f"renewals-{frappe.generate_hash(length=6)}.example.com"
		provisioned = origin == "provisioned"
		tenant = frappe.get_doc(
			{
				"doctype": "GX Tenant",
				"account": account.name,
				"product": PRODUCT,
				"origin": origin,
				"status": "ready",
				"display_name": "Renewals Co",
				"subdomain": site.split(".")[0],
				"site_url": f"https://{site}",
				"press_site_id": site if provisioned else None,
				"subscription": sub.name,
			}
		).insert()
		frappe.db.set_value("GX Subscription", sub.name, "tenant", tenant.name)
		sub.reload()
		return account, sub, tenant

	def _gx_customer(self, status, trial_ends_at=None, current_period_end=None):
		"""A GrowthX customer: a provisioned tenant that has a press_site_id."""
		return self._customer("provisioned", status, trial_ends_at, current_period_end)

	def _direct_customer(self, status, trial_ends_at=None, current_period_end=None):
		"""An ERPNext Direct customer: an imported tenant with no press_site_id."""
		return self._customer("imported", status, trial_ends_at, current_period_end)

	def test_helpers_build_both_kinds_of_customer(self):
		_, sub, tenant = self._gx_customer("active", current_period_end=add_months(nowdate(), 1))
		self.assertEqual((tenant.origin, bool(tenant.press_site_id)), ("provisioned", True))
		self.assertEqual(sub.tenant, tenant.name)
		_, _, direct = self._direct_customer("trialing", trial_ends_at=add_days(now_datetime(), 3))
		self.assertEqual((direct.origin, bool(direct.press_site_id)), ("imported", False))
		self.assertEqual(frappe.get_single("GX Billing Settings").suspend_sites, 1)
