"""Renewals: reminders, grace and suspension for GrowthX and ERPNext Direct subscriptions.
RenewalsCase is the shared base: provisioned (GrowthX) and imported (Direct) customers, a recording
Press client and the billing settings switched on."""

import json
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, add_months, cint, get_datetime, getdate, now_datetime, nowdate

from growthx_core import accounts, direct
from growthx_core.api.v1 import _serializers as s
from growthx_core.api.v1 import admin
from growthx_core.api.v1._core import iso, web_url
from growthx_core.billing import payments, renewal_emails, renewals, site_sync
from growthx_core.billing.doctype.gx_billing_settings import gx_billing_settings as billing_settings
from growthx_core.billing.renewal_schedule import parse_sent
from growthx_core.patches.v1 import billing_settings_defaults, direct_due_dates
from growthx_core.provisioning import press, runner
from growthx_core.support import support_inbox
from growthx_core.tests import test_admin_direct as direct_tests

PRODUCT = "test-renewals"
PLAN = "test-renewals-month"
YEARLY = "test-renewals-year"


class RecordingPressClient(press.FakePressClient):
	"""FakePressClient that remembers the lifecycle calls made to it."""

	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)
		self.calls: list[tuple[str, str]] = []
		self.fail_suspend = False
		self.fail_resume = False
		self.fail_sites: set[str] = set()  # sites Press refuses to suspend

	def suspend_site(self, site_id):
		if self.fail_suspend or site_id in self.fail_sites:
			raise press.PressError("Press refused")
		self.calls.append(("suspend_site", site_id))

	def resume_site(self, site_id):
		if self.fail_resume:
			raise press.PressError("Press refused")
		self.calls.append(("resume_site", site_id))

	def archive_site(self, site_id):
		self.calls.append(("archive_site", site_id))


SETTINGS_DEFAULTS = {
	"enabled": 0,
	"send_emails": 0,
	"suspend_sites": 0,
	"dry_run": 1,
	"online_payments": 0,
	"paid_reminder_days": "7,3,1",
	"yearly_extra_reminder_days": "30",
	"grace_warning_days": "1,3,5",
	"trial_reminder_days": "3,1",
	"suspend_after_days": 7,
	"trial_archive_after_days": 30,
	"trial_archive_warning_days": 5,
	"max_suspensions_per_run": 3,
	"max_archives_per_run": 2,
	"sync_max_suspensions_per_run": 1,
}


class TestSettingsDefaults(IntegrationTestCase):
	def test_settings_defaults_are_safe(self):
		s = frappe.get_single("GX Billing Settings")
		for field, value in SETTINGS_DEFAULTS.items():
			self.assertEqual(s.get(field), value, field)
		self.assertFalse(s.started_on)


class TestSettingsRules(IntegrationTestCase):
	"""Defaults survive a Single stored only in part, the patch stores them, and validate refuses values
	that would make the job act too fast."""

	def setUp(self):
		frappe.set_user("Administrator")

	def tearDown(self):
		frappe.db.rollback()

	def test_a_partly_stored_single_still_reads_the_defaults(self):
		# What the Press-side job's first report leaves: a row for the platform list and nothing else.
		frappe.db.delete("Singles", {"doctype": "GX Billing Settings"})
		frappe.db.set_single_value(
			"GX Billing Settings",
			{"platform_sites": "[]", "platform_sites_at": now_datetime()},
			update_modified=False,
		)
		settings = frappe.get_single("GX Billing Settings")
		# Frappe's own reading: nothing (0) for every field not stored
		self.assertEqual((cint(settings.suspend_after_days), settings.dry_run), (0, 0))
		job = renewals.new_job()
		self.assertEqual(
			(job.suspend_after, job.archive_warning_days, job.max_suspensions, job.max_archives), (7, 5, 3, 2)
		)
		self.assertEqual(billing_settings.int_setting("trial_archive_after_days"), 30)
		self.assertEqual(site_sync.get_site_states()["max_suspensions_per_run"], 1)
		# the patch stores every default (a stored value is kept)
		billing_settings_defaults.execute()
		settings = frappe.get_single("GX Billing Settings")
		for field, value in SETTINGS_DEFAULTS.items():
			self.assertEqual(settings.get(field), value, field)
		self.assertEqual(settings.platform_sites, "[]")
		frappe.db.set_single_value("GX Billing Settings", {"dry_run": 0, "suspend_after_days": 9})
		self.assertEqual(billing_settings.fill_defaults(), [])
		settings = frappe.get_single("GX Billing Settings")
		self.assertEqual((settings.dry_run, settings.suspend_after_days), (0, 9))

	def test_validate_refuses_values_that_act_too_fast(self):
		billing_settings.fill_defaults()
		for field, value in (
			("trial_archive_after_days", 0),
			("trial_archive_warning_days", 0),
			("trial_archive_warning_days", -2),
			("max_suspensions_per_run", 0),
			("max_archives_per_run", 0),
			("sync_max_suspensions_per_run", 0),
			("suspend_after_days", 0),
			("suspend_after_days", 5),  # the final warning goes on day 5: suspension must be a day later
			("paid_reminder_days", ""),
			("grace_warning_days", "1,x"),
			("trial_reminder_days", "0"),
			("yearly_extra_reminder_days", " , "),
		):
			settings = frappe.get_single("GX Billing Settings")
			settings.set(field, value)
			with self.assertRaises(frappe.ValidationError, msg=f"{field}={value!r}"):
				settings.save()
		settings = frappe.get_single("GX Billing Settings")
		settings.update(
			{"suspend_after_days": 6, "grace_warning_days": "2, 5", "trial_archive_warning_days": 1}
		)
		settings.save()
		self.assertEqual(frappe.get_single("GX Billing Settings").suspend_after_days, 6)

	def test_the_platform_report_does_not_block_an_open_form(self):
		billing_settings.fill_defaults()
		frappe.get_single("GX Billing Settings").save()
		form = frappe.get_single("GX Billing Settings")  # an admin opens the form...
		site_sync.report_platform_sites(json.dumps([{"site": "a.example.com"}]))  # ...the job reports...
		form.max_archives_per_run = 1
		form.save()  # ...and the admin's save still goes through (no "document modified")...
		self.assertEqual(frappe.get_single("GX Billing Settings").max_archives_per_run, 1)
		# ...without putting back the list it loaded (none): the job's report stays
		self.assertTrue(site_sync.platform_list_known())
		self.assertEqual(site_sync.match_platform_site("a.example.com"), "a.example.com")

	def test_started_on_is_kept_by_a_form_opened_before_it_was_set(self):
		billing_settings.fill_defaults()
		frappe.db.set_single_value("GX Billing Settings", "started_on", None)
		form = frappe.get_single("GX Billing Settings")
		frappe.db.set_single_value(  # the first live run, as the job writes it
			"GX Billing Settings", "started_on", "2026-12-01", update_modified=False
		)
		form.save()
		self.assertEqual(
			getdate(frappe.db.get_single_value("GX Billing Settings", "started_on", cache=False)),
			getdate("2026-12-01"),
		)


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
		# Live since long before this test's subscriptions were made: no first-run floor (TestFirstRun has it).
		settings.update(
			{"enabled": 1, "send_emails": 1, "suspend_sites": 1, "dry_run": 0, "started_on": "2026-01-01"}
		)
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

	def _customer(self, origin, status, trial_ends_at=None, current_period_end=None, plan=PLAN, account=None):
		account = account or self._account()
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
	"archive_warning_on",
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

	def test_grace_payment_on_the_new_end_day_starts_that_day(self):
		# 26 Dec + 1 month is 26 Jan, the day it is paid: that period would be over at once (C1)
		account, _sub, _t = self._gx_customer("past_due", current_period_end="2026-12-26")
		_, end = self._pay(account, "2027-01-26")
		self.assertEqual(end, getdate("2027-02-26"))

	def test_late_first_payment_starts_on_the_payment_day(self):
		# a direct "due now" customer paying more than a month late gets a full month, not an expired one
		account, sub, _t = self._direct_customer("past_due")
		frappe.db.set_value("GX Subscription", sub.name, "due_date", "2026-12-01")
		account.reload()
		_, details, end = payments.apply_payment(
			account, frappe.get_doc("GX Plan", PLAN), "late-1", getdate("2027-01-15"), "manual"
		)
		self.assertEqual(end, getdate("2027-02-15"))
		self.assertIn("period from 2027-01-15 (one from 2026-12-01 would already have ended)", details)

	def test_payment_after_extra_days_starts_on_the_payment_day(self):
		# suspended, then given extra days (past_due again, suspended_at kept): a renewal after a suspension
		account, sub, _t = self._gx_customer("past_due", current_period_end="2026-12-17")
		frappe.db.set_value("GX Subscription", sub.name, "suspended_at", "2026-12-24 10:00:00")
		_, end = self._pay(account, "2026-12-30")
		self.assertEqual(end, getdate("2027-01-30"))

	def test_starts_later_paid_after_its_start_still_starts_on_the_start_date(self):
		account, sub, _t = self._direct_customer("trialing", trial_ends_at="2026-12-15")
		frappe.db.set_value("GX Subscription", sub.name, "due_date", "2026-12-15")
		_, end = self._pay(account, "2026-12-20")  # the job was off: still trialing after the start (M2)
		self.assertEqual(end, getdate("2027-01-15"))

	def test_starts_later_paid_a_month_after_its_start_starts_on_the_payment_day(self):
		account, sub, _t = self._direct_customer("trialing", trial_ends_at="2026-12-15")
		frappe.db.set_value("GX Subscription", sub.name, "due_date", "2026-12-15")
		_, end = self._pay(account, "2027-01-20")
		self.assertEqual(end, getdate("2027-02-20"))

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
				"archive_warning_on": "2027-01-27",
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

	def test_payment_does_not_restore_another_billing_suspended_site(self):
		account, other, other_site = self._gx_customer("suspended", current_period_end="2026-11-01")
		_, paid, paid_site = self._customer(
			"provisioned", "suspended", current_period_end="2026-11-01", account=account
		)
		for sub, tenant in ((other, other_site), (paid, paid_site)):
			frappe.db.set_value("GX Tenant", tenant.name, "status", "suspended")
			frappe.db.set_value("GX Subscription", sub.name, "suspended_by", "billing")
		frappe.db.set_value("GX Account", account.name, "status", "suspended")
		sub, _ = self._pay(account, "2026-12-01")  # the newest subscription of the product: `paid`
		self.assertEqual(sub.name, paid.name)
		self.assertEqual(frappe.db.get_value("GX Tenant", paid_site.name, "status"), "ready")
		self.assertEqual(frappe.db.get_value("GX Tenant", other_site.name, "status"), "suspended")
		self.assertEqual(self.press.calls, [("resume_site", paid_site.press_site_id)])


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


