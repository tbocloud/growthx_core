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

Switching it on (Ruling 15):
- **Step 1:** `enabled` on, `send_emails` on, `suspend_sites` off, `dry_run` off (or on, to only log).
  While `suspend_sites` is off the job sends the reminders before day 0 and the day-0 email, but no
  warning after day 0 and nothing that names a suspension or pause day; no final warning is recorded,
  a plan cancelled at period end is left as it is, and the customer's renewal info has no `suspendsAt`.
  `renewal.would` lines say what would be suspended once it is on.
- **Step 2:** `suspend_sites` on. The warnings then run from that day, so every final notice is fresh: a
  paid plan is suspended no sooner than two days after its final notice (`suspend_after_days` minus the
  last warning day, with the defaults), a trial no sooner than the day after its last reminder, which is
  sent again naming that day.
The first live run (enabled, dry_run off) records `started_on`: a subscription made before it gets its
first day 0 no sooner than 7 days later, so every existing customer gets the whole timeline.
"""

from datetime import date, timedelta

import frappe
from frappe.utils import add_days, cint, getdate, now_datetime

from growthx_core.api.v1._core import audit, iso
from growthx_core.billing import locks, renewal_emails
from growthx_core.billing.doctype.gx_billing_settings.gx_billing_settings import get_int, int_setting
from growthx_core.billing.doctype.gx_subscription.gx_subscription import (
	sync_account_status,
	sync_account_status_after_cancel,
)
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
# Live and not suspended: a subscription whose site should be running.
RUNNING = ("trialing", "active", "past_due")
# A subscription made before the job's first live run gets its first day 0 no sooner than this.
FIRST_NOTICE_DAYS = 7
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


def resume_subscription_site(sub_name: str, why: str = "after payment") -> None:
	"""Bring the subscription's site back after a payment (or extra days). A GrowthX (provisioned) site is
	resumed on Press; an ERPNext Direct (imported) site is the customer's own and only changes status here.
	If Press refuses, the error is logged, the tenant stays suspended and the subscription is marked
	`resume_pending`: the hourly job tries again (Ruling 19). The mark is cleared once the site is back.
	(Give extra days undoes everything on a refusal, the mark included: the admin tries again.)"""
	# Locking reads, by name: a payment that waited for the account lock while the job suspended this site
	# must see the site as it is now. Callers that use lock_account read current rows anyway, but PayU's
	# _activate locks its checkout first, so it can't start the fresh transaction lock_account needs.
	sub = frappe.db.get_value(
		"GX Subscription", sub_name, ["tenant", "resume_pending"], as_dict=True, for_update=True
	)
	if not sub or not sub.tenant:
		return
	tenant = frappe.db.get_value(
		"GX Tenant", sub.tenant, ["name", "status", "origin", "press_site_id"], as_dict=True, for_update=True
	)
	if not tenant or tenant.status != "suspended":
		_mark_resume_pending(sub_name, sub, 0)  # the site isn't off: nothing to retry
		return
	if tenant.origin != "imported" and tenant.press_site_id:
		try:
			runner.get_client().resume_site(tenant.press_site_id)
		except Exception:
			frappe.log_error(title=f"GrowthX: resume_site failed for {tenant.name}")
			_mark_resume_pending(sub_name, sub, 1)
			return
	frappe.db.set_value("GX Tenant", tenant.name, "status", "ready")
	_mark_resume_pending(sub_name, sub, 0)
	audit("renewal.resume", "GX Subscription", sub_name, f"site {tenant.name} resumed {why}")


def _mark_resume_pending(sub_name: str, sub, value: int):
	if cint(sub.resume_pending) != value:
		frappe.db.set_value("GX Subscription", sub_name, "resume_pending", value, update_modified=False)


def mark_resume_pending(tenant_name: str, *, fresh: bool = True):
	"""Press refused to bring this tenant's site back when the customer's sites were restored (a payment, an
	admin's Reactivate): its running subscriptions are marked, so the job tries again. `fresh` as for
	payments.apply_payment (locks.account_rows)."""
	for row in locks.account_rows(
		"GX Subscription", {"tenant": tenant_name}, ["status", "resume_pending"], fresh=fresh
	):
		if row.status in RUNNING and not cint(row.resume_pending):
			frappe.db.set_value("GX Subscription", row.name, "resume_pending", 1, update_modified=False)


def clear_resume_pending(scope: dict, *, fresh: bool = True):
	"""Forget a resume Press refused, for the subscriptions in `scope` (an account's or a tenant's): their
	site is running again, or an admin suspended the customer (whose suspension the job's retry must never
	undo). `fresh` as for payments.apply_payment (locks.account_rows)."""
	for row in locks.account_rows("GX Subscription", scope, ["resume_pending"], fresh=fresh):
		if cint(row.resume_pending):
			frappe.db.set_value("GX Subscription", row.name, "resume_pending", 0, update_modified=False)


def resume_pending(sub, tenant) -> bool:
	"""Press refused to bring the site back after a payment or an admin's Reactivate (`resume_pending`, set
	only where Press refused a resume), and the site is still off while the subscription runs: the job tries
	again.
	Never inferred from the account's status, which any subscription save recomputes: a site an admin
	suspended is never resumed by the job (Ruling 19)."""
	return (
		bool(tenant)
		and bool(cint(sub.get("resume_pending")))
		and tenant.status == "suspended"
		and sub.status in RUNNING
	)


def never_paid(account: str) -> bool:
	"""True if none of the account's subscriptions has ever recorded a payment."""
	paid = frappe.get_all("GX Subscription", {"account": account}, pluck="last_payment_id")
	return not any(paid)


