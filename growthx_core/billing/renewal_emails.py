"""Renewal emails: one per stage of the renewal timeline. growthx_core.billing.renewals decides which one is
due and when; this module only writes and sends it.

Every email names the account, the site, the plan and its price, and the date that matters, and links to the
customer's billing page. While online payment is off (GX Billing Settings.online_payments, which mirrors the
website's switch), it asks for a bank transfer instead of offering Pay now. The wording is a first draft
for Sammish to approve.
"""

from datetime import date

import frappe
from frappe.utils import cint

from growthx_core import accounts, direct
from growthx_core.api.v1._core import audit, web_url
from growthx_core.billing import payments

KINDS = (
	"paid_reminder",
	"paid_expired",
	"paid_warning",
	"paid_final",
	"first_due",
	"suspended",
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
	if kind != "trial_archived":
		paragraphs.append(_how_to_pay(billing))
	if kind == "first_due" and not accounts.has_password(account.user):
		link = direct.issue_password_link(account.user)
		paragraphs.append(
			f'You have not set a password yet: <a href="{esc(link)}">set it here</a> to sign in'
			" (the link works once and expires in 24 hours)."
		)
	paragraphs.append(f'Your billing page: <a href="{esc(billing)}">{esc(billing)}</a>')
	html = f"<p>Hello {esc(account.account_name)},</p>" + "".join(f"<p>{p}</p>" for p in paragraphs)
	if not direct.send_email(account.contact_email, subject, html):
		return False
	where = "" if stage is None else f" (day {stage:+d})"
	audit("renewal.email", "GX Subscription", sub.name, f"{kind}{where} to {account.contact_email}")
	return True


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


def _how_to_pay(billing: str) -> str:
	esc = frappe.utils.escape_html
	if cint(frappe.db.get_single_value("GX Billing Settings", "online_payments", cache=False)):
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
	suspend = _day(dates["suspend"])
	return (
		f"Your GrowthX plan for {site} has expired",
		[
			f"Your plan for {what} expired on <b>{_day(dates['due'])}</b>."
			f" Renew before <b>{suspend}</b> to keep your site: if it is still unpaid then, the site is"
			" suspended. Your data stays safe.",
		],
	)


def _first_due(sub, site, what, dates):
	due, suspend = dates["due"], _day(dates["suspend"])
	when = "today" if due >= dates["today"] else f"on <b>{_day(due)}</b>"
	return (
		f"Your GrowthX payment for {site} is due",
		[
			f"Your payment for {what} is due {when}."
			f" Please pay before <b>{suspend}</b> to keep your site running: if it is still unpaid then, the site is"
			" suspended. Your data stays safe.",
		],
	)


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


def _trial_reminder(sub, site, what, dates):
	due, pause = dates["due"], _day(dates["suspend"])
	n = (due - dates["today"]).days
	if n > 0:
		subject = f"Your GrowthX trial ends in {_in_days(n)}"
		first = f"Your free trial of {what} ends on <b>{_day(due)}</b>, in {_in_days(n)}."
	else:
		subject = "Your GrowthX trial has ended"
		first = f"Your free trial of {what} {'ends today' if n == 0 else f'ended on <b>{_day(due)}</b>'}."
	return (
		subject,
		[
			first,
			f"Choose a plan and pay to keep your site running: from <b>{pause}</b> it is paused until you do. Your data stays safe.",
		],
	)


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
	"trial_reminder": _trial_reminder,
	"trial_ended": _trial_ended,
	"trial_archive_warning": _trial_archive_warning,
	"trial_archived": _trial_archived,
}
