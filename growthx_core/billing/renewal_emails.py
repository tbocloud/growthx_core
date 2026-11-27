"""Renewal emails: one per stage of the renewal timeline. growthx_core.billing.renewals decides which one is
due and when; this module only writes and sends it.

Every email names the account, the site, the plan and its price, and the date that matters, and links to the
customer's billing page. Emails are queued (Email Queue sends them and retries delivery), so "sent" means
queued without an error. Pay now (PayU) is offered only while online payment is on (GX Billing
Settings.online_payments, which mirrors the website's switch) and for a plan whose price includes GST (online
invoices need it); otherwise the email asks for a bank transfer. Without a `suspend` date (suspend_sites
off) no email names a suspension or pause day. The wording is a first draft for Sammish to approve.
"""

from datetime import date, timedelta

import frappe
from frappe.utils import cint, flt, get_datetime, now_datetime, sha256_hash

from growthx_core import accounts
from growthx_core.api.v1._core import audit, web_url
from growthx_core.billing import payments

KINDS = (
	"paid_reminder",
	"paid_expired",
	"paid_warning",
	"paid_final",
	"first_due",
	"suspended",
	"canceled",
	"trial_reminder",
	"trial_ended",
	"trial_archive_warning",
	"trial_archived",
)


def send(sub, tenant, account, kind: str, stage: int | None, dates: dict) -> bool:
	"""Email the account about its subscription `sub` (site `tenant`). `dates` has `today` and, as the kind
	needs them, `due` (day 0: the end date, the first due date or the trial's last day), `suspend` (the day
	the site is suspended or paused) and `archive` (the day a paused trial site is deleted). True if sent;
	the email is then audited."""
	if kind not in KINDS:
		raise ValueError(f"unknown renewal email: {kind}")
	esc = frappe.utils.escape_html
	plan = frappe.get_doc("GX Plan", sub.plan)
	site = _site(tenant)
	billing = web_url("/customer/billing")
	what = f"<b>{esc(site)}</b> ({esc(plan.plan_name)}, {esc(payments.plan_price(plan))})"
	subject, paragraphs = _CONTENT[kind](sub, site, what, dates)
	# Nothing to pay for a deleted site, or for a cancelled plan (it can't be paid: _canceled says whom to ask).
	if kind not in ("trial_archived", "canceled"):
		paragraphs.append(_how_to_pay(billing, plan))
	key = None
	if kind == "first_due" and not accounts.has_password(account.user):
		if _recent_password_key(account.user):
			# A new key would kill the link sent less than a day ago (the invite's, say): point to that one.
			forgot = web_url("/forgot-password")
			paragraphs.append(
				"You have not set a password yet: use the link in our earlier email, or"
				f' <a href="{esc(forgot)}">Forgot password</a> to get a new one.'
			)
		else:
			key = frappe.generate_hash()
			link = web_url(f"/reset-password?key={key}")
			paragraphs.append(
				f'You have not set a password yet: <a href="{esc(link)}">set it here</a> to sign in'
				" (the link works once and expires in 24 hours)."
			)
	paragraphs.append(f'Your billing page: <a href="{esc(billing)}">{esc(billing)}</a>')
	html = f"<p>Hello {esc(account.account_name)},</p>" + "".join(f"<p>{p}</p>" for p in paragraphs)
	if not _queue(account.contact_email, subject, html, sub.name):
		return False
	if key:
		_store_password_key(account.user, key)  # only now: an email that failed leaves the old link working
	where = "" if stage is None else f" (day {stage:+d})"
	audit("renewal.email", "GX Subscription", sub.name, f"{kind}{where} to {account.contact_email}")
	return True


def _queue(email: str, subject: str, html: str, sub_name: str) -> bool:
	"""Queue the email (not sent inline, so no commit happens mid-job). False if it could not be queued."""
	try:
		frappe.sendmail(
			recipients=[email],
			subject=subject,
			message=html,
			reference_doctype="GX Subscription",
			reference_name=sub_name,
			now=False,
		)
		return True
	except Exception:
		frappe.log_error(title="GrowthX: could not queue a renewal email")
		return False