class TestSuspendAndArchive(RenewalsCase):
	def setUp(self):
		super().setUp()
		self.commit = patch.object(frappe.db, "commit")  # runner.archive commits; keep it rollback-able
		self.commit.start()

	def tearDown(self):
		self.commit.stop()
		super().tearDown()

	def _states(self, sub, tenant):
		return (
			frappe.db.get_value("GX Subscription", sub.name, "status"),
			frappe.db.get_value("GX Tenant", tenant.name, "status"),
		)

	def test_suspend_gx_site_calls_press_then_marks_suspended(self):
		_, sub, tenant = self._gx_customer("trialing", trial_ends_at=add_days(now_datetime(), -1))
		self.assertTrue(renewals.suspend_subscription(sub.name))
		self.assertEqual(self.press.calls, [("suspend_site", tenant.press_site_id)])
		self.assertEqual(self._states(sub, tenant), ("suspended", "suspended"))
		sub.reload()
		self.assertTrue(sub.suspended_at)
		self.assertEqual(sub.suspended_by, "billing")
		self.assertEqual(getdate(sub.archive_due_on), getdate(add_days(nowdate(), 30)))
		log = frappe.get_last_doc("GX Audit Log", {"action": "renewal.suspend"})
		self.assertEqual(log.reference_name, sub.name)

	def test_suspend_paid_gx_site_has_no_archive_date(self):
		account, sub, _t = self._gx_customer("past_due", current_period_end=add_days(nowdate(), -8))
		frappe.db.set_value("GX Subscription", sub.name, "last_payment_id", "pay-1")
		self.assertTrue(renewals.suspend_subscription(sub.name, by="admin"))
		sub.reload()
		self.assertFalse(sub.archive_due_on)
		self.assertEqual(sub.suspended_by, "admin")

	def test_suspend_direct_site_never_calls_press(self):
		_, sub, tenant = self._direct_customer("trialing", trial_ends_at=add_days(now_datetime(), -1))
		self.assertTrue(renewals.suspend_subscription(sub.name))
		self.assertEqual(self.press.calls, [])
		self.assertEqual(self._states(sub, tenant), ("suspended", "suspended"))
		self.assertFalse(frappe.db.get_value("GX Subscription", sub.name, "archive_due_on"))

	def test_press_error_leaves_subscription_past_due(self):
		_, sub, tenant = self._gx_customer("past_due", current_period_end=add_days(nowdate(), -8))
		self.press.fail_suspend = True
		errors = frappe.db.count("Error Log")
		self.assertFalse(renewals.suspend_subscription(sub.name))
		self.assertEqual(self._states(sub, tenant), ("past_due", "ready"))
		self.assertFalse(frappe.db.get_value("GX Subscription", sub.name, "suspended_at"))
		self.assertEqual(frappe.db.count("Error Log"), errors + 1)

	def test_archive_refused_for_direct_and_for_once_paid(self):
		_, dsub, dtenant = self._direct_customer("suspended")
		frappe.db.set_value("GX Tenant", dtenant.name, "status", "suspended")
		self.assertFalse(renewals.archive_trial(dsub.name))
		self.assertEqual(self._states(dsub, dtenant), ("suspended", "suspended"))
		account, sub, tenant = self._gx_customer("suspended")
		frappe.db.set_value("GX Tenant", tenant.name, "status", "suspended")
		frappe.db.set_value("GX Subscription", sub.name, "last_payment_id", "pay-1")
		self.assertTrue(not renewals.never_paid(account.name))
		self.assertFalse(renewals.archive_trial(sub.name))
		self.assertEqual(self._states(sub, tenant), ("suspended", "suspended"))
		self.assertEqual(self.press.calls, [])

	def test_archive_never_paid_trial(self):
		account, sub, tenant = self._gx_customer("suspended")
		frappe.db.set_value("GX Tenant", tenant.name, "status", "suspended")
		self.assertTrue(renewals.never_paid(account.name))
		self.assertTrue(renewals.archive_trial(sub.name))
		self.assertEqual(self._states(sub, tenant), ("canceled", "archived"))
		self.assertEqual(self.press.calls, [("archive_site", tenant.press_site_id)])
		log = frappe.get_last_doc("GX Audit Log", {"action": "renewal.archive"})
		self.assertEqual(log.reference_name, sub.name)


NOTHING = {"emails": 0, "past_due": 0, "suspended": 0, "archived": 0, "held": 0}


