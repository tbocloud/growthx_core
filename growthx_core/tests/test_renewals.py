"""Renewals: reminders, grace and suspension for GrowthX and ERPNext Direct subscriptions.
RenewalsCase is the shared base: provisioned (GrowthX) and imported (Direct) customers, a recording
Press client and the billing settings switched on."""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, add_months, getdate, now_datetime, nowdate

from growthx_core.billing import payments
from growthx_core.provisioning import press, runner
from growthx_core.tests import test_admin_direct as direct_tests

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
		checks = {"enabled": 0, "send_emails": 0, "suspend_sites": 0, "dry_run": 1, "online_payments": 0}
		days = {
			"paid_reminder_days": "7,3,1",
			"yearly_extra_reminder_days": "30",
			"grace_warning_days": "1,3,5",
			"trial_reminder_days": "3,1",
		}
		caps = {
			"suspend_after_days": 7,
			"trial_archive_after_days": 30,
			"trial_archive_warning_days": 5,
			"max_suspensions_per_run": 3,
			"max_archives_per_run": 2,
			"sync_max_suspensions_per_run": 1,
		}
		for expected in (checks, days, caps):
			for field, value in expected.items():
				self.assertEqual(s.get(field), value, field)


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


RENEWAL_FIELDS = (
	"due_date",
	"notified_stages",
	"grace_until",
	"final_warning_on",
	"suspended_at",
	"suspended_by",
	"archive_due_on",
)


class TestPaymentPeriods(RenewalsCase):
	def _pay(self, account, paid_on, plan=PLAN, reference=None):
		account.reload()
		plan_doc = frappe.get_doc("GX Plan", plan)
		sub, _details, end = payments.apply_payment(
			account, plan_doc, reference or frappe.generate_hash(length=8), getdate(paid_on), "manual"
		)
		return sub, end

	def test_grace_payment_continues_from_old_end(self):
		account, _sub, _t = self._gx_customer("past_due", current_period_end="2026-12-26")
		_, end = self._pay(account, "2027-01-02")
		self.assertEqual(end, getdate("2027-01-26"))

	def test_renewal_after_suspension_starts_on_payment_day(self):
		account, _sub, _t = self._gx_customer("suspended", current_period_end="2026-12-26")
		_, end = self._pay(account, "2027-01-10")
		self.assertEqual(end, getdate("2027-02-10"))

	def test_starts_later_paid_early_starts_on_start_date(self):
		account, sub, _t = self._direct_customer("trialing", trial_ends_at="2026-12-15")
		frappe.db.set_value("GX Subscription", sub.name, "due_date", "2026-12-15")
		_, end = self._pay(account, "2026-12-10")
		self.assertEqual(end, getdate("2027-01-15"))

	def test_payment_clears_renewal_fields(self):
		account, sub, _t = self._gx_customer("suspended", current_period_end="2026-12-26")
		frappe.db.set_value(
			"GX Subscription",
			sub.name,
			{
				"due_date": "2026-12-26",
				"notified_stages": "7,3",
				"grace_until": "2027-01-02",
				"final_warning_on": "2027-01-01",
				"suspended_at": "2027-01-03 00:00:00",
				"suspended_by": "renewal",
				"archive_due_on": "2027-02-01",
			},
		)
		sub, _ = self._pay(account, "2027-01-10")
		sub.reload()
		for field in RENEWAL_FIELDS:
			self.assertFalse(sub.get(field), field)

	def test_payment_after_suspension_restores_the_site(self):
		account, sub, tenant = self._gx_customer("suspended", current_period_end="2026-12-26")
		frappe.db.set_value("GX Tenant", tenant.name, "status", "suspended")
		self._pay(account, "2027-01-10")
		self.assertIn(("resume_site", tenant.press_site_id), self.press.calls)
		self.assertEqual(frappe.db.get_value("GX Tenant", tenant.name, "status"), "ready")
		log = frappe.get_last_doc("GX Audit Log", {"action": "renewal.resume"})
		self.assertEqual(log.reference_name, sub.name)

	def test_payment_after_suspension_restores_an_imported_site_without_press(self):
		account, _sub, tenant = self._direct_customer("suspended", current_period_end="2026-12-26")
		frappe.db.set_value("GX Tenant", tenant.name, "status", "suspended")
		self._pay(account, "2027-01-10")
		self.assertEqual(frappe.db.get_value("GX Tenant", tenant.name, "status"), "ready")
		self.assertEqual(self.press.calls, [])


class TestFirstPaymentDueDate(direct_tests.DirectCase):
	def _added(self):
		with patch("growthx_core.api.v1.admin_direct.nowdate", return_value="2026-12-01"):
			status, body = self._add()
		self.assertEqual(status, 201, body)
		tenant = self._tenant(body)
		sub = frappe.get_doc("GX Subscription", tenant.subscription)
		self.assertEqual(getdate(sub.due_date), getdate("2026-12-01"))
		return frappe.get_doc("GX Account", tenant.account), sub

	def _pay(self, account, paid_on):
		account.reload()
		_, _, end = payments.apply_payment(
			account, frappe.get_doc("GX Plan", direct_tests.PLAN), "ref-1", getdate(paid_on), "manual"
		)
		return end

	def test_first_payment_starts_at_due_date(self):
		account, _sub = self._added()
		self.assertEqual(self._pay(account, "2026-12-05"), getdate("2027-01-01"))

	def test_first_payment_while_suspended_starts_at_due_date(self):
		account, sub = self._added()
		frappe.db.set_value("GX Subscription", sub.name, "status", "suspended")
		self.assertEqual(self._pay(account, "2026-12-10"), getdate("2027-01-01"))
