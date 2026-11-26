"""Renewals: reminders, grace and suspension for GrowthX and ERPNext Direct subscriptions.

The hourly job (`run`, design: growthx-infra docs/renewals-design.md 5.1, 5.2, 6.1) walks every live
subscription along its timeline:
- **Paid** monthly or yearly plans, and ERPNext Direct customers who are due (added "due now", or "billing
  starts later" reaching its start date): reminders before day 0, `past_due` on day 0, warnings after it,
  suspension once the final warning has been out long enough (never without it).
- **GrowthX trials:** reminders before the trial's last day; the site is paused from the next day, but
  never less than a day after the last reminder went out. A never-paid account's paused site is deleted
  (archived) 30 days later, after a warning.
Days are calendar days in the site's time zone (Asia/Kolkata). Nothing happens unless GX Billing Settings
is enabled; with dry_run, the job only writes `renewal.would` audit lines.
"""

from datetime import date, timedelta

import frappe
from frappe.utils import add_days, cint, getdate, now_datetime

from growthx_core.api.v1._core import audit
from growthx_core.billing import renewal_emails
from growthx_core.billing.doctype.gx_subscription.gx_subscription import sync_account_status
from growthx_core.billing.renewal_schedule import (
	final_stage,
	format_sent,
	paid_stages,
	parse_days,
	parse_sent,
	stage_to_send,
	suspension_day,
)
from growthx_core.provisioning import runner

LIVE = ("trialing", "active", "past_due", "suspended")
# Sites the job leaves alone: not built yet, or their build failed (an admin retries or archives those).
SITES_NOT_LIVE = ("requested", "provisioning", "failed")
# What a day list falls back to when its setting is blank or has no valid day in it.
DEFAULT_DAYS = {
	"paid_reminder_days": "7,3,1",
	"yearly_extra_reminder_days": "30",
	"grace_warning_days": "1,3,5",
	"trial_reminder_days": "3,1",
}
SAVEPOINT = "gx_renewal"
# Fields a payment or an admin changes and the job doesn't: checked again before suspending or archiving.
WATCHED = ("last_payment_id", "current_period_end", "trial_ends_at", "grace_until")


def _now():
	return now_datetime()


def _today() -> date:
	return getdate(_now())


def _date(value) -> date | None:
	return getdate(value) if value else None


def resume_subscription_site(sub_name: str) -> None:
	"""Bring the subscription's site back after a payment. A GrowthX (provisioned) site is resumed on
	Press; an ERPNext Direct (imported) site is the customer's own and only changes status here. If
	Press refuses, the error is logged and the tenant stays suspended."""
	tenant_name = frappe.db.get_value("GX Subscription", sub_name, "tenant")
	if not tenant_name:
		return
	tenant = frappe.db.get_value(
		"GX Tenant", tenant_name, ["name", "status", "origin", "press_site_id"], as_dict=True
	)
	if not tenant or tenant.status != "suspended":
		return
	if tenant.origin != "imported" and tenant.press_site_id:
		try:
			runner.get_client().resume_site(tenant.press_site_id)
		except Exception:
			frappe.log_error(title=f"GrowthX: resume_site failed for {tenant.name}")
			return
	frappe.db.set_value("GX Tenant", tenant.name, "status", "ready")
	audit("renewal.resume", "GX Subscription", sub_name, f"site {tenant.name} resumed after payment")


def never_paid(account: str) -> bool:
	"""True if none of the account's subscriptions has ever recorded a payment."""
	paid = frappe.get_all("GX Subscription", {"account": account}, pluck="last_payment_id")
	return not any(paid)


