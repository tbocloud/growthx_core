"""Applying a payment to a customer: shared by admin.record_payment (a bank transfer an admin
records) and online checkout (growthx_core.billing.payu), so both behave the same way.

The customer's subscription for the plan's product becomes active on that plan for one billing
period, and the account becomes active (a suspended customer's sites come back). Where the period
starts (growthx-infra docs/renewals-design.md 5.1, `_period_start`):
- a renewal paid early continues from the current period end;
- a renewal paid in grace (`past_due`) continues from the old end, so no days are free or lost;
- a renewal paid after a suspension (also one restored by Give extra days) starts on the payment day;
- a first payment starts on its due date (ERPNext Direct "due now" or "billing starts later"), else on
  the payment day.
A period that would already be over on the payment day (a payment months late) starts on the payment
day instead, and the audit details say so. A one-time (lifetime) plan has no period end and can't be
paid again.
"""

from datetime import date

import frappe
from frappe.utils import add_months, add_years, cint, flt, fmt_money, getdate

from growthx_core.api.v1._core import ApiError, validation_error
from growthx_core.billing import locks, renewals
from growthx_core.partners import commissions
from growthx_core.provisioning import runner


def _subscription_for(account, plan) -> str:
	if account.status not in ("trial", "suspended", "active"):
		raise ApiError(
			409, "invalid_transition", f"A customer who is {account.status} can't have a payment recorded."
		)
	subs = frappe.get_all(
		"GX Subscription",
		{"account": account.name, "product": plan.product},
		pluck="name",
		order_by="creation desc",
		limit=1,
	)
	if not subs:
		raise validation_error({"plan": "This customer has no subscription for this plan's product."})
	return subs[0]


def _refuse_lifetime_repeat(status: str, interval: str):
	if status == "active" and interval == "one_time":
		raise ApiError(409, "invalid_transition", "This customer has already paid for lifetime access.")


def check_payable(account, plan) -> str:
	"""The subscription a payment for `plan` would apply to, or an ApiError saying why it can't be
	applied. Checked before taking money online; apply_payment checks it again."""
	name = _subscription_for(account, plan)
	_refuse_lifetime_repeat(*frappe.db.get_value("GX Subscription", name, ["status", "billing_interval"]))
	return name


def _period_start(sub, paid_on: date) -> date:
	"""Where the paid period begins. A renewal paid in time or in grace continues from the old end (no
	free days, and none lost); after a suspension it begins on the payment day (the site couldn't be
	used). A `past_due` subscription with `suspended_at` was suspended and then given extra days: it
	counts as suspended. A first payment begins on the due date (billing start) when there is one, even
	if paid after it. apply_payment moves a start whose period would already be over to the payment day."""
	end = getdate(sub.current_period_end) if sub.current_period_end else None
	due = getdate(sub.due_date) if sub.get("due_date") else None
	if sub.status == "active" and end:
		return max(end, paid_on)
	if sub.status == "suspended" or (sub.status == "past_due" and sub.get("suspended_at")):
		return paid_on if end else due or paid_on
	if sub.status == "past_due":
		return end or due or paid_on
	if sub.status == "trialing" and due and _is_imported(sub):
		return due
	return paid_on


def _period_end(start: date, interval: str) -> date | None:
	if interval == "one_time":
		return None  # lifetime: never expires or renews
	return getdate(add_years(start, 1) if interval == "year" else add_months(start, 1))


def _is_imported(sub) -> bool:
	return bool(sub.tenant) and frappe.db.get_value("GX Tenant", sub.tenant, "origin") == "imported"