def suspend_subscription(sub_name: str, by: str = "billing") -> bool:
	"""Suspend the subscription's site. A GrowthX (provisioned) site is suspended on Press first and
	Core changes only if that succeeds; an ERPNext Direct (imported) site is the customer's own, so
	only status changes here. Only a GrowthX trial (still `trialing`) of a never-paid account gets an
	archive date: a paid plan, a direct customer or a cancelled plan is never archived by billing. The
	account becomes `suspended` once none of its subscriptions is live. Returns False if Press refused
	(logged, nothing changed)."""
	sub = frappe.db.get_value(
		"GX Subscription", sub_name, ["name", "account", "tenant", "status"], as_dict=True
	)
	if not sub or not sub.tenant:
		return False
	tenant = frappe.db.get_value("GX Tenant", sub.tenant, ["name", "origin", "press_site_id"], as_dict=True)
	if not tenant:
		return False
	imported = tenant.origin == "imported"
	on_press = not imported and bool(tenant.press_site_id)
	if on_press:
		try:
			runner.get_client().suspend_site(tenant.press_site_id)
		except Exception:
			frappe.log_error(title=f"GrowthX: suspend_site failed for {tenant.name}")
			return False
	try:
		# (a new suspension replaces any resume still pending)
		values = {"status": "suspended", "suspended_at": _now(), "suspended_by": by, "resume_pending": 0}
		if not imported and sub.status == "trialing" and never_paid(sub.account):
			values["archive_due_on"] = add_days(_today(), int_setting("trial_archive_after_days"))
		frappe.db.set_value("GX Tenant", tenant.name, "status", "suspended")
		frappe.db.set_value("GX Subscription", sub_name, values)
		sync_account_status(sub.account)
		audit("renewal.suspend", "GX Subscription", sub_name, f"site {tenant.name} suspended ({by})")
	except Exception:
		if on_press:
			# Core rolls back but the site stays off on Press: DevOps must resume it (or retry the suspension).
			# Deferred, so the log survives the caller's rollback.
			frappe.log_error(
				title=f"GrowthX: Press suspended {tenant.press_site_id} but Core was not updated",
				reference_doctype="GX Subscription",
				reference_name=sub_name,
				defer_insert=True,
			)
		raise
	return True


def archive_trial(sub_name: str) -> bool:
	"""Back up and archive a never-paid GrowthX trial's site and cancel the subscription. Refuses
	(False) for an ERPNext Direct tenant and for any account that has ever paid. runner.archive commits
	the tenant before the subscription is canceled: called again after a failure in between, this
	cancels the subscription without asking Press again. The account's status follows (churned once
	nothing else is live; an account an admin suspended stays suspended while another one is)."""
	sub = frappe.db.get_value("GX Subscription", sub_name, ["name", "account", "tenant"], as_dict=True)
	if not sub or not sub.tenant:
		return False
	tenant = frappe.db.get_value("GX Tenant", sub.tenant, ["origin", "status"], as_dict=True)
	if not tenant or tenant.origin == "imported" or not never_paid(sub.account):
		return False
	if tenant.status != "archived":
		runner.archive(sub.tenant)
	# The account again first (runner.archive's commit released it): the payments' order, account then
	# subscription, for the writes below.
	frappe.db.get_value("GX Account", sub.account, "name", for_update=True)
	frappe.db.set_value("GX Subscription", sub_name, "status", "canceled")
	# set_value skips the subscription's on_update; an admin's suspension of the account stands
	sync_account_status_after_cancel(sub.account)
	audit("renewal.archive", "GX Subscription", sub_name, f"site {sub.tenant} archived")
	return True