def suspend_subscription(sub_name: str, by: str = "billing") -> bool:
	"""Suspend the subscription's site. A GrowthX (provisioned) site is suspended on Press first and
	Core changes only if that succeeds; an ERPNext Direct (imported) site is the customer's own, so
	only status changes here. A never-paid GrowthX trial also gets an archive date. The account becomes
	`suspended` once none of its subscriptions is live. Returns False if Press refused (logged, nothing
	changed)."""
	sub = frappe.db.get_value("GX Subscription", sub_name, ["name", "account", "tenant"], as_dict=True)
	if not sub or not sub.tenant:
		return False
	tenant = frappe.db.get_value("GX Tenant", sub.tenant, ["name", "origin", "press_site_id"], as_dict=True)
	if not tenant:
		return False
	imported = tenant.origin == "imported"
	if not imported and tenant.press_site_id:
		try:
			runner.get_client().suspend_site(tenant.press_site_id)
		except Exception:
			frappe.log_error(title=f"GrowthX: suspend_site failed for {tenant.name}")
			return False
	values = {"status": "suspended", "suspended_at": _now(), "suspended_by": by}
	if not imported and never_paid(sub.account):
		days = cint(frappe.db.get_single_value("GX Billing Settings", "trial_archive_after_days"))
		values["archive_due_on"] = add_days(_today(), days)
	frappe.db.set_value("GX Tenant", tenant.name, "status", "suspended")
	frappe.db.set_value("GX Subscription", sub_name, values)
	sync_account_status(sub.account)
	audit("renewal.suspend", "GX Subscription", sub_name, f"site {tenant.name} suspended ({by})")
	return True


def archive_trial(sub_name: str) -> bool:
	"""Back up and archive a never-paid GrowthX trial's site and cancel the subscription. Refuses
	(False) for an ERPNext Direct tenant and for any account that has ever paid. runner.archive commits
	the tenant before the subscription is canceled: called again after a failure in between, this
	cancels the subscription without asking Press again."""
	sub = frappe.db.get_value("GX Subscription", sub_name, ["name", "account", "tenant"], as_dict=True)
	if not sub or not sub.tenant:
		return False
	tenant = frappe.db.get_value("GX Tenant", sub.tenant, ["origin", "status"], as_dict=True)
	if not tenant or tenant.origin == "imported" or not never_paid(sub.account):
		return False
	if tenant.status != "archived":
		runner.archive(sub.tenant)
	frappe.db.set_value("GX Subscription", sub_name, "status", "canceled")
	audit("renewal.archive", "GX Subscription", sub_name, f"site {sub.tenant} archived")
	return True


def day0(sub, tenant=None) -> date | None:
	"""Day 0 of the subscription's timeline, or None if it has none:
	- a GrowthX trial: the trial's last day (it is over from the next midnight);
	- an ERPNext Direct customer whose billing starts later (`trialing`): the start date;
	- otherwise the current period's end, or for a first payment the due date;
	- a paid lifetime plan: None (nothing to renew)."""
	if sub.billing_interval == "one_time" and sub.status == "active":
		return None
	if sub.status == "trialing":
		if tenant and tenant.origin == "imported":
			return _date(sub.due_date or sub.trial_ends_at)
		return _date(sub.trial_ends_at)
	if sub.current_period_end or sub.due_date:
		return _date(sub.current_period_end or sub.due_date)
	# A GrowthX trial the job paused: its archive dates count from the trial's end.
	return _date(sub.trial_ends_at) if sub.status == "suspended" else None


def run() -> dict:
	"""Scheduler (hourly): move every live subscription along its timeline. Returns what was done (or,
	with dry_run, would have been): {"emails", "past_due", "suspended", "archived", "held"}; `held` counts
	suspensions and archives left for the next run by the per-run caps."""
	settings = frappe.get_single("GX Billing Settings")
	job = _Run(settings)
	if not cint(settings.enabled):
		return job.counts
	for name in frappe.get_all(
		"GX Subscription", filters={"status": ("in", LIVE)}, order_by="creation asc", pluck="name"
	):
		job.process(name)
	return job.counts


def _days(settings, field: str) -> list[int]:
	return parse_days(settings.get(field)) or parse_days(DEFAULT_DAYS[field])