class TestRenewalJob(RenewalsCase):
	"""The hourly job (renewals.run) with the clock set by patching renewals._now."""

	def setUp(self):
		super().setUp()
		# The job commits per subscription: keep the test rollback-able.
		self.commit = patch.object(frappe.db, "commit")
		self.commit.start()
		# Only this test's subscriptions take part: park every other live one (tearDown rolls it back).
		frappe.db.sql(
			"update `tabGX Subscription` set status = 'canceled' where status in %(live)s",
			{"live": renewals.LIVE},
		)
		self.sent = []  # (subscription, kind, stage, day) of every email that went out
		real_send = renewal_emails.send

		def spy(sub, tenant, account, kind, stage, dates):
			ok = real_send(sub, tenant, account, kind, stage, dates)
			if ok:
				self.sent.append((sub.name, kind, stage, dates["today"]))
			return ok

		self.send_patch = patch.object(renewal_emails, "send", side_effect=spy)
		self.send_patch.start()

	def tearDown(self):
		self.send_patch.stop()
		self.commit.stop()
		super().tearDown()

	def _settings(self, **values):
		settings = frappe.get_single("GX Billing Settings")
		settings.update(values)
		settings.save()

	def _run(self, when):
		with patch.object(renewals, "_now", return_value=get_datetime(when)):
			return renewals.run()

	def _emails(self, sub):
		return [(kind, stage) for name, kind, stage, _day in self.sent if name == sub.name]

	def _status(self, doc):
		return frappe.db.get_value(doc.doctype, doc.name, "status")

	def _would_lines(self, sub):
		return frappe.get_all(
			"GX Audit Log",
			{"reference_name": sub.name, "action": "renewal.would"},
			pluck="details",
			order_by="creation asc",
		)

	def _paused_trial(self, archive_due_on, archive_warning_on=None, trial_ends_at="2026-12-01 15:00:00"):
		"""A never-paid GrowthX trial the job paused, with its archive date."""
		account, sub, tenant = self._gx_customer("suspended", trial_ends_at=trial_ends_at)
		frappe.db.set_value("GX Tenant", tenant.name, "status", "suspended")
		frappe.db.set_value(
			"GX Subscription",
			sub.name,
			{
				"suspended_by": "billing",
				"notified_stages": "-3,-1,0",
				"archive_due_on": archive_due_on,
				"archive_warning_on": archive_warning_on,
			},
		)
		return account, sub, tenant

	def _warned_past_due(self, end="2026-12-17", warned="2026-12-22", direct=False):
		"""Past due with every warning sent, the final one on `warned`: due for suspension two days later."""
		make = self._direct_customer if direct else self._gx_customer
		account, sub, tenant = make("past_due", current_period_end=end)
		frappe.db.set_value(
			"GX Subscription", sub.name, {"notified_stages": "-7,-3,-1,0,1,3,5", "final_warning_on": warned}
		)
		return account, sub, tenant

	def _made_on(self, sub, day):
		"""Set when the subscription was made (the job skips first_due on that day: Ruling 17)."""
		frappe.db.set_value("GX Subscription", sub.name, "creation", f"{day} 09:00:00", update_modified=False)

	def _renewal_audits(self, sub):
		return frappe.get_all(
			"GX Audit Log",
			{"reference_name": sub.name, "action": ("like", "renewal.%")},
			pluck="action",
			order_by="creation asc",
		)

	def test_disabled_does_nothing(self):
		_, sub, tenant = self._gx_customer("past_due", current_period_end="2026-12-10")
		self._settings(enabled=0)
		self.assertEqual(self._run("2027-01-10 10:00:00"), NOTHING)
		self.sendmail.assert_not_called()
		self.assertEqual((self._status(sub), self._status(tenant)), ("past_due", "ready"))
		self.assertEqual(self.press.calls, [])
		self.assertEqual(self._renewal_audits(sub), [])

	def test_dry_run_changes_nothing_but_audits(self):
		_, paid, paid_site = self._gx_customer("active", current_period_end="2026-12-26")
		_, trial, trial_site = self._gx_customer("trialing", trial_ends_at="2026-12-20 12:00:00")
		frappe.db.set_value(
			"GX Subscription", trial.name, {"notified_stages": "-3,-1", "final_warning_on": "2026-12-19"}
		)
		self._settings(dry_run=1)
		counts = self._run("2026-12-26 10:00:00")
		# paid: day 0 email and past_due; trial: trial_ended email and suspension
		self.assertEqual(counts, {**NOTHING, "emails": 2, "past_due": 1, "suspended": 1})
		self.sendmail.assert_not_called()
		self.assertEqual(self.press.calls, [])
		self.assertEqual((self._status(paid), self._status(paid_site)), ("active", "ready"))
		self.assertEqual((self._status(trial), self._status(trial_site)), ("trialing", "ready"))
		self.assertFalse(frappe.db.get_value("GX Subscription", paid.name, "notified_stages"))
		self.assertEqual(frappe.db.get_value("GX Subscription", trial.name, "notified_stages"), "-3,-1")
		self.assertEqual(self._renewal_audits(paid), ["renewal.would", "renewal.would"])
		self.assertEqual(self._renewal_audits(trial), ["renewal.would", "renewal.would"])

	def test_paid_timeline_end_to_end(self):
		_, sub, tenant = self._gx_customer("active", current_period_end="2026-12-26")
		self.sendmail.reset_mock()
		for day, status in (
			("2026-12-19", "active"),
			("2026-12-23", "active"),
			("2026-12-25", "active"),
			("2026-12-26", "past_due"),
			("2026-12-27", "past_due"),
			("2026-12-29", "past_due"),
			("2026-12-31", "past_due"),
			("2027-01-01", "past_due"),
			("2027-01-02", "suspended"),
		):
			for hour in ("09", "10"):  # a second run the same day sends nothing new
				self._run(f"{day} {hour}:00:00")
			self.assertEqual(self._status(sub), status, day)
		self.assertEqual(
			self._emails(sub),
			[
				("paid_reminder", -7),
				("paid_reminder", -3),
				("paid_reminder", -1),
				("paid_expired", 0),
				("paid_warning", 1),
				("paid_warning", 3),
				("paid_final", 5),
				("suspended", None),
			],
		)
		self.assertEqual(self.sendmail.call_count, 8)
		self.assertEqual(
			getdate(frappe.db.get_value("GX Subscription", sub.name, "final_warning_on")),
			getdate("2026-12-31"),
		)
		self.assertEqual(self._status(tenant), "suspended")
		self.assertEqual(self.press.calls, [("suspend_site", tenant.press_site_id)])

	def test_trial_suspends_after_the_last_trial_day(self):
		_, sub, tenant = self._gx_customer("trialing", trial_ends_at="2026-12-01 15:00:00")
		self._run("2026-11-28 10:00:00")
		self._run("2026-11-30 10:00:00")
		self.assertEqual(self._emails(sub), [("trial_reminder", -3), ("trial_reminder", -1)])
		self._run("2026-12-01 23:30:00")
		self.assertEqual((self._status(sub), self._status(tenant)), ("trialing", "ready"))
		self.assertEqual(self.press.calls, [])
		self._run("2026-12-02 00:10:00")
		self.assertEqual((self._status(sub), self._status(tenant)), ("suspended", "suspended"))
		self.assertEqual(self.press.calls, [("suspend_site", tenant.press_site_id)])
		self.assertEqual(self._emails(sub)[-1], ("trial_ended", 0))
		self.assertEqual(
			getdate(frappe.db.get_value("GX Subscription", sub.name, "archive_due_on")), getdate("2027-01-01")
		)

	def test_no_suspension_without_final_warning(self):
		_, sub, tenant = self._gx_customer("past_due", current_period_end="2026-12-17")
		self._settings(send_emails=0)
		self._run("2026-12-26 10:00:00")  # day +9: past the suspension day, but no final warning went out
		self.assertEqual((self._status(sub), self._status(tenant)), ("past_due", "ready"))
		self.assertEqual((self.sent, self.press.calls), ([], []))
		# Emails back on: the final warning goes now, and the site gets two more days.
		self._settings(send_emails=1)
		self._run("2026-12-27 10:00:00")
		self.assertEqual(self._emails(sub), [("paid_final", 5)])
		self._run("2026-12-28 10:00:00")
		self.assertEqual(self._status(sub), "past_due")
		self._run("2026-12-29 10:00:00")
		self.assertEqual(self._status(sub), "suspended")

	def test_first_run_with_many_overdue_respects_cap_and_sends_one_email_each(self):
		self._settings(max_suspensions_per_run=3)
		subs = [self._gx_customer("trialing", trial_ends_at="2026-12-06 12:00:00")[1] for _ in range(5)]
		# First run, 20 days after the trials ended: only the last reminder each, nobody paused without it.
		self.assertEqual(self._run("2026-12-26 10:00:00"), {**NOTHING, "emails": 5})
		for sub in subs:
			self.assertEqual(self._emails(sub), [("trial_reminder", -1)])
		self.assertEqual(self.press.calls, [])
		# Next day: three are paused (each told first), two wait for the next run.
		self.assertEqual(
			self._run("2026-12-27 10:00:00"), {**NOTHING, "emails": 3, "suspended": 3, "held": 2}
		)
		self.assertEqual(self._run("2026-12-28 10:00:00"), {**NOTHING, "emails": 2, "suspended": 2})
		for sub in subs:
			self.assertEqual(self._status(sub), "suspended")
			self.assertEqual(self._emails(sub), [("trial_reminder", -1), ("trial_ended", 0)])
		self.assertEqual(len(self.press.calls), 5)

	def test_direct_first_due_timeline(self):
		account, sub, tenant = self._direct_customer("past_due")
		self._made_on(sub, "2026-11-01")
		frappe.db.set_value("GX Subscription", sub.name, "due_date", "2026-12-01")
		accounts.mark_no_password(account.user)
		self.sendmail.reset_mock()
		for day in range(1, 9):
			self._run(f"2026-12-{day:02d} 10:00:00")
			self.assertEqual(self._status(sub), "suspended" if day == 8 else "past_due", day)
		self.assertEqual(
			[(kind, stage, str(day)) for name, kind, stage, day in self.sent if name == sub.name],
			[
				("first_due", 0, "2026-12-01"),
				("paid_warning", 1, "2026-12-02"),
				("paid_warning", 3, "2026-12-04"),
				("paid_final", 5, "2026-12-06"),
				("suspended", None, "2026-12-08"),
			],
		)
		first = self.sendmail.call_args_list[0].kwargs["message"]
		self.assertIn("/reset-password?key=", first)
		self.assertEqual(self._status(tenant), "suspended")
		self.assertEqual(self.press.calls, [])

	def test_direct_starts_later_uses_paid_timeline(self):
		_, sub, tenant = self._direct_customer("trialing", trial_ends_at="2026-12-15 00:00:00")
		self._made_on(sub, "2026-11-01")
		frappe.db.set_value("GX Subscription", sub.name, "due_date", "2026-12-15")
		self._run("2026-12-08 10:00:00")
		self.assertEqual(self._emails(sub), [("paid_reminder", -7)])
		self._run("2026-12-14 10:00:00")
		self.assertEqual(self._status(sub), "trialing")
		self._run("2026-12-15 10:00:00")
		self.assertEqual(self._status(sub), "past_due")
		self._run(
			"2026-12-16 10:00:00"
		)  # a trial would be paused today; a direct customer gets the grace days
		self.assertEqual((self._status(sub), self._status(tenant)), ("past_due", "ready"))
		self.assertEqual(
			self._emails(sub),
			[("paid_reminder", -7), ("paid_reminder", -1), ("first_due", 0), ("paid_warning", 1)],
		)
		self.assertEqual(self.press.calls, [])

	def test_never_paid_trial_archived_after_30_days_with_warning(self):
		_, sub, tenant = self._gx_customer("trialing", trial_ends_at="2026-12-01 15:00:00")
		self._run("2026-11-30 10:00:00")
		self._run("2026-12-02 10:00:00")
		self.assertEqual(self._status(sub), "suspended")
		self.assertEqual(
			getdate(frappe.db.get_value("GX Subscription", sub.name, "archive_due_on")), getdate("2027-01-01")
		)
		self._run("2026-12-26 10:00:00")
		self.assertEqual(self._emails(sub), [("trial_reminder", -1), ("trial_ended", 0)])
		self._run("2026-12-27 10:00:00")
		self.assertEqual(self._emails(sub)[-1], ("trial_archive_warning", None))
		self.assertEqual(
			getdate(frappe.db.get_value("GX Subscription", sub.name, "archive_warning_on")),
			getdate("2026-12-27"),
		)
		self._run("2026-12-31 10:00:00")
		self.assertEqual((self._status(sub), self._status(tenant)), ("suspended", "suspended"))
		self._run("2027-01-01 10:00:00")
		self.assertEqual((self._status(sub), self._status(tenant)), ("canceled", "archived"))
		self.assertEqual(
			self.press.calls, [("suspend_site", tenant.press_site_id), ("archive_site", tenant.press_site_id)]
		)
		self.assertEqual(
			self._emails(sub),
			[
				("trial_reminder", -1),
				("trial_ended", 0),
				("trial_archive_warning", None),
				("trial_archived", None),
			],
		)

	def test_late_archive_warning_gives_the_full_notice(self):
		_, sub, tenant = self._paused_trial(archive_due_on="2027-01-01")
		self.sendmail.reset_mock()
		self._run("2027-01-03 10:00:00")  # the job was off on the warning day: warn now, archive 5 days on
		self.assertEqual(self._emails(sub), [("trial_archive_warning", None)])
		self.assertIn("8 Jan 2027", self.sendmail.call_args.kwargs["message"])
		row = frappe.db.get_value(
			"GX Subscription", sub.name, ["archive_due_on", "archive_warning_on"], as_dict=True
		)
		self.assertEqual(
			(getdate(row.archive_due_on), getdate(row.archive_warning_on)),
			(getdate("2027-01-01"), getdate("2027-01-03")),
		)
		self._run("2027-01-07 10:00:00")
		self.assertEqual(self._status(tenant), "suspended")
		self._run("2027-01-08 10:00:00")
		self.assertEqual(self._status(tenant), "archived")

	def test_lifetime_paid_is_ignored(self):
		_, sub, tenant = self._gx_customer("active")
		frappe.db.set_value(
			"GX Subscription",
			sub.name,
			{"billing_interval": "one_time", "last_payment_id": "pay-1", "trial_ends_at": "2026-11-01"},
		)
		for when in ("2026-12-01 10:00:00", "2027-06-01 10:00:00"):
			self.assertEqual(self._run(when), NOTHING)
		self.assertEqual((self._status(sub), self._status(tenant)), ("active", "ready"))
		self.assertEqual((self.sent, self.press.calls), ([], []))
		self.assertEqual(self._renewal_audits(sub), [])

	def test_admin_suspended_is_left_alone(self):
		account, sub, tenant = self._gx_customer("past_due", current_period_end="2026-12-10")
		# customer_action suspend: the account and its sites are suspended, its subscriptions untouched
		frappe.db.set_value("GX Account", account.name, "status", "suspended")
		frappe.db.set_value("GX Tenant", tenant.name, "status", "suspended")
		frappe.db.set_value("GX Subscription", sub.name, "final_warning_on", "2026-12-15")
		self.assertEqual(self._run("2026-12-26 10:00:00"), NOTHING)
		self.assertEqual((self._status(sub), self._status(tenant)), ("past_due", "suspended"))
		self.assertEqual((self.sent, self.press.calls), ([], []))
		self.assertEqual(self._renewal_audits(sub), [])

	def test_cancel_at_period_end_notifies_cancels_suspends_never_archives(self):
		_, sub, tenant = self._gx_customer("active", current_period_end="2026-12-26")
		frappe.db.set_value("GX Subscription", sub.name, "cancel_at_period_end", 1)
		self._run("2026-12-19 10:00:00")
		self.assertEqual(self.sent, [])  # no reminders for a plan the customer cancelled
		self._run("2026-12-26 10:00:00")
		self.assertEqual(self._emails(sub), [("canceled", 0)])
		# a cancelled (soon churned) account can't pay: no payment wording, ask GrowthX instead
		notice = self.sendmail.call_args.kwargs["message"]
		for promise in ("comes back", "Pay now", "bank transfer"):
			self.assertNotIn(promise, notice)
		self.assertIn(support_inbox(), notice)
		self.assertEqual((self._status(sub), self._status(tenant)), ("canceled", "suspended"))
		self.assertFalse(frappe.db.get_value("GX Subscription", sub.name, "archive_due_on"))
		self.assertIn("renewal.cancel", self._renewal_audits(sub))
		self._run("2027-02-10 10:00:00")
		self.assertEqual(self._status(tenant), "suspended")
		self.assertEqual(self.press.calls, [("suspend_site", tenant.press_site_id)])
		self.assertEqual(self._emails(sub), [("canceled", 0)])

	def test_cancel_at_period_end_suspends_only_after_the_notice(self):
		_, quiet, quiet_site = self._gx_customer("active", current_period_end="2026-12-26")
		frappe.db.set_value("GX Subscription", quiet.name, "cancel_at_period_end", 1)
		self._settings(send_emails=0)
		self._run("2026-12-26 10:00:00")
		# no notice could go out: canceled, the site keeps running, and the log says what was held back
		self.assertEqual((self._status(quiet), self._status(quiet_site)), ("canceled", "ready"))
		self.assertEqual(self.press.calls, [])
		self.assertIn("no notice was sent", " ".join(self._would_lines(quiet)))
		_, held, held_site = self._gx_customer("active", current_period_end="2026-12-27")
		frappe.db.set_value("GX Subscription", held.name, "cancel_at_period_end", 1)
		self._settings(send_emails=1, suspend_sites=0)
		self._run("2026-12-27 10:00:00")
		# suspend_sites off: held as it is (no notice, no cancel) until suspension is live (Ruling 15)
		self.assertEqual(self._emails(held), [])
		self.assertEqual((self._status(held), self._status(held_site)), ("active", "ready"))
		self.assertIn("suspend_sites is off", " ".join(self._would_lines(held)))
		self.assertEqual(self.press.calls, [])
		# switched on: the notice, the cancel and the suspension go together
		self._settings(suspend_sites=1)
		self._run("2027-01-03 10:00:00")
		self.assertEqual(self._emails(held), [("canceled", 0)])
		self.assertEqual((self._status(held), self._status(held_site)), ("canceled", "suspended"))

	def test_one_failing_subscription_does_not_stop_the_run(self):
		_, bad, _t = self._gx_customer("active", current_period_end="2026-12-26")
		_, good, _t2 = self._gx_customer("active", current_period_end="2026-12-26")
		real_audit = renewals.audit

		def audit(action, doctype=None, name=None, details=None, actor=None):
			if action == "renewal.past_due" and name == bad.name:
				raise RuntimeError("audit failed")
			return real_audit(action, doctype, name, details, actor)

		with patch.object(renewals, "audit", side_effect=audit):
			self._run("2026-12-26 10:00:00")
		self.assertEqual(self._status(bad), "active")  # its status change was rolled back...
		self.assertEqual(frappe.db.get_value("GX Subscription", bad.name, "notified_stages"), "-7,-3,-1,0")
		self.assertTrue(
			frappe.db.exists("Error Log", {"method": f"GrowthX: renewal job failed for {bad.name}"})
		)
		self.assertEqual(self._status(good), "past_due")  # ...and the next one was still done

	def test_failed_email_is_not_recorded_and_is_retried(self):
		_, sub, _t = self._gx_customer("past_due", current_period_end="2026-12-21")
		self.sendmail.side_effect = Exception("no outgoing email account")
		self._run("2026-12-26 10:00:00")
		self.assertFalse(frappe.db.get_value("GX Subscription", sub.name, "notified_stages"))
		self.assertFalse(frappe.db.get_value("GX Subscription", sub.name, "final_warning_on"))
		self.sendmail.side_effect = None
		self._run("2026-12-26 11:00:00")
		self.assertEqual(self._emails(sub), [("paid_final", 5)])
		self.assertEqual(
			getdate(frappe.db.get_value("GX Subscription", sub.name, "final_warning_on")),
			getdate("2026-12-26"),
		)

	def test_blank_day_settings_fall_back_to_defaults(self):
		# (validate refuses these; stored straight, as a Single saved before they were set would have them)
		frappe.db.set_single_value(
			"GX Billing Settings",
			{"paid_reminder_days": "", "grace_warning_days": " , ", "trial_reminder_days": "none"},
		)
		_, paid, _t = self._gx_customer("past_due", current_period_end="2026-12-21")
		_, trial, _t2 = self._gx_customer("trialing", trial_ends_at="2026-12-29 12:00:00")
		self._run("2026-12-26 10:00:00")
		self.assertEqual(self._emails(paid), [("paid_final", 5)])
		self.assertEqual(self._emails(trial), [("trial_reminder", -3)])

	def test_archive_is_finished_when_the_site_was_already_archived(self):
		_, sub, tenant = self._gx_customer("suspended", trial_ends_at="2026-11-20 12:00:00")
		frappe.db.set_value(
			"GX Subscription", sub.name, {"suspended_by": "billing", "archive_due_on": "2026-12-21"}
		)
		frappe.db.set_value(
			"GX Tenant", tenant.name, "status", "archived"
		)  # runner.archive committed, then a crash
		self._run("2026-12-26 10:00:00")
		self.assertEqual(self._status(sub), "canceled")
		self.assertEqual(self.press.calls, [])

	def test_emails_name_the_site_plan_price_and_billing_page(self):
		_, sub, tenant = self._gx_customer("active", current_period_end="2026-12-26")
		self._run("2026-12-19 10:00:00")
		offline = self.sendmail.call_args.kwargs
		self.assertIs(offline["now"], False)  # queued: Email Queue sends it
		self.assertEqual(
			(offline["reference_doctype"], offline["reference_name"]), ("GX Subscription", sub.name)
		)
		self.assertEqual(
			offline["recipients"], [frappe.db.get_value("GX Account", sub.account, "contact_email")]
		)
		plan = frappe.get_doc("GX Plan", PLAN)
		for text in (
			"Renewals Co",
			tenant.site_url.removeprefix("https://"),
			plan.plan_name,
			payments.plan_price(plan),
			"26 Dec 2026",
			web_url("/customer/billing"),
			"bank transfer",
		):
			self.assertIn(text, offline["message"])
		self.assertNotIn("Pay now", offline["message"])
		# online payment on, but the plan is "+ GST": PayU can't take it, so still a bank transfer
		self._settings(online_payments=1)
		self._run("2026-12-23 10:00:00")
		plus_gst = self.sendmail.call_args.kwargs["message"]
		self.assertIn("bank transfer", plus_gst)
		self.assertNotIn("Pay now", plus_gst)
		frappe.db.set_value("GX Plan", PLAN, "gst_included", 1)
		self._run("2026-12-25 10:00:00")
		online = self.sendmail.call_args.kwargs["message"]
		self.assertIn("Pay now", online)
		self.assertNotIn("bank transfer", online)

	# ---------------------------------------------------------------- fix round 1

	def test_locks_the_account_before_the_subscription(self):
		_, sub, _t = self._warned_past_due()
		locks = []
		real_get_value = frappe.db.get_value

		def get_value(*args, **kwargs):
			if kwargs.get("for_update"):
				locks.append(args[0])
			return real_get_value(*args, **kwargs)

		with patch.object(frappe.db, "get_value", side_effect=get_value):
			self._run("2026-12-26 10:00:00")
		self.assertEqual(self._status(sub), "suspended")
		# first read, again before suspending, and again after the commit: always the payments' order
		self.assertGreaterEqual(len(locks), 6)
		self.assertEqual(locks, ["GX Account", "GX Subscription"] * (len(locks) // 2))

	def test_press_suspended_but_core_not_updated_is_logged(self):
		_, sub, tenant = self._warned_past_due()
		with (
			patch.object(renewals, "sync_account_status", side_effect=RuntimeError("deadlock")),
			patch.object(frappe, "log_error", wraps=frappe.log_error) as log_error,
		):
			self._run("2026-12-26 10:00:00")
		self.assertEqual(self.press.calls, [("suspend_site", tenant.press_site_id)])
		self.assertEqual((self._status(sub), self._status(tenant)), ("past_due", "ready"))
		titles = [c.kwargs.get("title") for c in log_error.call_args_list]
		self.assertIn(f"GrowthX: Press suspended {tenant.press_site_id} but Core was not updated", titles)
		self.assertIn(f"GrowthX: renewal job failed for {sub.name}", titles)

	def test_dry_run_projects_one_would_suspend_line_a_day(self):
		_, sub, _t = self._gx_customer("past_due", current_period_end="2026-12-17")
		self._settings(dry_run=1, suspend_sites=0)
		self._run("2026-12-26 10:00:00")
		self._run("2026-12-26 11:00:00")  # the same day: no new lines
		self._run("2026-12-27 10:00:00")
		lines = self._would_lines(sub)
		# suspend_sites off: the day it would be if suspension went live that day (a fresh final notice + 2)
		self.assertEqual(
			[line for line in lines if "suspend" in line],
			[
				"2026-12-26: would suspend on 2026-12-28 (suspend_sites is off)",
				"2026-12-27: would suspend on 2026-12-29 (suspend_sites is off)",
			],
		)
		# plus the day-0 email it would send each day (no final warning while suspension is off)
		self.assertEqual(
			[line for line in lines if "email" in line],
			[
				"2026-12-26: would email paid_expired (day +0)",
				"2026-12-27: would email paid_expired (day +0)",
			],
		)
		self.assertEqual((self.sent, self.press.calls), ([], []))

	def test_suspend_sites_off_logs_would_suspend(self):
		_, sub, tenant = self._warned_past_due()
		self._settings(suspend_sites=0)
		self._run("2026-12-26 10:00:00")
		self._run("2026-12-26 11:00:00")
		self.assertEqual((self._status(sub), self._status(tenant)), ("past_due", "ready"))
		self.assertEqual(self.press.calls, [])
		self.assertEqual(
			self._would_lines(sub),
			[
				"2026-12-26: would suspend on 2026-12-24 (unpaid after the final warning; suspend_sites is off)"
			],
		)

	def test_plan_change_on_a_paused_trial_keeps_the_archive_warning(self):
		from growthx_core.api.v1 import admin

		_, sub, tenant = self._paused_trial(archive_due_on="2027-01-01")
		admin._move_subscription_to_plan(
			frappe.get_doc("GX Subscription", sub.name), frappe.get_doc("GX Plan", YEARLY)
		)
		self.assertTrue(frappe.db.get_value("GX Subscription", sub.name, "current_period_end"))
		self._run("2026-12-27 10:00:00")
		self.assertEqual(self._emails(sub), [("trial_archive_warning", None)])
		self.assertEqual(
			getdate(frappe.db.get_value("GX Subscription", sub.name, "archive_warning_on")),
			getdate("2026-12-27"),
		)
		self._run("2027-01-01 10:00:00")
		self.assertEqual(self._status(tenant), "archived")

	def test_extend_trial_restarts_the_reminders(self):
		from growthx_core.api.v1 import admin

		account, sub, _t = self._gx_customer("trialing", trial_ends_at="2026-12-05 15:00:00")
		self._run("2026-12-02 10:00:00")
		self._run("2026-12-04 10:00:00")
		admin._extend_trial(frappe.get_doc("GX Account", account.name), 7)
		row = frappe.db.get_value(
			"GX Subscription",
			sub.name,
			["trial_ends_at", "notified_stages", "final_warning_on"],
			as_dict=True,
		)
		self.assertEqual((row.notified_stages, row.final_warning_on), (None, None))
		end = getdate(row.trial_ends_at)
		self._run(f"{add_days(end, -3)} 10:00:00")
		self._run(f"{add_days(end, -1)} 10:00:00")
		self._run(f"{end} 23:00:00")
		self.assertEqual(self._status(sub), "trialing")
		self.assertEqual(
			self._emails(sub),
			[("trial_reminder", -3), ("trial_reminder", -1), ("trial_reminder", -3), ("trial_reminder", -1)],
		)

	def test_stale_final_warning_is_forgotten(self):
		_, sub, tenant = self._gx_customer("trialing", trial_ends_at="2026-12-10 12:00:00")
		# left from an earlier trial end (an admin moved it without clearing them)
		frappe.db.set_value(
			"GX Subscription", sub.name, {"notified_stages": "-3,-1", "final_warning_on": "2026-11-20"}
		)
		self._run("2026-12-07 10:00:00")
		self.assertEqual(self._emails(sub), [("trial_reminder", -3)])
		self._run("2026-12-11 10:00:00")  # the last reminder goes late, and the site gets a day after it
		self.assertEqual(self._emails(sub), [("trial_reminder", -3), ("trial_reminder", -1)])
		self.assertEqual((self._status(sub), self.press.calls), ("trialing", []))
		self._run("2026-12-12 10:00:00")
		self.assertEqual((self._status(sub), self._status(tenant)), ("suspended", "suspended"))

	def test_paid_and_direct_suspensions_never_get_an_archive_date(self):
		_, paid, paid_site = self._warned_past_due()  # never paid, but not a trial
		_, direct, direct_site = self._warned_past_due(direct=True)
		self._run("2026-12-26 10:00:00")
		for sub, tenant in ((paid, paid_site), (direct, direct_site)):
			self.assertEqual((self._status(sub), self._status(tenant)), ("suspended", "suspended"))
			self.assertFalse(frappe.db.get_value("GX Subscription", sub.name, "archive_due_on"))
		self._run("2027-03-01 10:00:00")
		self.assertEqual((self._status(paid_site), self._status(direct_site)), ("suspended", "suspended"))
		self.assertEqual(self.press.calls, [("suspend_site", paid_site.press_site_id)])

	def test_record_is_skipped_when_the_subscription_changed_during_the_send(self):
		_, sub, _t = self._gx_customer("active", current_period_end="2026-12-26")

		def paid_meanwhile(sub_doc, tenant, account, kind, stage, dates):
			frappe.db.set_value(
				"GX Subscription",
				sub_doc.name,
				{"last_payment_id": "paid-during-send", "current_period_end": "2027-01-26"},
				update_modified=False,
			)
			return True

		failed = f"GrowthX: renewal job failed for {sub.name}"
		logged_before = frappe.db.count("Error Log", {"method": failed})
		with (
			patch.object(renewal_emails, "send", side_effect=paid_meanwhile),
			patch.object(frappe, "log_error", wraps=frappe.log_error) as log_error,
		):
			self._run("2026-12-26 10:00:00")
		row = frappe.db.get_value("GX Subscription", sub.name, ["status", "notified_stages"], as_dict=True)
		self.assertEqual((row.status, row.notified_stages), ("active", None))
		# A change under the job is not an error: nothing logged by this run.
		titles = [c.kwargs.get("title") for c in log_error.call_args_list]
		trace = frappe.db.get_value("Error Log", {"method": failed}, "error", order_by="creation desc")
		self.assertNotIn(failed, titles, trace)
		self.assertEqual(frappe.db.count("Error Log", {"method": failed}), logged_before)

	def test_legacy_past_due_without_due_date_counts_from_creation(self):
		_, sub, _t = self._direct_customer("past_due")
		frappe.db.set_value(
			"GX Subscription", sub.name, "creation", "2026-11-30 09:00:00", update_modified=False
		)
		self._run("2026-11-30 10:00:00")  # day 0 is the day it was made (no first_due on that day: Ruling 17)
		self._run("2026-12-01 10:00:00")
		self.assertEqual(self._emails(sub), [("paid_warning", 1)])
		self.assertEqual(frappe.db.get_value("GX Subscription", sub.name, "notified_stages"), "-7,-3,-1,0,1")

	def test_archive_cap(self):
		self._settings(max_archives_per_run=2)
		paused = [
			self._paused_trial(archive_due_on="2026-12-20", archive_warning_on="2026-12-15")[1]
			for _ in range(3)
		]
		counts = self._run("2026-12-26 10:00:00")
		self.assertEqual(counts, {**NOTHING, "archived": 2, "held": 1, "emails": 2})
		self.assertEqual([self._status(sub) for sub in paused], ["canceled", "canceled", "suspended"])
		self.assertEqual(self._run("2026-12-27 10:00:00")["archived"], 1)

	def test_failed_press_suspend_is_not_counted_against_the_cap(self):
		self._settings(max_suspensions_per_run=1)
		_, refused, refused_site = self._warned_past_due()
		_, ok, _t = self._warned_past_due()
		self.press.fail_sites = {refused_site.press_site_id}
		counts = self._run("2026-12-26 10:00:00")
		self.assertEqual((counts["suspended"], counts["held"]), (1, 0))
		self.assertEqual((self._status(refused), self._status(ok)), ("past_due", "suspended"))

	def test_failed_trial_ended_email_blocks_the_suspension(self):
		_, sub, tenant = self._gx_customer("trialing", trial_ends_at="2026-12-20 12:00:00")
		frappe.db.set_value(
			"GX Subscription", sub.name, {"notified_stages": "-3,-1", "final_warning_on": "2026-12-19"}
		)
		self.sendmail.side_effect = Exception("no outgoing email account")
		self._run("2026-12-26 10:00:00")
		self.assertEqual(
			(self._status(sub), self._status(tenant), self.press.calls), ("trialing", "ready", [])
		)
		self.sendmail.side_effect = None
		self._run("2026-12-26 11:00:00")
		self.assertEqual(self._emails(sub), [("trial_ended", 0)])
		self.assertEqual(self._status(sub), "suspended")

	# ---------------------------------------------------------------- final review fixes

	def _messages(self):
		return [c.kwargs["message"] for c in self.sendmail.call_args_list]

	def test_a_resume_press_refused_at_payment_is_retried_by_the_job(self):
		account, sub, tenant = self._warned_past_due()
		self._run("2026-12-26 10:00:00")
		self.assertEqual((self._status(sub), self._status(tenant)), ("suspended", "suspended"))
		self.press.fail_resume = True
		payments.apply_payment(
			frappe.get_doc("GX Account", account.name),
			frappe.get_doc("GX Plan", PLAN),
			"paid-late",
			getdate("2026-12-27"),
			"manual",
		)
		# paid, but Press refused: the site is still off, the subscription is marked for a retry (Ruling 19),
		# and Admin → Renewals says so
		self.assertEqual((self._status(sub), self._status(tenant)), ("active", "suspended"))
		self.assertEqual(frappe.db.get_value("GX Account", account.name, "status"), "active")
		self.assertEqual(frappe.db.get_value("GX Subscription", sub.name, "resume_pending"), 1)
		status, body = direct_tests._call(admin.renewals)
		self.assertEqual(status, 200, body)
		self.assertEqual([i["subscriptionId"] for i in body["resumePending"]], [sub.name])
		self.assertNotIn(sub.name, [i["subscriptionId"] for i in body["suspended"]])
		# the next run tries again; refused once more, it stays marked for the run after
		self._run("2026-12-27 10:00:00")
		self.assertEqual(self._status(tenant), "suspended")
		self.assertEqual(frappe.db.get_value("GX Subscription", sub.name, "resume_pending"), 1)
		self.press.fail_resume = False
		self._run("2026-12-27 11:00:00")
		self.assertEqual(self._status(tenant), "ready")
		self.assertEqual(self.press.calls[-1], ("resume_site", tenant.press_site_id))
		log = frappe.get_last_doc("GX Audit Log", {"action": "renewal.resume", "reference_name": sub.name})
		self.assertIn("by the renewal job", log.details)
		self.assertEqual(self._status(sub), "active")
		self.assertEqual(frappe.db.get_value("GX Subscription", sub.name, "resume_pending"), 0)
		# back and no longer marked: later runs don't ask Press again
		self._run("2026-12-27 12:00:00")
		self.assertEqual(self.press.calls.count(("resume_site", tenant.press_site_id)), 1)

	def test_live_without_suspension_promises_no_dates(self):
		self._settings(suspend_sites=0)
		_, paid, paid_site = self._gx_customer("active", current_period_end="2026-12-26")
		_, trial, trial_site = self._gx_customer("trialing", trial_ends_at="2026-12-29 12:00:00")
		_, cancelled, cancelled_site = self._gx_customer("active", current_period_end="2026-12-26")
		frappe.db.set_value("GX Subscription", cancelled.name, "cancel_at_period_end", 1)
		self.sendmail.reset_mock()
		for day in ("2026-12-19", "2026-12-23", "2026-12-25", "2026-12-26", "2026-12-27", "2026-12-28"):
			self._run(f"{day} 10:00:00")
		for day in ("2026-12-29", "2026-12-31", "2027-01-02", "2027-01-05"):
			self._run(f"{day} 10:00:00")
		# reminders and the day-0 email only: no warnings after day 0, no final notice
		self.assertEqual(
			self._emails(paid),
			[("paid_reminder", -7), ("paid_reminder", -3), ("paid_reminder", -1), ("paid_expired", 0)],
		)
		self.assertEqual(self._emails(trial), [("trial_reminder", -3), ("trial_reminder", -1)])
		self.assertEqual(self._emails(cancelled), [])  # held as it is
		for message in self._messages():
			for promise in ("suspended", "paused", "deleted"):
				self.assertNotIn(promise, message)
		self.assertEqual(
			[self._status(x) for x in (paid, trial, cancelled)], ["past_due", "trialing", "active"]
		)
		self.assertEqual([self._status(x) for x in (paid_site, trial_site, cancelled_site)], ["ready"] * 3)
		self.assertEqual(self.press.calls, [])
		for sub in (paid, trial):
			self.assertFalse(frappe.db.get_value("GX Subscription", sub.name, "final_warning_on"))
		# what would happen is logged, and the Billing page promises no date
		self.assertIn(
			"2027-01-05: would suspend on 2027-01-07 (suspend_sites is off)", self._would_lines(paid)
		)
		self.assertIn("suspend_sites is off", " ".join(self._would_lines(trial)))
		self.assertIn("would cancel and suspend", " ".join(self._would_lines(cancelled)))
		for sub, stage in ((paid, "expired"), (trial, "trial_ended"), (cancelled, "expired")):
			info = renewals.renewal_info(
				frappe.get_doc("GX Subscription", sub.name), today=getdate("2027-01-05")
			)
			self.assertEqual((info["stage"], info["suspendsAt"], info["daysLeft"]), (stage, None, 0))

	def test_switching_suspension_on_sends_a_fresh_final_notice_first(self):
		self._settings(suspend_sites=0)  # rollout step 1
		_, paid, paid_site = self._gx_customer("past_due", current_period_end="2026-12-10")
		_, trial, trial_site = self._gx_customer("trialing", trial_ends_at="2026-12-12 12:00:00")
		self._run("2026-12-11 10:00:00")
		self._run("2026-12-20 10:00:00")
		self.assertEqual(
			(self._emails(paid), self._emails(trial)), ([("paid_expired", 0)], [("trial_reminder", -1)])
		)
		self._settings(suspend_sites=1)  # step 2, weeks after day 0
		self._run("2026-12-21 10:00:00")
		# a final notice now, naming a day at least two days on; nobody suspended yet
		self.assertEqual(self._emails(paid), [("paid_expired", 0), ("paid_final", 5)])
		self.assertEqual(self._emails(trial), [("trial_reminder", -1), ("trial_reminder", -1)])
		self.assertTrue(any("suspended on <b>23 Dec 2026</b>" in m for m in self._messages()))
		self.assertTrue(any("from <b>22 Dec 2026</b> it is paused" in m for m in self._messages()))
		for sub in (paid, trial):
			self.assertEqual(
				getdate(frappe.db.get_value("GX Subscription", sub.name, "final_warning_on")),
				getdate("2026-12-21"),
			)
		self.assertEqual((self._status(paid_site), self._status(trial_site)), ("ready", "ready"))
		self._run("2026-12-22 10:00:00")
		self.assertEqual((self._status(paid_site), self._status(trial_site)), ("ready", "suspended"))
		self._run("2026-12-23 10:00:00")
		self.assertEqual(self._status(paid_site), "suspended")
		self.assertEqual(self._emails(paid)[-1], ("suspended", None))

	def test_first_live_run_gives_existing_customers_a_week(self):
		frappe.db.set_single_value("GX Billing Settings", "started_on", None)
		_, old, _t = self._gx_customer("past_due", current_period_end="2026-11-01")  # long expired
		_, trial, _t2 = self._gx_customer("trialing", trial_ends_at="2026-12-02 12:00:00")
		for sub in (old, trial):
			frappe.db.set_value(
				"GX Subscription", sub.name, "creation", "2026-11-20 10:00:00", update_modified=False
			)
		# a dry run doesn't start the clock: it shows what going live that day would do
		self._settings(dry_run=1)
		self._run("2026-11-28 10:00:00")
		self.assertFalse(frappe.db.get_single_value("GX Billing Settings", "started_on", cache=False))
		self.assertIn("2026-11-28: would email paid_reminder (day -7)", self._would_lines(old))
		self._settings(dry_run=0)
		self._run("2026-12-01 10:00:00")  # the first live run
		self.assertEqual(
			getdate(frappe.db.get_single_value("GX Billing Settings", "started_on", cache=False)),
			getdate("2026-12-01"),
		)
		self.assertTrue(frappe.db.exists("GX Audit Log", {"action": "renewal.started"}))
		# day 0 is 8 Dec for both: the whole timeline from today
		self.assertEqual(self._emails(old), [("paid_reminder", -7)])
		for day in ("2026-12-05", "2026-12-07", "2026-12-08"):
			self._run(f"{day} 10:00:00")
		self.assertEqual(
			self._emails(old),
			[("paid_reminder", -7), ("paid_reminder", -3), ("paid_reminder", -1), ("paid_expired", 0)],
		)
		self.assertEqual(self._emails(trial), [("trial_reminder", -3), ("trial_reminder", -1)])
		self.assertEqual(self._status(trial), "trialing")
		info = renewals.renewal_info(frappe.get_doc("GX Subscription", old.name), today=getdate("2026-12-08"))
		self.assertEqual(info["expiresAt"], iso("2026-12-08"))
		# a subscription made after the first live run keeps its own day 0
		_, new, _t3 = self._gx_customer("active", current_period_end="2026-12-04")
		frappe.db.set_value(
			"GX Subscription", new.name, "creation", "2026-12-02 10:00:00", update_modified=False
		)
		info = renewals.renewal_info(frappe.get_doc("GX Subscription", new.name), today=getdate("2026-12-08"))
		self.assertEqual(info["expiresAt"], iso("2026-12-04"))

	def test_invite_then_job_the_same_day_keeps_the_invite_link(self):
		account, sub, _t = self._direct_customer("past_due")
		accounts.mark_no_password(account.user)
		frappe.db.set_value(  # added "due now" today
			"GX Subscription",
			sub.name,
			{"due_date": nowdate(), "creation": now_datetime()},
			update_modified=False,
		)
		key = direct.issue_password_link(account.user).split("key=")[1]  # the invite's link
		stored = frappe.db.get_value("User", account.user, "reset_password_key")
		self._run(now_datetime())
		self.assertEqual(self._emails(sub), [])  # no first_due: the invite already asked to pay
		self.assertIn(0, parse_sent(frappe.db.get_value("GX Subscription", sub.name, "notified_stages")))
		self.assertEqual(frappe.db.get_value("User", account.user, "reset_password_key"), stored)
		self.assertEqual(frappe.utils.sha256_hash(key), stored)  # the invite's link still works

	def test_first_due_never_replaces_a_set_password_link_under_a_day_old(self):
		account, sub, _t = self._direct_customer("past_due")
		accounts.mark_no_password(account.user)
		self._made_on(sub, "2026-11-01")
		frappe.db.set_value("GX Subscription", sub.name, "due_date", "2026-12-01")
		direct.issue_password_link(account.user)  # e.g. a payment link the admin sent an hour ago
		stored = frappe.db.get_value("User", account.user, "reset_password_key")
		self.sendmail.reset_mock()
		self._run("2026-12-01 10:00:00")
		self.assertEqual(self._emails(sub), [("first_due", 0)])
		message = self.sendmail.call_args.kwargs["message"]
		self.assertNotIn("reset-password?key=", message)
		self.assertIn(web_url("/forgot-password"), message)
		self.assertEqual(frappe.db.get_value("User", account.user, "reset_password_key"), stored)

	def test_extend_trial_moves_a_direct_billing_start(self):
		account, sub, _t = self._direct_customer("trialing", trial_ends_at="2027-03-15 00:00:00")
		self._made_on(sub, "2026-11-01")
		frappe.db.set_value("GX Subscription", sub.name, "due_date", "2027-03-15")
		details = admin._extend_trial(frappe.get_doc("GX Account", account.name), 7)
		row = frappe.db.get_value("GX Subscription", sub.name, ["trial_ends_at", "due_date"], as_dict=True)
		self.assertEqual((getdate(row.trial_ends_at), getdate(row.due_date)), (getdate("2027-03-22"),) * 2)
		self.assertIn("billing starts 2027-03-22 (was 2027-03-15)", details)
		self._run("2027-03-15 10:00:00")
		self.assertEqual(self._status(sub), "trialing")  # not due yet: it starts a week later
		self._run("2027-03-22 10:00:00")
		self.assertEqual(self._status(sub), "past_due")
		self.assertEqual(self._emails(sub)[-1], ("first_due", 0))


class TestRenewalInfo(RenewalsCase):
	"""renewals.renewal_info: the stage, days left and dates the customer's Billing page renders."""

	def _info(self, sub, today="2026-12-26"):
		return renewals.renewal_info(sub, today=getdate(today))

	def _expect(self, info, stage, days, expires, suspends=None, archives=None, can_pay=True, advance=False):
		self.assertEqual(
			info,
			{
				"stage": stage,
				"daysLeft": days,
				"expiresAt": iso(expires),
				"suspendsAt": iso(suspends),
				"archivesAt": iso(archives),
				"canPay": can_pay,
				"payInAdvance": advance,
			},
		)

	def test_renewal_info_stages(self):
		# paid, outside the reminder window: nothing to pay yet
		_, sub, _t = self._gx_customer("active", current_period_end="2027-01-20")
		self._expect(self._info(sub), "paid", 25, "2027-01-20", can_pay=False, advance=True)
		# inside it (7 days): renew now, and the suspension date it would lead to
		_, sub, _t = self._gx_customer("active", current_period_end="2026-12-29")
		self._expect(self._info(sub), "due_soon", 3, "2026-12-29", suspends="2027-01-05")
		# a yearly plan's 30-day reminder is an email only: due_soon from 7 days, like a monthly one (M1)
		_, sub, _t = self._customer("provisioned", "active", current_period_end="2027-01-20", plan=YEARLY)
		self._expect(self._info(sub), "paid", 25, "2027-01-20", can_pay=False, advance=True)
		self._expect(self._info(sub, "2027-01-13"), "due_soon", 7, "2027-01-20", suspends="2027-01-27")
		# first payment still owed: due 26 Dec, final warning planned for 31 Dec, suspended 2 Jan
		_, sub, _t = self._gx_customer("past_due")
		frappe.db.set_value("GX Subscription", sub.name, "due_date", "2026-12-26")
		sub.reload()
		self._expect(self._info(sub), "first_due", 7, "2026-12-26", suspends="2027-01-02")
		# expired 2 days ago; an admin's extra days push the suspension out
		_, sub, _t = self._gx_customer("past_due", current_period_end="2026-12-24")
		self._expect(self._info(sub), "expired", 5, "2026-12-24", suspends="2026-12-31")
		frappe.db.set_value("GX Subscription", sub.name, "grace_until", "2027-01-10")
		sub.reload()
		self._expect(self._info(sub), "expired", 15, "2026-12-24", suspends="2027-01-10")
		# the job has not run yet on day 0: still active, but already expired
		_, sub, _t = self._gx_customer("active", current_period_end="2026-12-26")
		self._expect(self._info(sub), "expired", 7, "2026-12-26", suspends="2027-01-02")
		# a GrowthX trial: ends 29 Dec; paused on 30 Dec, a day after the last reminder
		_, sub, _t = self._gx_customer("trialing", trial_ends_at="2026-12-29 15:00:00")
		self._expect(self._info(sub), "trial", 3, "2026-12-29", suspends="2026-12-30")
		# ... past its last day but not paused yet
		self._expect(self._info(sub, "2026-12-30"), "trial_ended", 1, "2026-12-29", suspends="2026-12-31")
		# suspended for non-payment
		_, sub, tenant = self._gx_customer("suspended", current_period_end="2026-12-10")
		frappe.db.set_value("GX Tenant", tenant.name, "status", "suspended")
		self._expect(self._info(sub), "suspended", 0, "2026-12-10")
		# a never-paid trial the job paused: the day it will be deleted
		_, sub, tenant = self._gx_customer("suspended", trial_ends_at="2026-12-20 12:00:00")
		frappe.db.set_value("GX Tenant", tenant.name, "status", "suspended")
		frappe.db.set_value(
			"GX Subscription", sub.name, {"suspended_by": "billing", "archive_due_on": "2027-01-05"}
		)
		sub.reload()
		self._expect(self._info(sub), "trial_ended", 10, "2026-12-20", archives="2027-01-05")
		# once the deletion warning is out, the notice period runs from it
		frappe.db.set_value("GX Subscription", sub.name, "archive_warning_on", "2026-12-30")
		sub.reload()
		self._expect(self._info(sub, "2026-12-31"), "trial_ended", 5, "2026-12-20", archives="2027-01-05")
		# a direct customer whose billing starts later follows the paid timeline
		_, sub, tenant = self._direct_customer("trialing", trial_ends_at="2026-12-29")
		self._expect(self._info(sub), "due_soon", 3, "2026-12-29", suspends="2027-01-05")

	def test_renewal_info_cancelled_plan_is_suspended_on_day_zero(self):
		# The job suspends a plan cancelled at period end on day 0, with no grace
		_, sub, _t = self._gx_customer("active", current_period_end="2026-12-29")
		frappe.db.set_value("GX Subscription", sub.name, "cancel_at_period_end", 1)
		sub.reload()
		self._expect(self._info(sub), "due_soon", 3, "2026-12-29", suspends="2026-12-29")
		_, sub, _t = self._gx_customer("active", current_period_end="2027-01-20")
		frappe.db.set_value("GX Subscription", sub.name, "cancel_at_period_end", 1)
		sub.reload()
		self._expect(
			self._info(sub), "paid", 25, "2027-01-20", suspends="2027-01-20", can_pay=False, advance=True
		)
		_, sub, _t = self._gx_customer("past_due", current_period_end="2026-12-24")
		frappe.db.set_value("GX Subscription", sub.name, "cancel_at_period_end", 1)
		sub.reload()
		self._expect(self._info(sub), "expired", 0, "2026-12-24", suspends="2026-12-24")
		# with suspend_sites off nothing suspends it
		settings = frappe.get_single("GX Billing Settings")
		settings.suspend_sites = 0
		settings.save()
		self._expect(self._info(sub), "expired", 0, "2026-12-24")

	def test_renewal_info_is_none_where_nothing_renews(self):
		_, sub, _t = self._gx_customer("active", current_period_end="2027-01-20")
		frappe.db.set_value("GX Subscription", sub.name, "billing_interval", "one_time")
		sub.reload()
		self.assertIsNone(self._info(sub))
		_, sub, _t = self._gx_customer("canceled", current_period_end="2026-12-24")
		self.assertIsNone(self._info(sub))

	def test_subscription_serializer_carries_renewal(self):
		_, sub, _t = self._gx_customer("active", current_period_end="2026-12-29")
		with patch.object(renewals, "_now", return_value=get_datetime("2026-12-26 10:00:00")):
			data = s.subscription(sub)
		self.assertEqual((data["renewal"]["stage"], data["renewal"]["daysLeft"]), ("due_soon", 3))


class TestRenewalAdminApi(RenewalsCase):
	def setUp(self):
		super().setUp()
		self.commit = patch.object(frappe.db, "commit")  # the job commits per subscription
		self.commit.start()
		# Only this test's subscriptions take part: park every other live one (tearDown rolls it back).
		frappe.db.sql(
			"update `tabGX Subscription` set status = 'canceled' where status in %(live)s",
			{"live": renewals.LIVE},
		)

		self.sent = []  # (subscription, kind, stage) of every email that went out
		real_send = renewal_emails.send

		def spy(sub, tenant, account, kind, stage, dates):
			ok = real_send(sub, tenant, account, kind, stage, dates)
			if ok:
				self.sent.append((sub.name, kind, stage))
			return ok

		self.send_patch = patch.object(renewal_emails, "send", side_effect=spy)
		self.send_patch.start()

	def tearDown(self):
		self.send_patch.stop()
		self.commit.stop()
		super().tearDown()

	def _at(self, when, fn=None, data=None, **kwargs):
		with patch.object(renewals, "_now", return_value=get_datetime(when)):
			if fn is None:
				return renewals.run()
			return direct_tests._call(fn, data, **kwargs)

	def _suspend(self, sub, tenant, **extra):
		frappe.db.set_value("GX Tenant", tenant.name, "status", "suspended")
		frappe.db.set_value(
			"GX Subscription",
			sub.name,
			{
				"status": "suspended",
				"suspended_by": "billing",
				"suspended_at": "2026-12-24 10:00:00",
				**extra,
			},
		)

	def _suspended_past_due(self, direct=False):
		make = self._direct_customer if direct else self._gx_customer
		account, sub, tenant = make("past_due", current_period_end="2026-12-17")
		frappe.db.set_value(
			"GX Subscription",
			sub.name,
			{"notified_stages": "-7,-3,-1,0,1,3,5", "final_warning_on": "2026-12-22"},
		)
		self._suspend(sub, tenant)
		return account, sub, tenant

	def _row(self, sub):
		return frappe.db.get_value("GX Subscription", sub.name, "*", as_dict=True)

	def test_extend_grace_restores_and_delays_suspension(self):
		account, sub, tenant = self._suspended_past_due()
		status, body = self._at(
			"2026-12-26 10:00:00", admin.extend_grace, {"id": sub.name, "until": "2027-01-05"}
		)
		self.assertEqual(status, 200, body)
		self.assertEqual((body["subscriptionId"], body["stage"]), (sub.name, "expired"))
		self.assertEqual(body["suspendsAt"], iso("2027-01-05"))
		row = self._row(sub)
		self.assertEqual((row.status, getdate(row.grace_until)), ("past_due", getdate("2027-01-05")))
		self.assertFalse(row.suspended_by)
		self.assertTrue(row.suspended_at)  # kept: paid later, it is a renewal after a suspension (Ruling 11)
		self.assertEqual(frappe.db.get_value("GX Tenant", tenant.name, "status"), "ready")
		self.assertIn(("resume_site", tenant.press_site_id), self.press.calls)
		self.assertEqual(frappe.db.get_value("GX Account", account.name, "status"), "active")
		log = frappe.get_last_doc("GX Audit Log", {"action": "renewal.extend_grace"})
		self.assertEqual(log.reference_name, sub.name)
		# the job leaves it alone until the new date, without repeating the warnings already sent
		self._at("2026-12-27 10:00:00")
		self._at("2027-01-04 10:00:00")
		self.assertEqual(frappe.db.get_value("GX Tenant", tenant.name, "status"), "ready")
		self.assertEqual(
			self.press.calls.count(("suspend_site", tenant.press_site_id)), 0, "suspended before the new date"
		)
		self.assertEqual(self.sent, [], "no reminder repeats before the new date")
		self._at("2027-01-05 10:00:00")
		self.assertEqual(self._row(sub).status, "suspended")
		self.assertEqual(self.sent, [(sub.name, "suspended", None)])
		self.assertEqual(frappe.db.get_value("GX Tenant", tenant.name, "status"), "suspended")

	def test_extend_grace_on_a_paused_trial_restores_the_trial_and_is_kept(self):
		_, sub, tenant = self._gx_customer("trialing", trial_ends_at="2026-12-20 12:00:00")
		self._suspend(
			sub,
			tenant,
			notified_stages="-3,-1,0",
			final_warning_on="2026-12-19",
			archive_due_on="2027-01-19",
			archive_warning_on="2027-01-14",
		)
		status, body = self._at(
			"2026-12-26 10:00:00", admin.extend_grace, {"id": sub.name, "until": "2026-12-30"}
		)
		self.assertEqual(status, 200, body)
		row = self._row(sub)
		self.assertEqual(row.status, "trialing")
		self.assertFalse(row.archive_due_on or row.archive_warning_on or row.suspended_by)
		self.assertEqual(frappe.db.get_value("GX Tenant", tenant.name, "status"), "ready")
		self.assertEqual((body["stage"], body["suspendsAt"]), ("trial_ended", iso("2026-12-30")))
		self.assertEqual(
			self._row(sub).notified_stages, "-3,-1"
		)  # the reminders stay; only trial_ended is due
		self._at("2026-12-29 10:00:00")
		self.assertEqual((self._row(sub).status, self.sent), ("trialing", []))
		self._at("2026-12-30 10:00:00")
		self.assertEqual(self._row(sub).status, "suspended")
		self.assertEqual(self.sent, [(sub.name, "trial_ended", 0)])

	def test_extend_grace_on_a_running_subscription_only_moves_the_date(self):
		_, sub, tenant = self._gx_customer("past_due", current_period_end="2026-12-24")
		status, body = self._at(
			"2026-12-26 10:00:00", admin.extend_grace, {"id": sub.name, "until": "2027-01-10"}
		)
		self.assertEqual(status, 200, body)
		self.assertEqual((self._row(sub).status, self.press.calls), ("past_due", []))
		self.assertEqual(body["suspendsAt"], iso("2027-01-10"))

	def test_extend_grace_keeps_an_admin_suspension(self):
		_, sub, tenant = self._gx_customer("suspended", current_period_end="2026-12-24")
		frappe.db.set_value("GX Subscription", sub.name, "suspended_by", "admin")
		frappe.db.set_value("GX Tenant", tenant.name, "status", "suspended")
		status, _body = self._at(
			"2026-12-26 10:00:00", admin.extend_grace, {"id": sub.name, "until": "2027-01-10"}
		)
		self.assertEqual(status, 200)
		self.assertEqual(self._row(sub).status, "suspended")
		self.assertEqual(frappe.db.get_value("GX Tenant", tenant.name, "status"), "suspended")

	def test_extend_grace_refused_when_press_cannot_resume(self):
		_, sub, tenant = self._suspended_past_due()
		with patch.object(renewals, "resume_subscription_site") as resume:  # it logs and leaves it suspended
			status, body = self._at(
				"2026-12-26 10:00:00", admin.extend_grace, {"id": sub.name, "until": "2027-01-05"}
			)
		resume.assert_called_once()
		self.assertEqual((status, body["error"]["code"]), (502, "press_unavailable"))

	def test_extend_grace_validation(self):
		_, sub, _t = self._suspended_past_due()
		for until, field in (
			("2026-12-25", "until"),  # past
			("2026-12-26", "until"),  # today is not in the future
			("2027-02-25", "until"),  # 61 days
			("5 Jan", "until"),
			(None, "until"),
		):
			status, body = self._at(
				"2026-12-26 10:00:00", admin.extend_grace, {"id": sub.name, "until": until}
			)
			self.assertEqual(status, 422, until)
			self.assertIn(field, body["error"]["fields"])
		status, _ = self._at(
			"2026-12-26 10:00:00", admin.extend_grace, {"id": sub.name, "until": "2027-02-24"}
		)
		self.assertEqual(status, 200)  # 60 days is the limit
		status, _ = self._at("2026-12-26 10:00:00", admin.extend_grace, {"id": "nope", "until": "2027-01-05"})
		self.assertEqual(status, 404)
		account, sub2, _t = self._gx_customer("past_due", current_period_end="2026-12-24")
		frappe.set_user(account.user)
		status, _ = self._at(
			"2026-12-26 10:00:00",
			admin.extend_grace,
			{"id": sub2.name, "until": "2027-01-05"},
			auth_patched=False,
		)
		self.assertEqual(status, 403)

	def test_extend_grace_refused_for_a_canceled_subscription(self):
		_, sub, _t = self._gx_customer("canceled", current_period_end="2026-12-24")
		status, body = self._at(
			"2026-12-26 10:00:00", admin.extend_grace, {"id": sub.name, "until": "2027-01-05"}
		)
		self.assertEqual((status, body["error"]["code"]), (409, "invalid_transition"))

	def test_reactivate_refused_for_billing_suspension(self):
		account, sub, tenant = self._suspended_past_due()
		frappe.db.set_value("GX Account", account.name, "status", "suspended")
		status, body = self._at(
			"2026-12-26 10:00:00",
			admin.update_customer_status,
			{"id": account.name, "action": "reactivate"},
		)
		self.assertEqual((status, body["error"]["code"]), (409, "billing_suspended"))
		self.assertIn("Give extra days", body["error"]["message"])
		self.assertEqual(frappe.db.get_value("GX Tenant", tenant.name, "status"), "suspended")
		# an admin's own suspension can still be reactivated here
		frappe.db.set_value("GX Subscription", sub.name, "suspended_by", "admin")
		status, _ = self._at(
			"2026-12-26 10:00:00",
			admin.update_customer_status,
			{"id": account.name, "action": "reactivate"},
		)
		self.assertEqual(status, 200)

	def test_admin_renewals_lists(self):
		_, expiring, t1 = self._gx_customer("active", current_period_end="2026-12-30")
		_, yearly, t2 = self._customer("provisioned", "active", current_period_end="2027-01-20", plan=YEARLY)
		self._gx_customer("active", current_period_end="2027-01-20")  # not yet
		_, past, t3 = self._gx_customer("past_due", current_period_end="2026-12-24")
		_, susp, t4 = self._suspended_past_due()
		self._gx_customer("trialing", trial_ends_at="2026-12-29 15:00:00")  # in no list
		_, direct, t5 = self._direct_customer("active", current_period_end="2027-01-20")
		for tenant in (t1, t2, t3, t4, t5):
			frappe.db.set_value("GX Tenant", tenant.name, "platform_site", f"{tenant.name}.press")
		frappe.db.set_value(
			"GX Tenant", t5.name, {"sync_status": "Not found", "sync_message": "No such site on Press"}
		)
		_, unmatched, t6 = self._direct_customer(
			"active", current_period_end="2027-01-20"
		)  # no platform_site
		_, _healthy, t7 = self._direct_customer("active", current_period_end="2027-01-20")
		frappe.db.set_value("GX Tenant", t7.name, {"platform_site": "healthy.press", "sync_status": "ok"})
		status, body = self._at("2026-12-26 10:00:00", admin.renewals)
		self.assertEqual(status, 200, body)
		ids = {key: [i["subscriptionId"] for i in body[key]] for key in body}
		self.assertEqual(ids["expiring"], [expiring.name, yearly.name])  # a yearly plan counts 30 days
		self.assertEqual(
			(ids["pastDue"], ids["suspended"]),
			([past.name], [susp.name]),
			"trial and healthy ones are in no list",
		)
		self.assertEqual(set(ids["syncProblems"]), {direct.name, unmatched.name})
		item = body["pastDue"][0]
		self.assertEqual(
			set(item),
			{
				"subscriptionId",
				"accountId",
				"accountName",
				"site",
				"origin",
				"planName",
				"stage",
				"expiresAt",
				"suspendsAt",
				"archivesAt",
				"syncStatus",
				"syncMessage",
			},
		)
		self.assertEqual(
			(item["stage"], item["origin"], item["planName"], item["suspendsAt"]),
			("expired", "provisioned", "Monthly", iso("2026-12-31")),
		)
		self.assertEqual(body["suspended"][0]["stage"], "suspended")
		problem = {i["subscriptionId"]: i for i in body["syncProblems"]}[direct.name]
		self.assertEqual(
			(problem["syncStatus"], problem["syncMessage"]), ("Not found", "No such site on Press")
		)

	# ---------------------------------------------------------------- final review fixes

	def test_terminate_refuses_a_suspended_imported_site(self):
		_, sub, tenant = self._suspended_past_due(direct=True)
		status, body = self._at("2026-12-26 10:00:00", admin.terminate_tenant, {"id": tenant.name})
		self.assertEqual((status, body["error"]["code"]), (409, "site_suspended"))
		self.assertIn("Reactivate or give extra days first", body["error"]["message"])
		self.assertEqual(
			(self._row(sub).status, frappe.db.get_value("GX Tenant", tenant.name, "status")),
			("suspended", "suspended"),
		)
		# running again (Give extra days), it can be terminated
		self._at("2026-12-26 10:00:00", admin.extend_grace, {"id": sub.name, "until": "2027-01-05"})
		status, body = self._at("2026-12-26 10:00:00", admin.terminate_tenant, {"id": tenant.name})
		self.assertEqual((status, body["status"]), (200, "archived"), body)
		self.assertEqual(self.press.calls, [])

	def test_terminate_waits_until_the_platform_has_the_site_running_again(self):
		# Ruling 20: running again in Core, but still off on Press until the sync job's next run
		_, sub, tenant = self._suspended_past_due(direct=True)
		host = site_sync.host_of(tenant.site_url)
		frappe.db.set_value("GX Tenant", tenant.name, "platform_site", host)

		def report(action, platform_status):
			site_sync.report_site_states(
				json.dumps([{"site": host, "platform_status": platform_status, "action": action}])
			)

		report("suspended", "Suspended")  # the sync job switched it off on Press
		status, body = self._at(
			"2026-12-26 10:00:00", admin.extend_grace, {"id": sub.name, "until": "2027-01-05"}
		)
		self.assertEqual(status, 200, body)
		self.assertEqual(frappe.db.get_value("GX Tenant", tenant.name, "status"), "ready")
		for action, platform_status in (("suspended", "Suspended"), ("", "SUSPENDED"), ("held", "Suspended")):
			report(action, platform_status)
			status, body = self._at("2026-12-26 10:01:00", admin.terminate_tenant, {"id": tenant.name})
			self.assertEqual((status, body["error"]["code"]), (409, "site_suspended"), (action, body))
			self.assertIn("still switched off on the hosting platform", body["error"]["message"])
		self.assertEqual(frappe.db.get_value("GX Tenant", tenant.name, "status"), "ready")
		self.assertEqual(self._row(sub).status, "past_due")
		# the sync job has switched it back on: it can be terminated
		report("reactivated", "Active")
		status, body = self._at("2026-12-26 10:05:00", admin.terminate_tenant, {"id": tenant.name})
		self.assertEqual((status, body["status"]), (200, "archived"), body)
		self.assertEqual(self.press.calls, [])

	def test_an_admin_suspension_survives_subscription_saves_and_the_job(self):
		# N1 / Ruling 19: a subscription save recomputes the account's status (the account turns active
		# again), but the job only retries a resume Press refused after a payment: the admin's suspension stands
		account, sub, tenant = self._gx_customer("active", current_period_end="2027-01-20")
		status, body = self._at(
			"2026-12-26 09:00:00",
			admin.update_customer_status,
			{"id": account.name, "action": "suspend", "reason": "Chargeback"},
		)
		self.assertEqual(status, 200, body)
		self.assertEqual(frappe.db.get_value("GX Tenant", tenant.name, "status"), "suspended")
		status, body = self._at(
			"2026-12-26 10:00:00", admin.extend_grace, {"id": sub.name, "until": "2027-01-05"}
		)
		self.assertEqual(status, 200, body)
		self.assertEqual(frappe.db.get_value("GX Account", account.name, "status"), "active")  # flipped
		self._at("2026-12-26 11:00:00")
		status, body = self._at("2026-12-26 11:30:00", admin.assign_plan, {"id": tenant.name, "plan": YEARLY})
		self.assertEqual(status, 200, body)
		self._at("2026-12-26 12:00:00")
		self.assertEqual(frappe.db.get_value("GX Tenant", tenant.name, "status"), "suspended")
		self.assertEqual(self.press.calls, [("suspend_site", tenant.press_site_id)])  # never resumed
		self.assertFalse(self._row(sub).resume_pending)
		self.assertFalse(
			frappe.db.exists("GX Audit Log", {"action": "renewal.resume", "reference_name": sub.name})
		)
		status, body = self._at("2026-12-26 12:00:00", admin.renewals)
		self.assertEqual((status, body["resumePending"]), (200, []), body)

	def test_an_admin_suspension_stops_a_pending_resume(self):
		account, sub, tenant = self._suspended_past_due()
		self.press.fail_resume = True
		payments.apply_payment(
			frappe.get_doc("GX Account", account.name),
			frappe.get_doc("GX Plan", PLAN),
			"pay-1",
			getdate("2026-12-26"),
			"manual",
		)
		self.assertEqual(self._row(sub).resume_pending, 1)  # paid, but Press refused
		self.press.fail_resume = False
		# the admin suspends the customer before the job's next run: the site stays off
		status, body = self._at(
			"2026-12-26 10:00:00",
			admin.update_customer_status,
			{"id": account.name, "action": "suspend", "reason": "Chargeback"},
		)
		self.assertEqual(status, 200, body)
		self.assertEqual(self._row(sub).resume_pending, 0)
		self._at("2026-12-26 11:00:00")
		self.assertEqual(frappe.db.get_value("GX Tenant", tenant.name, "status"), "suspended")
		self.assertEqual(self.press.calls, [])
		# the admin's Reactivate brings it back
		status, body = self._at(
			"2026-12-26 12:00:00", admin.update_customer_status, {"id": account.name, "action": "reactivate"}
		)
		self.assertEqual(status, 200, body)
		self.assertEqual(frappe.db.get_value("GX Tenant", tenant.name, "status"), "ready")
		self.assertEqual(self.press.calls, [("resume_site", tenant.press_site_id)])

	def test_the_job_takes_the_account_lock_before_its_first_read(self):
		"""MariaDB fixes a transaction's snapshot (REPEATABLE READ) at its first plain read. The job waits for
		the account lock while an admin's Suspend holds it: had it read anything first, it would not see that
		Suspend once it got the lock, and could retry the resume the Suspend had cancelled. So after each
		commit, the job's first read is the lock itself."""
		_, sub, site = self._gx_customer("active", current_period_end="2027-01-20")
		frappe.db.set_value("GX Tenant", site.name, "status", "suspended")
		frappe.db.set_value("GX Subscription", sub.name, "resume_pending", 1)  # Press refused a resume
		log = []
		real_sql = frappe.db.sql

		def logged(query, *args, **kwargs):
			log.append(str(query).strip().lower())
			return real_sql(query, *args, **kwargs)

		frappe.db.commit.side_effect = lambda *args, **kwargs: log.append("commit")
		with patch.object(frappe.db, "sql", side_effect=logged):
			self._at("2026-12-26 10:00:00")
		locks = [i for i, q in enumerate(log) if "for update" in q and "`tabgx account`" in q]
		self.assertGreater(len(locks), 1)  # the first lock, and the one again after the resume's commit
		for lock in locks:
			begin = max(i for i in range(lock) if log[i] == "commit")
			self.assertEqual([q for q in log[begin + 1 : lock] if q.startswith("select")], [])
		self.assertEqual(self.press.calls, [("resume_site", site.press_site_id)])  # the retry still works

	def _locking_reads(self, fn):
		"""(doctype, the row's name or the filter fields, for_update) of each GX Tenant / GX Subscription read
		`fn` makes."""
		calls = []
		real = {
			"get_all": frappe.get_all,
			"get_value": frappe.db.get_value,
			"get_values": frappe.db.get_values,
		}

		def spy(name):
			def read(doctype, filters=None, *args, **kwargs):
				if doctype in ("GX Tenant", "GX Subscription"):
					key = filters if isinstance(filters, str) else tuple(sorted(filters or {}))
					calls.append((doctype, key, bool(kwargs.get("for_update"))))
				return real[name](doctype, filters, *args, **kwargs)

			return read

		with patch.object(frappe, "get_all", side_effect=spy("get_all")):
			with patch.object(frappe.db, "get_value", side_effect=spy("get_value")):
				with patch.object(frappe.db, "get_values", side_effect=spy("get_values")):
					fn()
		# `account` and `tenant` have no index: a locking read filtered on them would lock the whole table.
		self.assertEqual([c for c in calls if c[2] and not isinstance(c[1], str)], [])
		return calls

	def test_admin_actions_and_payments_read_sites_and_marks_under_lock(self):
		"""A Suspend or a payment that waited for the account lock while the renewal job held it (through a
		Press call) must see what the job committed: after the lock, they read the account's sites and
		subscriptions again with a lock, row by row (the latest rows, not the request's snapshot)."""
		account, unpaid, unpaid_site = self._gx_customer("suspended", current_period_end="2026-12-01")
		frappe.db.set_value("GX Subscription", unpaid.name, "suspended_by", "billing")
		frappe.db.set_value("GX Tenant", unpaid_site.name, "status", "suspended")
		# the same customer's other subscription, made later (so a payment for the plan applies to it)
		_, sub, site = self._customer(
			"provisioned", "active", current_period_end="2027-01-20", account=account
		)
		doc = frappe.get_doc("GX Account", account.name)
		suspend = self._locking_reads(
			lambda: payments.set_customer_status(doc, "suspended", "ready", "suspended", "suspend_site")
		)
		self.assertIn(("GX Tenant", site.name, True), suspend)
		self.assertIn(("GX Subscription", sub.name, True), suspend)  # its resume mark
		self.assertEqual(frappe.db.get_value("GX Tenant", site.name, "status"), "suspended")
		# a resume after payment reads the subscription and its site under lock
		resume = self._locking_reads(lambda: renewals.resume_subscription_site(sub.name))
		self.assertIn(("GX Subscription", sub.name, True), resume)
		self.assertIn(("GX Tenant", site.name, True), resume)
		self.assertEqual(frappe.db.get_value("GX Tenant", site.name, "status"), "ready")
		# a payment by a suspended customer reads the other billing-suspended subscription under lock
		frappe.db.set_value("GX Tenant", site.name, "status", "suspended")
		frappe.db.set_value("GX Account", account.name, "status", "suspended")
		pay = self._locking_reads(
			lambda: payments.apply_payment(
				frappe.get_doc("GX Account", account.name),
				frappe.get_doc("GX Plan", PLAN),
				"pay-lock",
				getdate("2026-12-26"),
				"manual",
			)
		)
		self.assertIn(("GX Subscription", unpaid.name, True), pay)
		self.assertEqual(
			[frappe.db.get_value("GX Tenant", t.name, "status") for t in (site, unpaid_site)],
			["ready", "suspended"],  # the paid one is back; the unpaid one stays off
		)
		# the reactivate guard reads the billing suspension under lock too
		frappe.db.set_value("GX Account", account.name, "status", "suspended")
		answer = []
		guard = self._locking_reads(
			lambda: answer.append(
				self._at(
					"2026-12-26 10:00:00",
					admin.update_customer_status,
					{"id": account.name, "action": "reactivate"},
				)
			)
		)
		self.assertIn(("GX Subscription", unpaid.name, True), guard)
		self.assertEqual(answer[0][0], 409, answer[0][1])

	def test_day_zero_keeps_a_resume_mark_an_admin_cleared_meanwhile(self):
		"""The job saves the whole subscription on day 0 (past_due). An admin's Suspend that committed between
		its steps cleared `resume_pending`: the save must not write the old mark back (the next run would
		then resume the site the admin suspended)."""
		_, sub, _site = self._gx_customer("active", current_period_end="2026-12-26")
		frappe.db.set_value("GX Subscription", sub.name, "resume_pending", 1)  # marked; the site runs
		real_past_due = renewals._Run._past_due

		def admin_suspend_then_past_due(job, s, details):
			frappe.db.set_value("GX Subscription", s.name, "resume_pending", 0, update_modified=False)
			return real_past_due(job, s, details)

		with patch.object(renewals._Run, "_past_due", admin_suspend_then_past_due):
			self._at("2026-12-26 10:00:00")
		row = self._row(sub)
		self.assertEqual((row.status, row.resume_pending), ("past_due", 0))

	def test_resume_pending_lists_and_retries_only_a_refused_billing_resume(self):
		_, pending, pending_site = self._gx_customer("active", current_period_end="2027-01-20")
		frappe.db.set_value("GX Tenant", pending_site.name, "status", "suspended")
		# Press refused to resume it after a payment
		frappe.db.set_value("GX Subscription", pending.name, "resume_pending", 1)
		# an admin's suspension (the account turned active again by a subscription save): not marked
		_, held, held_site = self._gx_customer("active", current_period_end="2027-01-20")
		frappe.db.set_value("GX Tenant", held_site.name, "status", "suspended")
		# marked, but the site is running: nothing to retry
		_, running, running_site = self._gx_customer("active", current_period_end="2027-01-20")
		frappe.db.set_value("GX Subscription", running.name, "resume_pending", 1)
		status, body = self._at("2026-12-26 10:00:00", admin.renewals)
		self.assertEqual(status, 200, body)
		self.assertEqual([i["subscriptionId"] for i in body["resumePending"]], [pending.name])
		self.assertIn(held.name, [i["subscriptionId"] for i in body["suspended"]])
		self._at("2026-12-26 11:00:00")
		self.assertEqual(self.press.calls, [("resume_site", pending_site.press_site_id)])
		self.assertEqual(
			[
				frappe.db.get_value("GX Tenant", t.name, "status")
				for t in (pending_site, held_site, running_site)
			],
			["ready", "suspended", "ready"],
		)

	def test_a_resume_refused_while_a_customer_comes_back_is_retried(self):
		# Press refuses while the customer's sites are restored: by a payment after an admin's suspension (the
		# paid site and the customer's other one), or by Reactivate. The job tries again (Rulings 13, 19).
		account, other, other_site = self._gx_customer("active", current_period_end="2027-01-20")
		_, paid, paid_site = self._customer(
			"provisioned", "active", current_period_end="2027-01-20", account=account
		)
		suspend = {"id": account.name, "action": "suspend", "reason": "Chargeback"}
		status, body = self._at("2026-12-26 09:00:00", admin.update_customer_status, suspend)
		self.assertEqual(status, 200, body)
		self.press.fail_resume = True
		sub, _details, _end = payments.apply_payment(
			frappe.get_doc("GX Account", account.name),
			frappe.get_doc("GX Plan", PLAN),
			"pay-1",
			getdate("2026-12-26"),
			"manual",
		)
		self.assertEqual(sub.name, paid.name)  # the newest subscription of the product
		self.assertEqual(frappe.db.get_value("GX Account", account.name, "status"), "active")
		self.assertEqual([self._row(s).resume_pending for s in (other, paid)], [1, 1])
		self.press.fail_resume = False
		self._at("2026-12-26 10:00:00")
		self.assertEqual(
			[frappe.db.get_value("GX Tenant", t.name, "status") for t in (other_site, paid_site)],
			["ready", "ready"],
		)
		self.assertEqual([self._row(s).resume_pending for s in (other, paid)], [0, 0])
		# Reactivate refused by Press: the customer is active again, the job brings the site back
		status, body = self._at("2026-12-26 11:00:00", admin.update_customer_status, suspend)
		self.assertEqual(status, 200, body)
		self.press.fail_resume = True
		status, body = self._at(
			"2026-12-26 11:30:00", admin.update_customer_status, {"id": account.name, "action": "reactivate"}
		)
		self.assertEqual(status, 200, body)
		self.assertEqual(frappe.db.get_value("GX Tenant", other_site.name, "status"), "suspended")
		self.assertEqual(self._row(other).resume_pending, 1)
		self.press.fail_resume = False
		self._at("2026-12-26 12:00:00")
		self.assertEqual(frappe.db.get_value("GX Tenant", other_site.name, "status"), "ready")
		self.assertEqual(self._row(other).resume_pending, 0)

	def test_extend_grace_refused_for_an_archived_site(self):
		_, sub, tenant = self._gx_customer("suspended", trial_ends_at="2026-11-20 12:00:00")
		self._suspend(sub, tenant, archive_due_on="2026-12-21")
		frappe.db.set_value("GX Tenant", tenant.name, "status", "archived")  # archived, not canceled yet
		status, body = self._at(
			"2026-12-26 10:00:00", admin.extend_grace, {"id": sub.name, "until": "2027-01-05"}
		)
		self.assertEqual((status, body["error"]["code"]), (409, "tenant_archived"))
		self.assertEqual(self._row(sub).status, "suspended")
		self.assertEqual(self.press.calls, [])

	def test_paid_after_extra_days_the_period_starts_on_the_payment_day(self):
		account, sub, _t = self._suspended_past_due()  # ended 17 Dec, suspended 24 Dec
		self._at("2026-12-26 10:00:00", admin.extend_grace, {"id": sub.name, "until": "2027-01-05"})
		_, _, end = payments.apply_payment(
			frappe.get_doc("GX Account", account.name),
			frappe.get_doc("GX Plan", PLAN),
			"pay-1",
			getdate("2026-12-30"),
			"manual",
		)
		self.assertEqual(end, getdate("2027-01-30"))  # not 17 Jan: the site was off from 24 Dec
		self.assertFalse(self._row(sub).suspended_at)  # the payment clears it

	def test_admin_renewals_lists_unscheduled_and_dateless_suspended(self):
		_, dateless, _t = self._gx_customer("active")  # no end, due date or trial end (like GXS-00005)
		_, lifetime, _t2 = self._gx_customer("active")
		frappe.db.set_value(
			"GX Subscription", lifetime.name, {"billing_interval": "one_time", "last_payment_id": "p"}
		)
		_, legacy, legacy_site = self._direct_customer(
			"past_due"
		)  # a legacy direct one, suspended by the job
		self._suspend(legacy, legacy_site)
		status, body = self._at("2026-12-26 10:00:00", admin.renewals)
		self.assertEqual(status, 200, body)
		self.assertEqual([i["subscriptionId"] for i in body["unscheduled"]], [dateless.name])
		item = {i["subscriptionId"]: i for i in body["suspended"]}[legacy.name]
		self.assertEqual(
			(item["stage"], item["expiresAt"], item["suspendsAt"], item["archivesAt"]),
			("suspended", None, None, None),
		)
		info = renewals.renewal_info(
			frappe.get_doc("GX Subscription", legacy.name), today=getdate("2026-12-26")
		)
		self.assertEqual((info["stage"], info["expiresAt"], info["canPay"]), ("suspended", None, True))

	def test_patch_gives_legacy_direct_subscriptions_a_due_date(self):
		_, legacy, _t = self._direct_customer("past_due")
		frappe.db.set_value(
			"GX Subscription", legacy.name, "creation", "2026-10-05 11:00:00", update_modified=False
		)
		_, paid, _t2 = self._direct_customer("past_due", current_period_end="2026-11-01")
		_, gx, _t3 = self._gx_customer("past_due")
		_, dated, _t4 = self._direct_customer("past_due")
		frappe.db.set_value("GX Subscription", dated.name, "due_date", "2026-11-20")
		direct_due_dates.execute()
		due = {
			x.name: frappe.db.get_value("GX Subscription", x.name, "due_date")
			for x in (legacy, paid, gx, dated)
		}
		self.assertEqual(getdate(due[legacy.name]), getdate("2026-10-05"))
		self.assertEqual((due[paid.name], due[gx.name]), (None, None))
		self.assertEqual(getdate(due[dated.name]), getdate("2026-11-20"))
		self.assertTrue(
			frappe.db.exists("GX Audit Log", {"action": "renewal.due_date", "reference_name": legacy.name})
		)