def _recent_password_key(user: str) -> bool:
	"""True if the user has a set-password key issued less than 24 hours ago (still usable)."""
	row = frappe.db.get_value(
		"User", user, ["reset_password_key", "last_reset_password_key_generated_on"], as_dict=True
	)
	issued = row and row.reset_password_key and row.last_reset_password_key_generated_on
	return bool(issued) and now_datetime() - get_datetime(issued) < timedelta(hours=24)


def _store_password_key(user: str, key: str):
	"""The same single-use key growthx_core.direct.issue_password_link stores (hashed; 24 hours)."""
	frappe.db.set_value(
		"User",
		user,
		{"reset_password_key": sha256_hash(key), "last_reset_password_key_generated_on": now_datetime()},
	)


def _site(tenant) -> str:
	return (
		(tenant.site_url or tenant.display_name or tenant.name)
		.removeprefix("https://")
		.removeprefix("http://")
	)


def _day(value: date) -> str:
	"""26 Dec 2026"""
	return f"{value.day} {value:%b %Y}"


def _in_days(n: int) -> str:
	return "1 day" if n == 1 else f"{n} days"


def payable_online(plan) -> bool:
	"""PayU needs a GST-inclusive price (billing.tax_invoice.apply refuses a "+ GST" one)."""
	return not (flt(plan.gst_rate) and not cint(plan.gst_included))


def _how_to_pay(billing: str, plan) -> str:
	esc = frappe.utils.escape_html
	online = cint(frappe.db.get_single_value("GX Billing Settings", "online_payments", cache=False))
	if online and payable_online(plan):
		return (
			f'<a href="{esc(billing)}">Open your billing page</a>, sign in and press <b>Pay now</b>.'
			" You pay securely on PayU with a card, UPI or net banking."
		)
	from growthx_core.support import support_inbox

	inbox = support_inbox()
	return (
		"Please pay by bank transfer: write to"
		f' <a href="mailto:{esc(inbox)}">{esc(inbox)}</a> for our bank details, and we record your payment'
		" as soon as it arrives."
	)


def _expired_or_due(sub, what: str, due: date) -> str:
	if sub.current_period_end:
		return f"Your plan for {what} expired on <b>{_day(due)}</b>."
	return f"Your payment for {what} was due on <b>{_day(due)}</b>."


def _paid_reminder(sub, site, what, dates):
	due, n = dates["due"], (dates["due"] - dates["today"]).days
	return (
		f"Your GrowthX plan for {site} renews in {_in_days(n)}",
		[
			f"Your plan for {what} renews on <b>{_day(due)}</b>, in {_in_days(n)}. Renew now to keep your site running."
		],
	)


def _paid_expired(sub, site, what, dates):
	text = f"Your plan for {what} expired on <b>{_day(dates['due'])}</b>."
	if dates.get("suspend"):
		text += (
			f" Renew before <b>{_day(dates['suspend'])}</b> to keep your site: if it is still unpaid then,"
			" the site is suspended. Your data stays safe."
		)
	else:
		text += " Please renew it to keep your site running."
	return (f"Your GrowthX plan for {site} has expired", [text])


def _first_due(sub, site, what, dates):
	due = dates["due"]
	when = "today" if due >= dates["today"] else f"on <b>{_day(due)}</b>"
	text = f"Your payment for {what} is due {when}."
	if dates.get("suspend"):
		text += (
			f" Please pay before <b>{_day(dates['suspend'])}</b> to keep your site running: if it is still"
			" unpaid then, the site is suspended. Your data stays safe."
		)
	else:
		text += " Please pay to keep your site running."
	return (f"Your GrowthX payment for {site} is due", [text])


