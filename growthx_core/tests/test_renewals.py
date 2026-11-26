"""Renewals: reminders, grace and suspension for GrowthX and ERPNext Direct subscriptions.
RenewalsCase is the shared base: provisioned (GrowthX) and imported (Direct) customers, a recording
Press client and the billing settings switched on."""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, add_months, get_datetime, getdate, now_datetime, nowdate

from growthx_core import accounts
from growthx_core.api.v1 import _serializers as s
from growthx_core.api.v1 import admin
from growthx_core.api.v1._core import iso, web_url
from growthx_core.billing import payments, renewal_emails, renewals
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
		self.fail_suspend = False
		self.fail_sites: set[str] = set()  # sites Press refuses to suspend

	def suspend_site(self, site_id):
		if self.fail_suspend or site_id in self.fail_sites:
			raise press.PressError("Press refused")
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
		_, sub, tenant = self._gx_customer("past_due", current_period_end="2026-12-10")
		# customer_action suspend: the account's sites are suspended, its subscriptions untouched
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
		_, told, told_site = self._gx_customer("active", current_period_end="2026-12-27")
		frappe.db.set_value("GX Subscription", told.name, "cancel_at_period_end", 1)
		self._settings(send_emails=1, suspend_sites=0)
		self._run("2026-12-27 10:00:00")
		self.assertEqual(self._emails(told), [("canceled", 0)])
		self.assertEqual((self._status(told), self._status(told_site)), ("canceled", "ready"))
		self.assertIn("suspend_sites is off", " ".join(self._would_lines(told)))
		self.assertEqual(self.press.calls, [])

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
		self._settings(paid_reminder_days="", grace_warning_days=" , ", trial_reminder_days="none")
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
		self._settings(online_payments=1)
		self._run("2026-12-23 10:00:00")
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
		self.assertEqual(
			[line for line in lines if "suspend" in line],
			["2026-12-26: would suspend on 2026-12-28", "2026-12-27: would suspend on 2026-12-29"],
		)
		self.assertEqual(len(lines), 4)  # plus the final warning it would send each day
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
			"GX Subscription", sub.name, "creation", "2026-12-01 09:00:00", update_modified=False
		)
		self._run("2026-12-01 10:00:00")
		self._run("2026-12-02 10:00:00")
		self.assertEqual(self._emails(sub), [("first_due", 0), ("paid_warning", 1)])

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
		# a yearly plan is reminded 30 days ahead
		_, sub, _t = self._customer("provisioned", "active", current_period_end="2027-01-20", plan=YEARLY)
		self._expect(self._info(sub), "due_soon", 25, "2027-01-20", suspends="2027-01-27")
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
		self.assertFalse(row.suspended_by or row.suspended_at)
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