def day0(sub, tenant=None) -> date | None:
	"""Day 0 of the subscription's timeline, or None if it has none:
	- a GrowthX trial: the trial's last day (it is over from the next midnight);
	- an ERPNext Direct customer whose billing starts later (`trialing`): the start date;
	- otherwise the current period's end, or for a first payment the due date;
	- a never-paid `past_due` subscription made before due dates were stored: the day it was created;
	- a paid lifetime plan: None (nothing to renew)."""
	if sub.billing_interval == "one_time" and sub.status == "active":
		return None
	if sub.status == "trialing":
		if tenant and tenant.origin == "imported":
			return _date(sub.due_date or sub.trial_ends_at)
		return _date(sub.trial_ends_at)
	if sub.current_period_end or sub.due_date:
		return _date(sub.current_period_end or sub.due_date)
	if sub.status == "past_due" and not sub.get("last_payment_id"):
		return _date(sub.get("creation"))
	# A GrowthX trial the job paused: its emails name the day the trial ended.
	return _date(sub.trial_ends_at) if sub.status == "suspended" else None


def run() -> dict:
	"""Scheduler (hourly): move every live subscription along its timeline. Returns what was done (or,
	with dry_run, would have been): {"emails", "past_due", "suspended", "archived", "held"}; `held` counts
	suspensions and archives left for the next run by the per-run caps."""
	settings = frappe.get_single("GX Billing Settings")
	job = _Run(settings)
	if not cint(settings.enabled):
		return job.counts
	if not job.started_on:
		if job.dry:
			job.first_day = job.today  # a dry run shows what going live today would do
		else:
			# The first live run: subscriptions made before today get at least FIRST_NOTICE_DAYS.
			job.started_on = job.first_day = job.today
			frappe.db.set_single_value("GX Billing Settings", "started_on", job.today, update_modified=False)
			audit("renewal.started", details=f"renewals live from {job.today}")
			frappe.db.commit()
	# Loaded now: a meta lookup that reached the database inside a subscription's transaction would fix its
	# snapshot before the account lock (see _Run.process).
	for doctype in ("GX Account", "GX Subscription", "GX Tenant"):
		frappe.get_meta(doctype)
	for name in frappe.get_all(
		"GX Subscription",
		filters={"status": ("in", LIVE)},
		or_filters={"not_billed": 0, "resume_pending": 1},  # a ticked one only for a refused resume
		order_by="creation asc",
		pluck="name",
	):
		job.process(name)
	return job.counts


def new_job() -> "_Run":
	"""A job object carrying today's date and the settings' rules, for computing what the job would do."""
	return _Run(frappe.get_single("GX Billing Settings"))


def _days(settings, field: str) -> list[int]:
	return parse_days(settings.get(field)) or parse_days(DEFAULT_DAYS[field])


class _Changed(Exception):
	"""The subscription changed under the job (a payment, an admin): it is left for the next run."""


