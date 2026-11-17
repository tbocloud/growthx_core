"""Applying a payment to a customer: shared by admin.record_payment (a bank transfer an admin
records) and online checkout (growthx_core.billing.payu), so both behave the same way.

The customer's subscription for the plan's product becomes active on that plan for one billing
period, and the account becomes active (a suspended customer's sites come back). The period runs
from the payment date, or for a renewal from the current period end if that's later. A one-time
(lifetime) plan has no period end and can't be paid again.
"""

from datetime import date

import frappe
from frappe.utils import add_months, add_years, cint, flt, fmt_money, getdate

from growthx_core.api.v1._core import ApiError, validation_error
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


def apply_payment(account, plan, reference: str, paid_on: date, gateway: str):
	"""`account` is the GX Account document (locked by the caller), `plan` the GX Plan document.
	Returns (subscription, details for the audit line, period end or None for lifetime)."""
	sub = frappe.get_doc("GX Subscription", _subscription_for(account, plan), for_update=True)
	previous_plan = sub.plan
	if sub.last_payment_id == reference:
		raise ApiError(409, "duplicate_payment", "A payment with this reference is already recorded.")
	_refuse_lifetime_repeat(sub.status, sub.billing_interval)
	start = paid_on
	if sub.status == "active" and sub.current_period_end:
		start = max(getdate(sub.current_period_end), paid_on)
	if plan.billing_interval == "one_time":
		period_end = None  # lifetime: never expires or renews
	else:
		period_end = add_years(start, 1) if plan.billing_interval == "year" else add_months(start, 1)
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
		}
	)
	sub.save(ignore_permissions=True)
	if account.status == "active" and previous_plan != plan.name:
		old_name = frappe.db.get_value("GX Plan", previous_plan, "plan_name") or previous_plan
		details = f"plan change, {old_name} → {plan.plan_name}"
	elif account.status == "active":
		details = "renewal"
	else:
		details = set_customer_status(account, "active", "suspended", "ready", "resume_site")
	return sub, details, period_end


def plan_price(plan) -> str:
	"""e.g. "₹ 1,499.00 + 18% GST", so a mistyped amount stands out in the audit line."""
	text = fmt_money(plan.price, currency="INR")
	if cint(plan.gst_included):
		return f"{text} incl. GST"
	return f"{text} + {flt(plan.gst_rate):g}% GST" if flt(plan.gst_rate) else text


def set_customer_status(account, new_status: str, site_from: str, site_to: str, press_action: str) -> str:
	"""Change the account's status and move its sites from `site_from` to `site_to` on Press. A site
	Press refuses keeps its status and is named in the audit details."""
	previous = account.status
	account.status = new_status
	account.save(ignore_permissions=True)
	client = None
	failed = []
	for t in frappe.get_all(
		"GX Tenant", {"account": account.name, "status": site_from}, ["name", "press_site_id"]
	):
		try:
			if t.press_site_id:
				client = client or runner.get_client()
				getattr(client, press_action)(t.press_site_id)
		except Exception:
			frappe.log_error(title=f"GrowthX: {press_action} failed for {t.name}")
			failed.append(t.name)
			continue
		frappe.db.set_value("GX Tenant", t.name, "status", site_to)
	details = f"{previous} → {new_status}"
	if failed:
		details += f" (sites not changed, Press error: {', '.join(failed)})"
	return details