class _Run:
	"""One run of the job: the settings, today's date and the counts."""

	def __init__(self, settings):
		self.today = _today()
		self.dry = bool(cint(settings.dry_run))
		self.send_emails = bool(cint(settings.send_emails))
		self.suspend_sites = bool(cint(settings.suspend_sites))
		self.reminder_days = _days(settings, "paid_reminder_days")
		self.yearly_days = _days(settings, "yearly_extra_reminder_days")
		self.warning_days = _days(settings, "grace_warning_days")
		self.trial_days = _days(settings, "trial_reminder_days")
		self.final = final_stage(self.warning_days)
		self.suspend_after = cint(settings.suspend_after_days)
		self.archive_warning_days = cint(settings.trial_archive_warning_days)
		self.max_suspensions = cint(settings.max_suspensions_per_run)
		self.max_archives = cint(settings.max_archives_per_run)
		self.counts = {"emails": 0, "past_due": 0, "suspended": 0, "archived": 0, "held": 0}
		self.seen = {}  # the subscription's WATCHED fields as read (and locked) at the start

	# ------------------------------------------------------------ one subscription, isolated

	def process(self, name: str):
		"""One subscription. Its work is committed step by step; if anything fails, what isn't committed yet
		is undone, the error is logged and the run goes on with the next subscription."""
		self.checkpoint()
		try:
			self._process(name)
		except Exception:
			try:
				frappe.db.rollback(save_point=SAVEPOINT)
			except Exception:
				frappe.db.rollback()  # the savepoint went with a commit (runner.archive's): undo what followed
			frappe.log_error(title=f"GrowthX: renewal job failed for {name}")
		frappe.db.commit()

	def checkpoint(self):
		"""Keep what is done so far (an email that went out must stay recorded) and start a new savepoint."""
		frappe.db.commit()
		frappe.db.savepoint(SAVEPOINT)

	def _process(self, name: str):
		# Locked: a payment for this subscription waits until we commit, and we wait for one in progress.
		sub = frappe.get_doc("GX Subscription", name, for_update=True)
		if sub.status not in LIVE or (sub.billing_interval == "one_time" and sub.status == "active"):
			return  # a paid lifetime plan has nothing to renew
		self.seen = {field: sub.get(field) for field in WATCHED}
		tenant = sub.tenant and frappe.db.get_value(
			"GX Tenant",
			sub.tenant,
			["name", "status", "origin", "press_site_id", "site_url", "display_name"],
			as_dict=True,
		)
		account = frappe.db.get_value(
			"GX Account", sub.account, ["name", "account_name", "contact_email", "user"], as_dict=True
		)
		if not tenant or not account:
			return
		if tenant.status == "archived":
			self._finish_archive(sub, tenant, account)
			return
		if tenant.status in SITES_NOT_LIVE:
			return
		if tenant.status == "suspended" and sub.suspended_by != "billing":
			return  # an admin's suspension (customer_action suspend, account-wide): the admin lifts it
		if sub.status == "suspended":
			self._paused_trial(sub, tenant, account)
			return
		start = day0(sub, tenant)
		if start is None:
			return
		offset = (self.today - start).days
		if sub.status == "trialing" and tenant.origin != "imported":
			self._trial(sub, tenant, account, start, offset)
		else:
			self._paid(sub, tenant, account, start, offset)

	# ------------------------------------------------------------ paid plans and ERPNext Direct

	def _paid(self, sub, tenant, account, start: date, offset: int):
		stages = paid_stages(
			self.reminder_days, self.warning_days, self.yearly_days, sub.billing_interval == "year"
		)
		cancelling = bool(cint(sub.cancel_at_period_end))
		if self.send_emails and not cancelling:  # no reminders for a plan the customer cancelled
			stage = stage_to_send(stages, offset, parse_sent(sub.notified_stages))
			if stage is not None:
				final = stage == self.final
				dates = {
					"due": start,
					"suspend": self._suspends_on(sub, start, self.today if final else None),
				}
				if self._email(sub, tenant, account, self._paid_kind(sub, stage), stage, dates):
					self._record(sub, [s for s in stages if s <= stage], final_warning=final)
		if offset >= 0 and sub.status in ("active", "trialing"):
			self._past_due(sub, f"day 0 was {start}")
		if sub.status != "past_due" or not self.suspend_sites:
			return
		if cancelling:
			# Cancelled at period end: suspended on day 0 (no grace), canceled, and never archived.
			if offset >= 0 and not self._held("suspended", self.max_suspensions):
				self._cancel(sub)
			return
		on = suspension_day(
			start, self.suspend_after, self.final, _date(sub.final_warning_on), _date(sub.grace_until)
		)
		if on is None or self.today < on or self._held("suspended", self.max_suspensions):
			return
		if self._suspend(sub, "after the final warning") and self.send_emails:
			self._email(sub, tenant, account, "suspended", None, {"due": start})

	def _paid_kind(self, sub, stage: int) -> str:
		if stage < 0:
			return "paid_reminder"
		if stage == 0:
			return "paid_expired" if sub.current_period_end else "first_due"  # never paid: the first payment
		return "paid_final" if stage == self.final else "paid_warning"

	def _suspends_on(self, sub, start: date, final_warning_on: date | None) -> date:
		"""The suspension day to tell the customer: the real one once the final warning is out, else the one
		it will be if the final warning goes on its day (or today, if that is already past)."""
		sent = final_warning_on or _date(sub.final_warning_on)
		planned = max(self.today, start + timedelta(days=self.final))
		return suspension_day(start, self.suspend_after, self.final, sent or planned, _date(sub.grace_until))

	def _cancel(self, sub):
		if not self._suspend(sub, "at period end (cancelled)"):
			return
		if self.dry:
			return
		sub.reload()  # suspend_subscription wrote the row
		sub.status = "canceled"
		sub.archive_due_on = None
		sub.save(ignore_permissions=True)
		audit("renewal.suspend", "GX Subscription", sub.name, "subscription canceled at period end")
		self.checkpoint()

	# ------------------------------------------------------------ GrowthX trials

	def _trial(self, sub, tenant, account, start: date, offset: int):
		reminders = sorted(-d for d in self.trial_days)
		last = reminders[-1]
		if self.send_emails:
			stage = stage_to_send(reminders, offset, parse_sent(sub.notified_stages))
			if stage is not None:
				warned = self.today if stage == last else _date(sub.final_warning_on)
				planned = max(self.today, start + timedelta(days=last))
				pause = max(start, (warned or planned)) + timedelta(days=1)
				dates = {"due": start, "suspend": pause}
				if self._email(sub, tenant, account, "trial_reminder", stage, dates):
					self._record(sub, [s for s in reminders if s <= stage], final_warning=stage == last)
		# Paused from the day after the trial's last day, and never less than a day after the last reminder.
		warned = _date(sub.final_warning_on)
		if offset < 1 or not self.suspend_sites or not warned or self.today < warned + timedelta(days=1):
			return
		if self._held("suspended", self.max_suspensions):
			return
		if self.send_emails and 0 not in parse_sent(sub.notified_stages):
			if not self._email(sub, tenant, account, "trial_ended", 0, {"due": start}):
				return  # the site is paused once the customer has been told: retried next run
			self._record(sub, [0])
		self._suspend(sub, "trial ended")

	def _paused_trial(self, sub, tenant, account):
		"""A never-paid GrowthX site the job paused: warn, then archive on `archive_due_on`. The warning must
		have gone out first; a late one moves the archive date so the customer always gets the full notice."""
		due = _date(sub.archive_due_on)
		if (
			sub.suspended_by != "billing"
			or tenant.origin == "imported"
			or tenant.status != "suspended"
			or cint(sub.cancel_at_period_end)  # cancelled at period end: never archived
			or not due
			or not self.suspend_sites
		):
			return
		start = day0(sub, tenant)
		if start is None or not never_paid(sub.account):
			return
		if (due - start).days not in parse_sent(sub.notified_stages):
			# The warning is recorded as the archive day's offset, so a moved archive date gets its own.
			if not self.send_emails or self.today < due - timedelta(days=self.archive_warning_days):
				return
			due = max(due, self.today + timedelta(days=max(self.archive_warning_days, 1)))
			dates = {"due": start, "archive": due}
			if self._email(sub, tenant, account, "trial_archive_warning", None, dates):
				self._record(sub, [(due - start).days], archive_due_on=due)
			return
		if self.today < due or self._held("archived", self.max_archives):
			return
		if self.dry:
			self._would(sub, f"archive {tenant.name}")
		elif not self._unchanged(sub) or not archive_trial(sub.name):
			return
		else:
			self.checkpoint()
		self.counts["archived"] += 1
		if self.send_emails:
			self._email(sub, tenant, account, "trial_archived", None, {"due": start})

	def _finish_archive(self, sub, tenant, account):
		"""The tenant is archived but the subscription isn't canceled yet (the job stopped between the two):
		cancel it now, without asking Press again."""
		due = _date(sub.archive_due_on)
		if sub.status != "suspended" or sub.suspended_by != "billing" or tenant.origin == "imported":
			return
		if not due or self.today < due:
			return
		if self.dry:
			self._would(sub, f"finish archiving {tenant.name}")
			return
		if self._unchanged(sub) and archive_trial(sub.name):
			self.checkpoint()
			if self.send_emails:
				self._email(sub, tenant, account, "trial_archived", None, {"due": day0(sub, tenant) or due})

	# ------------------------------------------------------------ actions (dry_run: audit lines only)

	def _email(self, sub, tenant, account, kind: str, stage: int | None, dates: dict) -> bool:
		if self.dry:
			self._would(sub, f"email {kind}" + ("" if stage is None else f" (day {stage:+d})"))
		elif not renewal_emails.send(sub, tenant, account, kind, stage, {"today": self.today, **dates}):
			return False
		self.counts["emails"] += 1
		return True

	def _record(self, sub, stages, final_warning: bool = False, archive_due_on: date | None = None):
		"""Note the stages as sent (an email that failed is not noted, so it is tried again next run)."""
		values = {"notified_stages": format_sent(parse_sent(sub.notified_stages) | set(stages))}
		if final_warning:
			values["final_warning_on"] = self.today
		if archive_due_on:
			values["archive_due_on"] = archive_due_on
		if self.dry:
			sub.update(values)
			return
		sub.db_set(values, update_modified=False)
		self.checkpoint()

	def _past_due(self, sub, details: str):
		if self.dry:
			self._would(sub, f"{sub.status} → past_due ({details})")
			sub.status = "past_due"
		else:
			sub.status = "past_due"
			sub.save(ignore_permissions=True)  # on_update keeps the account's status in step
			audit("renewal.past_due", "GX Subscription", sub.name, details)
			self.checkpoint()
		self.counts["past_due"] += 1

	def _suspend(self, sub, reason: str) -> bool:
		if self.dry:
			self._would(sub, f"suspend ({reason})")
		elif not self._unchanged(sub):
			return False  # paid (or changed by an admin) meanwhile: the next run looks at it afresh
		elif not suspend_subscription(sub.name):
			return False  # Press refused: logged; retried next run, and not counted against the cap
		else:
			self.checkpoint()
		self.counts["suspended"] += 1
		return True

	def _unchanged(self, sub) -> bool:
		"""Lock the row again (a checkpoint's commit released it) and check that nobody changed it since it
		was read: a payment that waited for the lock must never be followed by a suspension or an archive."""
		row = frappe.db.get_value(
			"GX Subscription", sub.name, ["status", *WATCHED], as_dict=True, for_update=True
		)
		return bool(row) and row.status == sub.status and all(row[f] == self.seen[f] for f in WATCHED)

	def _held(self, count: str, cap: int) -> bool:
		"""True if this run's cap is reached: the subscription waits for the next run."""
		if self.counts[count] < cap:
			return False
		self.counts["held"] += 1
		return True

	def _would(self, sub, what: str):
		audit("renewal.would", "GX Subscription", sub.name, what)