def _paid_warning(sub, site, what, dates):
	suspend = _day(dates["suspend"])
	return (
		f"Reminder: pay before {suspend} to keep {site} running",
		[
			_expired_or_due(sub, what, dates["due"]),
			f"Your site will be suspended on <b>{suspend}</b> unless you pay before then. Your data stays safe.",
		],
	)


def _paid_final(sub, site, what, dates):
	suspend = _day(dates["suspend"])
	return (
		f"Final notice: {site} will be suspended on {suspend}",
		[
			_expired_or_due(sub, what, dates["due"]),
			f"This is the final notice: unless you pay, your site is suspended on <b>{suspend}</b>, from 00:00"
			" India time. Your data stays safe, and the site comes back as soon as you pay.",
		],
	)


def _suspended(sub, site, what, dates):
	return (
		f"Your site {site} is suspended",
		[
			_expired_or_due(sub, what, dates["due"]),
			"Because it has not been paid, your site is now suspended. Your data is safe: pay and the site"
			" comes back.",
		],
	)


def _canceled(sub, site, what, dates):
	from growthx_core.support import support_inbox

	esc = frappe.utils.escape_html
	inbox = support_inbox()
	lines = [f"As you asked, your plan for {what} ended on <b>{_day(dates['due'])}</b> and is now cancelled."]
	if dates.get("suspend"):
		lines.append(f"Your site is suspended from <b>{_day(dates['suspend'])}</b>. Your data stays safe.")
	lines.append(
		f'If you change your mind, write to <a href="mailto:{esc(inbox)}">{esc(inbox)}</a> and we will help'
		" you start again."
	)
	return (f"Your GrowthX plan for {site} is cancelled", lines)


def _trial_reminder(sub, site, what, dates):
	due = dates["due"]
	n = (due - dates["today"]).days
	if n > 0:
		subject = f"Your GrowthX trial ends in {_in_days(n)}"
		first = f"Your free trial of {what} ends on <b>{_day(due)}</b>, in {_in_days(n)}."
	else:
		subject = "Your GrowthX trial has ended"
		first = f"Your free trial of {what} {'ends today' if n == 0 else f'ended on <b>{_day(due)}</b>'}."
	second = "Choose a plan and pay to keep your site running"
	if dates.get("suspend"):
		second += f": from <b>{_day(dates['suspend'])}</b> it is paused until you do. Your data stays safe."
	else:
		second += "."
	return (subject, [first, second])


def _trial_ended(sub, site, what, dates):
	return (
		f"Your GrowthX trial has ended and {site} is paused",
		[
			f"Your free trial of {what} ended on <b>{_day(dates['due'])}</b>, and your site is now paused.",
			"Your data is safe: pay to continue where you left off.",
		],
	)


def _trial_archive_warning(sub, site, what, dates):
	archive = _day(dates["archive"])
	return (
		f"Your site {site} will be deleted on {archive}",
		[
			f"Your site {what} is paused: it was not paid for when its trial or plan ended on"
			f" <b>{_day(dates['due'])}</b>.",
			f"Unless you pay before then, it will be deleted on <b>{archive}</b>, and its data can't be"
			" recovered.",
		],
	)


def _trial_archived(sub, site, what, dates):
	return (
		f"Your site {site} has been deleted",
		[
			f"Your site {what} was paused when its trial or plan ended on <b>{_day(dates['due'])}</b>"
			f" without payment, and was deleted on <b>{_day(dates['today'])}</b>, as we wrote before."
			" Its data can't be recovered.",
			"Thank you for trying GrowthX. You are welcome back any time.",
		],
	)


_CONTENT = {
	"paid_reminder": _paid_reminder,
	"paid_expired": _paid_expired,
	"paid_warning": _paid_warning,
	"paid_final": _paid_final,
	"first_due": _first_due,
	"suspended": _suspended,
	"canceled": _canceled,
	"trial_reminder": _trial_reminder,
	"trial_ended": _trial_ended,
	"trial_archive_warning": _trial_archive_warning,
	"trial_archived": _trial_archived,
}