class _Run:
	"""One run of the job: the settings, today's date and the counts.

	Locking: a subscription is locked after its account (GX Account, then GX Subscription), the order
	payments use, so the two never deadlock. Each checkpoint commits, which releases both; the next step
	locks them again in that order and checks that nobody changed the subscription in between."""

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
		self.suspend_after = get_int(settings, "suspend_after_days")
		self.archive_warning_days = get_int(settings, "trial_archive_warning_days")
		self.max_suspensions = get_int(settings, "max_suspensions_per_run")
		self.max_archives = get_int(settings, "max_archives_per_run")
		self.started_on = _date(settings.get("started_on"))
		# Subscriptions made before this day get their first day 0 FIRST_NOTICE_DAYS after it, at the earliest.
		self.first_day = self.started_on
		self.counts = {"emails": 0, "past_due": 0, "suspended": 0, "archived": 0, "held": 0}
		self.sub_name = self.account_name = None
		self.seen = {}  # the subscription's WATCHED fields as first read under the lock

	# ------------------------------------------------------------ one subscription, isolated

	def process(self, name: str):
		"""One subscription. Its work is committed step by step; if anything fails, what isn't committed yet
		is undone, the error is logged and the run goes on with the next subscription.
		The account is read before the checkpoint, so the next transaction's first read is the lock: MariaDB
		fixes a snapshot at a transaction's first plain read, and the job must see what an admin committed
		while it waited for the lock (a Suspend that cancelled a pending resume)."""
		try:
			account = frappe.db.get_value("GX Subscription", name, "account")
			self.checkpoint()
			self._process(name, account)
		except _Changed:
			self._undo()
		except Exception:
			self._undo()
			frappe.log_error(title=f"GrowthX: renewal job failed for {name}")
		frappe.db.commit()
		self.sub_name = self.account_name = None

	def _undo(self):
		try:
			frappe.db.rollback(save_point=SAVEPOINT)
		except Exception:
			frappe.db.rollback()  # the savepoint went with a commit (runner.archive's): undo what followed

	def checkpoint(self, sub=None):
		"""Keep what is done so far (an email queued must stay recorded) and start a new savepoint. With
		`sub`, lock it again (account first) and stop if it changed meanwhile."""
		# "The first statement after a commit is the lock" (see process) also relies on after_commit
		# callbacks not reading the database: today they only enqueue to Redis (emails, realtime).
		frappe.db.commit()
		frappe.db.savepoint(SAVEPOINT)
		if sub is not None and not self._unchanged(sub):
			raise _Changed

	def _lock(self):
		"""Lock the account, then the subscription (the order payments use). Returns the subscription's
		status and WATCHED fields."""
		frappe.db.get_value("GX Account", self.account_name, "name", for_update=True)
		return frappe.db.get_value(
			"GX Subscription", self.sub_name, ["status", *WATCHED], as_dict=True, for_update=True
		)

	def _unchanged(self, sub) -> bool:
		"""Lock again and check that nobody changed the subscription since it was read: a payment that
		waited for the lock must never be followed by a suspension, an archive or a stale record."""
		row = self._lock()
		return bool(row) and row.status == sub.status and all(row[f] == self.seen[f] for f in WATCHED)

	def start(self, sub, tenant=None) -> date | None:
		"""Day 0 (`day0`), for a subscription made before the first live run no sooner than
		FIRST_NOTICE_DAYS after it: every existing customer gets the whole timeline from switch-on."""
		start = day0(sub, tenant)
		first = self.first_day
		if start is None or not first or sub.status == "suspended":
			return start
		created = _date(sub.get("creation"))
		if created and created < first:
			return max(start, first + timedelta(days=FIRST_NOTICE_DAYS))
		return start

	def _process(self, name: str, account: str | None):
		if not account:
			return
		self.account_name, self.sub_name = account, name
		row = self._lock()  # the transaction's first read (see process)
		if not row:
			return
		self.seen = {field: row[field] for field in WATCHED}
		sub = frappe.get_doc("GX Subscription", name)
		if cint(sub.not_billed):
			# A test or internal subscription: no reminder, suspension or archive. Bringing a site back is
			# always safe, so a resume Press refused (after Reactivate) is still retried, and nothing else.
			tenant = sub.tenant and frappe.db.get_value(
				"GX Tenant", sub.tenant, ["name", "status"], as_dict=True
			)
			if resume_pending(sub, tenant):
				self._resume(sub, tenant)
			return
		if sub.status not in LIVE or (sub.billing_interval == "one_time" and sub.status == "active"):
			return  # a paid lifetime plan has nothing to renew
		tenant = sub.tenant and frappe.db.get_value(
			"GX Tenant",
			sub.tenant,
			["name", "status", "origin", "press_site_id", "site_url", "display_name"],
			as_dict=True,
		)
		account = frappe.db.get_value(
			"GX Account",
			sub.account,
			["name", "account_name", "contact_email", "user", "status"],
			as_dict=True,
		)
		if not tenant or not account:
			return
		if tenant.status == "archived":
			self._finish_archive(sub, tenant, account)
			return
		if tenant.status in SITES_NOT_LIVE:
			return
		if resume_pending(sub, tenant) and not self._resume(sub, tenant):
			return
		if tenant.status == "suspended" and sub.suspended_by != "billing":
			return  # an admin's suspension (customer_action suspend, account-wide): the admin lifts it
		if sub.status == "suspended":
			self._paused_trial(sub, tenant, account)
			return
		start = self.start(sub, tenant)
		if start is None:
			return
		offset = (self.today - start).days
		if sub.status == "trialing" and tenant.origin != "imported":
			self._trial(sub, tenant, account, start, offset)
		else:
			self._paid(sub, tenant, account, start, offset)

	# ------------------------------------------------------------ paid plans and ERPNext Direct

	def _paid(self, sub, tenant, account, start: date, offset: int):
		self._drop_stale_warnings(sub, start)  # a final warning is never sent before day 0
		if cint(sub.cancel_at_period_end):
			if offset >= 0 and sub.status in ("active", "past_due"):
				self._cancel_at_period_end(sub, tenant, account, start)
			return  # and no reminders for a plan the customer cancelled
		stages = paid_stages(
			self.reminder_days, self.warning_days, self.yearly_days, sub.billing_interval == "year"
		)
		if not self.suspend_sites:
			# No warnings after day 0 until suspension is live: they would name a day nothing happens.
			stages = [s for s in stages if s <= 0]
		if self.send_emails:
			stage = stage_to_send(stages, offset, parse_sent(sub.notified_stages))
			kind = stage is not None and self._paid_kind(sub, stage)
			if kind == "first_due" and _date(sub.get("creation")) == self.today:
				# Added today: the invite already asked them to pay, and this email would replace its
				# set-password link (Ruling 17). Counted as sent.
				self._record(sub, [s for s in stages if s <= stage])
			elif stage is not None:
				final = stage == self.final
				dates = {
					"due": start,
					"suspend": self._suspends_on(sub, start, self.today if final else None),
				}
				if self._email(sub, tenant, account, kind, stage, dates):
					self._record(sub, [s for s in stages if s <= stage], final_warning=final)
		if offset >= 0 and sub.status in ("active", "trialing"):
			self._past_due(sub, f"day 0 was {start}")
		if sub.status != "past_due":
			return
		on = suspension_day(
			start, self.suspend_after, self.final, _date(sub.final_warning_on), _date(sub.grace_until)
		)
		if on is None:
			# Never suspended before the final warning has gone out. With suspend_sites off: the day it
			# would be if suspension went live today (a fresh final notice, then the notice period).
			if not self.suspend_sites:
				self._would(sub, f"suspend on {self._planned(sub, start, None)} (suspend_sites is off)")
			return
		if self.today < on:
			if self.dry or not self.suspend_sites:
				self._would(sub, f"suspend on {on}")
			return
		if self._may_suspend(sub, on, "unpaid after the final warning") and self._suspend(
			sub, on, "unpaid after the final warning"
		):
			if self.send_emails:
				self._email(sub, tenant, account, "suspended", None, {"due": start})

	def _trial_pause(self, sub, start: date, warned: date) -> date:
		"""The day a trial's site is paused: the day after the trial's last day or after its last reminder
		(`warned`), whichever is later, and not before `grace_until`."""
		pause = max(start, warned) + timedelta(days=1)
		grace = _date(sub.grace_until)
		return max(pause, grace) if grace else pause

	def _paid_kind(self, sub, stage: int) -> str:
		if stage < 0:
			return "paid_reminder"
		if stage == 0:
			return "paid_expired" if sub.current_period_end else "first_due"  # never paid: the first payment
		return "paid_final" if stage == self.final else "paid_warning"

	def _suspends_on(self, sub, start: date, final_warning_on: date | None) -> date | None:
		"""The suspension day to tell the customer (`_planned`), or None while suspend_sites is off: no day
		is promised until suspension is live."""
		return self._planned(sub, start, final_warning_on) if self.suspend_sites else None

	def _planned(self, sub, start: date, final_warning_on: date | None) -> date:
		"""The suspension day: the real one once the final warning is out, else the one it will be if the
		final warning goes on its day (or today, if that is already past)."""
		warned = _date(sub.final_warning_on)
		sent = final_warning_on or (warned if warned and warned >= start else None)  # (an older one is stale)
		planned = max(self.today, start + timedelta(days=self.final))
		return suspension_day(start, self.suspend_after, self.final, sent or planned, _date(sub.grace_until))

	def _cancel_at_period_end(self, sub, tenant, account, start: date):
		"""Cancelled at period end (Ruling 5): on day 0 one notice, status `canceled`, and the site suspended
		if the notice went out. Never archived. While suspend_sites is off nothing changes (Ruling 15): the
		notice, the cancel and the suspension go together once it is on."""
		reason = "cancelled at period end"
		if not self.suspend_sites:
			self._would(sub, f"cancel and suspend ({reason} on {start}; suspend_sites is off)")
			return
		notified = 0 in parse_sent(sub.notified_stages)
		if (notified or self.send_emails) and self._held("suspended", self.max_suspensions):
			return  # the cancel waits for the suspension, next run
		if self.send_emails and not notified:
			if not self._email(sub, tenant, account, "canceled", 0, {"due": start, "suspend": self.today}):
				return  # nothing changes until the notice is out: retried next run
			self._record(sub, [0])
			notified = True
		if notified:
			if not self._suspend(sub, self.today, reason):
				return  # Press refused: still active, retried next run (the notice is not sent again)
		else:
			self._would(sub, f"suspend on {self.today} ({reason}; no notice was sent)")
		self._set_canceled(sub)

	def _set_canceled(self, sub):
		if self.dry:
			self._would(sub, "cancel (cancel_at_period_end)")
			sub.status = "canceled"
			return
		sub.reload()  # suspend_subscription may have written the row
		sub.status = "canceled"
		sub.archive_due_on = None
		sub.save(ignore_permissions=True)  # on_update keeps the account's status in step
		audit("renewal.cancel", "GX Subscription", sub.name, "canceled at period end, as the customer asked")
		self.checkpoint(sub)

	# ------------------------------------------------------------ GrowthX trials

	def _trial(self, sub, tenant, account, start: date, offset: int):
		reminders = sorted(-d for d in self.trial_days)
		last = reminders[-1]
		# The last reminder is never sent before its day: an earlier one belongs to an earlier trial end.
		self._drop_stale_warnings(sub, start + timedelta(days=last))
		sent = parse_sent(sub.notified_stages)
		if self.suspend_sites and not sub.final_warning_on:
			# The last reminder went while suspend_sites was off, without a pause day: it goes again with one.
			sent.discard(last)
		planned = max(self.today, start + timedelta(days=last))
		if self.send_emails:
			stage = stage_to_send(reminders, offset, sent)
			if stage is not None:
				# The last reminder is the final warning only once it can name the pause day (suspend_sites).
				final = stage == last and self.suspend_sites
				warned = self.today if final else _date(sub.final_warning_on)
				pause = self._trial_pause(sub, start, warned or planned) if self.suspend_sites else None
				if self._email(
					sub, tenant, account, "trial_reminder", stage, {"due": start, "suspend": pause}
				):
					self._record(sub, [s for s in reminders if s <= stage], final_warning=final)
		# Paused from the day after the trial's last day, never less than a day after the last reminder, and
		# never before an admin's extra days (grace_until) run out.
		warned = _date(sub.final_warning_on)
		if not warned:
			if not self.suspend_sites and offset >= last:
				self._would(
					sub, f"suspend on {self._trial_pause(sub, start, planned)} (suspend_sites is off)"
				)
			return
		on = self._trial_pause(sub, start, warned)
		if self.today < on:
			if self.dry or not self.suspend_sites:
				self._would(sub, f"suspend on {on}")
			return
		if not self._may_suspend(sub, on, "trial ended"):
			return
		if self.send_emails and 0 not in parse_sent(sub.notified_stages):
			if not self._email(sub, tenant, account, "trial_ended", 0, {"due": start}):
				return  # the site is paused once the customer has been told: retried next run
			self._record(sub, [0])
		self._suspend(sub, on, "trial ended")

	def _paused_trial(self, sub, tenant, account):
		"""A never-paid GrowthX trial site the job paused: a warning from `archive_due_on` minus the warning
		days, then the archive, no sooner than `archive_due_on` and the full warning period after the warning
		actually went out (`archive_warning_on`)."""
		due = _date(sub.archive_due_on)
		if (
			sub.suspended_by != "billing"
			or tenant.origin == "imported"
			or tenant.status != "suspended"
			or cint(sub.cancel_at_period_end)  # cancelled at period end: never archived
			or not due
			or not never_paid(sub.account)
		):
			return
		notice = timedelta(days=self.archive_warning_days)
		warned = _date(sub.archive_warning_on)
		ended = _date(sub.trial_ends_at) or _date(sub.suspended_at) or due
		if not self.suspend_sites:
			if self.today >= due:
				self._would(
					sub, f"archive on {max(due, (warned or self.today) + notice)} (suspend_sites is off)"
				)
			return
		if not warned:
			if self.today < due - notice:
				return
			on = max(due, self.today + notice)
			if not self.send_emails:
				if self.today >= due:
					self._would(sub, f"archive on {on} (no deletion warning: send_emails is off)")
				return
			if self._email(
				sub, tenant, account, "trial_archive_warning", None, {"due": ended, "archive": on}
			):
				self._record(sub, archive_warning=True)
				if self.dry:
					self._would(sub, f"archive on {on}")
			return
		on = max(due, warned + notice)
		if self.today < on:
			if self.dry:
				self._would(sub, f"archive on {on}")
			return
		if self._held("archived", self.max_archives):
			return
		if self.dry:
			self._would(sub, f"archive on {on}")
		elif not self._unchanged(sub) or not archive_trial(sub.name):
			return
		else:
			sub.status = "canceled"
			self.checkpoint(sub)
		self.counts["archived"] += 1
		if self.send_emails:
			self._email(sub, tenant, account, "trial_archived", None, {"due": ended})

	def archive_on(self, sub, tenant) -> date | None:
		"""The day a paused trial's site will be deleted: no sooner than `archive_due_on` and the full warning
		period after the deletion warning (or, if it isn't out yet, after today). None where the job never
		archives (not a never-paid GrowthX trial the job paused), and while suspend_sites is off."""
		due = _date(sub.archive_due_on)
		if (
			not self.suspend_sites
			or sub.suspended_by != "billing"
			or not due
			or (tenant and tenant.origin == "imported")
			or cint(sub.cancel_at_period_end)
			or not never_paid(sub.account)
		):
			return None
		notice = timedelta(days=self.archive_warning_days)
		return max(due, (_date(sub.archive_warning_on) or self.today) + notice)

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
			sub.status = "canceled"
			self.checkpoint(sub)
			if self.send_emails:
				ended = _date(sub.trial_ends_at) or _date(sub.suspended_at) or due
				self._email(sub, tenant, account, "trial_archived", None, {"due": ended})

	# ------------------------------------------------------------ actions (dry_run: audit lines only)

	def _resume(self, sub, tenant) -> bool:
		"""Press refused to bring the site back after a payment or a Reactivate (`resume_pending`). Try again
		(Rulings 13, 19); True once it is back."""
		if self.dry:
			self._would(sub, f"resume {tenant.name} (Press had refused it)")
			return False
		resume_subscription_site(sub.name, "by the renewal job (Press had refused it)")
		if frappe.db.get_value("GX Tenant", tenant.name, "status") == "suspended":
			return False  # refused again (logged, still pending): tried again next run
		tenant.status = "ready"
		sub.resume_pending = 0  # as stored: a later save of `sub` must not mark it pending again
		self.checkpoint(sub)
		return True

	def _drop_stale_warnings(self, sub, earliest: date):
		"""A final warning dated before it could have been sent belongs to an earlier timeline (an admin
		moved the trial end or the period): forget the stages sent so the new timeline warns again."""
		warned = _date(sub.final_warning_on)
		if not warned or warned >= earliest:
			return
		values = {"notified_stages": None, "final_warning_on": None}
		if self.dry:
			sub.update(values)
		else:
			sub.db_set(values, update_modified=False)

	def _email(self, sub, tenant, account, kind: str, stage: int | None, dates: dict) -> bool:
		if self.dry:
			self._would(sub, f"email {kind}" + ("" if stage is None else f" (day {stage:+d})"))
		elif not renewal_emails.send(sub, tenant, account, kind, stage, {"today": self.today, **dates}):
			return False
		self.counts["emails"] += 1
		return True

	def _record(self, sub, stages=(), final_warning: bool = False, archive_warning: bool = False):
		"""Note what was sent (an email that failed is not noted, so it is tried again next run)."""
		values = {}
		if stages:
			values["notified_stages"] = format_sent(parse_sent(sub.notified_stages) | set(stages))
		if final_warning:
			values["final_warning_on"] = self.today
		if archive_warning:
			values["archive_warning_on"] = self.today
		if self.dry:
			sub.update(values)
			return
		if not self._unchanged(sub):
			raise _Changed  # paid meanwhile: the old timeline's record must not land on the new period
		sub.db_set(values, update_modified=False)
		self.checkpoint(sub)

	def _past_due(self, sub, details: str):
		if self.dry:
			self._would(sub, f"set past_due ({details})")
			sub.status = "past_due"
		else:
			sub.reload()  # the whole row is saved: never write back what an admin changed meanwhile
			sub.status = "past_due"
			sub.save(ignore_permissions=True)  # on_update keeps the account's status in step
			audit("renewal.past_due", "GX Subscription", sub.name, details)
			self.checkpoint(sub)
		self.counts["past_due"] += 1

	def _may_suspend(self, sub, on: date, reason: str) -> bool:
		"""The subscription is due for suspension: False (with a `renewal.would` line) while suspend_sites is
		off, or (held) when this run's cap is reached."""
		if not self.suspend_sites:
			self._would(sub, f"suspend on {on} ({reason}; suspend_sites is off)")
			return False
		return not self._held("suspended", self.max_suspensions)

	def _suspend(self, sub, on: date, reason: str) -> bool:
		if self.dry:
			self._would(sub, f"suspend on {on} ({reason})")
		elif not self._unchanged(sub):
			raise _Changed  # paid (or changed by an admin) meanwhile: the next run looks at it afresh
		elif not suspend_subscription(sub.name):
			return False  # Press refused: logged; retried next run, and not counted against the cap
		else:
			sub.status = "suspended"
			self.checkpoint(sub)
		self.counts["suspended"] += 1
		return True

	def _held(self, count: str, cap: int) -> bool:
		"""True if this run's cap is reached: the subscription waits for the next run."""
		if self.counts[count] < cap:
			return False
		self.counts["held"] += 1
		return True

	def _would(self, sub, what: str):
		"""A `renewal.would` line, at most once a day per subscription (the job runs hourly)."""
		details = f"{self.today}: would {what}"
		if not frappe.db.exists(
			"GX Audit Log", {"action": "renewal.would", "reference_name": sub.name, "details": details}
		):
			audit("renewal.would", "GX Subscription", sub.name, details)