def apply_payment(
	account,
	plan,
	reference: str,
	paid_on: date,
	gateway: str,
	amount=None,
	sales_invoice: str | None = None,
	*,
	fresh: bool = True,
):
	"""`account` is the GX Account document (locked by the caller), `plan` the GX Plan document,
	`amount` what the customer paid in rupees, GST included (the plan's price when not given).
	Returns (subscription, details for the audit line, period end or None for lifetime).
	`fresh`: the caller locked the account with locks.lock_account (a fresh transaction), so plain reads
	are current. False for one that couldn't (PayU's `_activate`): the account's sites and subscriptions
	are then read by name under lock (locks.account_rows).

	If a partner referred the customer, the payment also earns that partner a commission (one per
	payment, see growthx_core.partners.commissions); a problem there never stops the activation."""
	paid_on = getdate(paid_on)
	sub = frappe.get_doc("GX Subscription", _subscription_for(account, plan), for_update=True)
	previous_plan = sub.plan
	if sub.last_payment_id == reference:
		raise ApiError(409, "duplicate_payment", "A payment with this reference is already recorded.")
	_refuse_lifetime_repeat(sub.status, sub.billing_interval)
	start = _period_start(sub, paid_on)
	period_end = _period_end(start, plan.billing_interval)
	late = None
	if period_end and period_end <= paid_on:
		# Paid so late that the period from `start` is already over: it would be expired the moment it is
		# paid. It starts on the payment day instead.
		late = f"period from {paid_on.isoformat()} (one from {start.isoformat()} would already have ended)"
		period_end = _period_end(paid_on, plan.billing_interval)
	sub.update(
		{
			"plan": plan.name,
			"billing_interval": plan.billing_interval,
			"price": plan.price,
			"gst_included": plan.gst_included,
			"gst_rate": plan.gst_rate,
			"status": "active",
			"trial_ends_at": None,
			"current_period_end": period_end,
			"cancel_at_period_end": 0,
			"gateway": gateway,
			"gateway_subscription_id": None,
			"last_payment_id": reference,
			"due_date": None,
			"notified_stages": None,
			"grace_until": None,
			"final_warning_on": None,
			"suspended_at": None,
			"suspended_by": None,
			"archive_due_on": None,
			"archive_warning_on": None,
		}
	)
	sub.save(ignore_permissions=True)
	renewals.resume_subscription_site(sub.name)
	if account.status == "active" and previous_plan != plan.name:
		old_name = frappe.db.get_value("GX Plan", previous_plan, "plan_name") or previous_plan
		details = f"plan change, {old_name} → {plan.plan_name}"
	elif account.status == "active":
		details = "renewal"
	else:
		# Another subscription's site that billing suspended stays suspended until that one is paid.
		unpaid = [
			r.tenant
			for r in locks.account_rows(
				"GX Subscription", {"account": account.name}, ["suspended_by", "tenant"], fresh=fresh
			)
			if r.suspended_by == "billing" and r.name != sub.name
		]
		details = set_customer_status(
			account,
			"active",
			"suspended",
			"ready",
			"resume_site",
			keep=set(filter(None, unpaid)),
			fresh=fresh,
		)
	# After the status change: the customer now counts as an active client for the partner's tier.
	commissions.record_for_payment(
		account,
		plan,
		reference,
		paid_on,
		gross_amount=amount,
		gateway=gateway,
		sales_invoice=sales_invoice,
		subscription=sub.name,
	)
	if late:
		details += f"; {late}"
	return sub, details, period_end


def plan_price(plan) -> str:
	"""e.g. "₹ 1,499.00 + 18% GST", so a mistyped amount stands out in the audit line."""
	text = fmt_money(plan.price, currency="INR")
	if cint(plan.gst_included):
		return f"{text} incl. GST"
	return f"{text} + {flt(plan.gst_rate):g}% GST" if flt(plan.gst_rate) else text


def set_customer_status(
	account,
	new_status: str,
	site_from: str,
	site_to: str,
	press_action: str,
	keep: set | None = None,
	*,
	fresh: bool = True,
) -> str:
	"""Change the account's status and move its sites from `site_from` to `site_to` on Press, except the
	tenants in `keep`. A site Press refuses keeps its status and is named in the audit details; a resume
	it refuses is marked `resume_pending`, which the renewal job retries. Suspending the account clears
	every such mark (the admin's suspension stands), and a site moved back to `ready` has nothing to retry.
	`fresh` as for apply_payment: False reads the sites and marks by name under lock."""
	previous = account.status
	account.status = new_status
	account.save(ignore_permissions=True)
	if new_status == "suspended":
		renewals.clear_resume_pending({"account": account.name}, fresh=fresh)
	client = None
	failed = []
	for t in locks.account_rows(
		"GX Tenant", {"account": account.name}, ["status", "press_site_id", "origin"], fresh=fresh
	):
		if t.status != site_from or (keep and t.name in keep):
			continue
		try:
			# An imported (ERPNext Direct) site is the customer's own: its status moves, Press isn't asked.
			if t.press_site_id and t.origin != "imported":
				client = client or runner.get_client()
				getattr(client, press_action)(t.press_site_id)
		except Exception:
			frappe.log_error(title=f"GrowthX: {press_action} failed for {t.name}")
			failed.append(t.name)
			if site_to == "ready":
				renewals.mark_resume_pending(t.name, fresh=fresh)  # a refused resume: the job tries again
			continue
		frappe.db.set_value("GX Tenant", t.name, "status", site_to)
		if site_to == "ready":
			renewals.clear_resume_pending({"tenant": t.name}, fresh=fresh)
	details = f"{previous} → {new_status}"
	if failed:
		details += f" (sites not changed, Press error: {', '.join(failed)})"
	return details