# ---------------------------------------------------------------- what the customer sees


def was_trial(sub) -> bool:
	"""A GrowthX subscription that never paid and has a trial end: a trial, paused or not."""
	return bool(sub.trial_ends_at) and not sub.current_period_end and not sub.get("last_payment_id")


def renewal_info(sub, tenant=None, today: date | None = None, _job: _Run | None = None) -> dict | None:
	"""Where the subscription is on its renewal timeline, computed with the job's own rules (the same day 0,
	reminder window, suspension day and archive day), so the website renders it without date logic of its own.
	None for a lifetime plan that is paid, for a canceled subscription and for one marked not billed.

	`stage`: paid, due_soon, first_due, expired, suspended, trial, trial_ended. `daysLeft`: whole days to the
	next deadline (the end for paid and trial, the suspension day once expired, the deletion day for a paused
	trial; 0 when there is none). `expiresAt`, `suspendsAt`, `archivesAt`: calendar days (IST) as ISO dates or
	None; `suspendsAt` and `archivesAt` are None while suspend_sites is off. `canPay` is False only in `paid`
	(outside the reminder window); `payInAdvance` is True there. A suspended subscription with no dates at
	all is still `suspended` (with no dates)."""
	if cint(sub.get("not_billed")):
		return None
	if sub.status not in LIVE or (sub.billing_interval == "one_time" and sub.status == "active"):
		return None
	job = _job or new_job()
	if today:
		job.today = today
	if tenant is None and sub.tenant:
		tenant = frappe.db.get_value("GX Tenant", sub.tenant, ["name", "status", "origin"], as_dict=True)
	start = job.start(sub, tenant)
	imported = bool(tenant and tenant.origin == "imported")
	paused = sub.status == "suspended" or bool(tenant and tenant.status == "suspended")
	if start is None and not paused:
		return None
	cancelled = bool(cint(sub.cancel_at_period_end))
	archives = suspends = None
	can_pay, advance = True, False
	if paused:
		# (start is None only for a suspended subscription made before due dates were stored)
		stage, days = "suspended", 0
		if start and not imported and was_trial(sub):
			stage = "trial_ended"
			archives = job.archive_on(sub, tenant)
			days = max((archives - job.today).days, 0) if archives else 0
	elif sub.status == "trialing" and not imported:
		offset = (job.today - start).days
		reminders = sorted(-d for d in job.trial_days)
		warned = _date(sub.final_warning_on)
		planned = max(job.today, start + timedelta(days=reminders[-1]))
		if job.suspend_sites:
			suspends = job._trial_pause(sub, start, warned or planned)
		stage = "trial" if offset <= 0 else "trial_ended"
		days = -offset if offset <= 0 else _days_to(suspends, job.today)
	elif job.today < start:
		# Inside the reminder window (7 days; a yearly plan's 30-day reminder is an email only), or paid up.
		offset = (job.today - start).days
		stage = "due_soon" if -offset <= max(job.reminder_days) else "paid"
		days, can_pay, advance = -offset, stage != "paid", stage == "paid"
		if stage == "due_soon" and not cancelled:
			suspends = job._suspends_on(sub, start, None)
	else:
		stage = "first_due" if not sub.current_period_end else "expired"
		if not cancelled:
			suspends = job._suspends_on(sub, start, None)
		days = _days_to(suspends, job.today)
	if cancelled and not paused:
		# Cancelled at period end: no grace; the job suspends the site on day 0 (if suspend_sites is on).
		suspends = start if job.suspend_sites else None
	return {
		"stage": stage,
		"daysLeft": days,
		"expiresAt": iso(start),
		"suspendsAt": iso(suspends),
		"archivesAt": iso(archives),
		"canPay": can_pay,
		"payInAdvance": advance,
	}


def _days_to(day: date | None, today: date) -> int:
	return max((day - today).days, 0) if day else 0
