"""Checkout (Phase 3). Needs Razorpay keys in site_config; until D5 closes, returns 503 without them."""

import frappe
from frappe.utils import add_months, add_years, now_datetime

from growthx_core.api.v1 import _serializers as s
from growthx_core.api.v1._core import (
	ApiError,
	body,
	current_user,
	endpoint,
	get_choice,
	get_str,
	not_found,
	price,
	validation_error,
)
from growthx_core.api.v1.customer import current_account
from growthx_core.billing import gateway

PENDING_TTL = 24 * 60 * 60


def _pending_key(reference: str) -> str:
	return f"gx:checkout:{reference}"


def _unavailable():
	return ApiError(
		503, "payment_unavailable", "Payments are not available right now. Please try again later."
	)


@endpoint(methods=("POST",), roles=("customer",), idempotent=True)
def create_checkout():
	data = body()
	plan_id = get_str(data, "planId", required=True)
	interval = get_choice(data, "billingInterval", ("month", "year"), required=True)
	subscription_id = get_str(data, "subscriptionId")
	account = current_account(["name", "account_name", "contact_email", "phone"])

	plan = frappe.db.get_value("GX Plan", {"name": plan_id, "is_active": 1}, "*", as_dict=True)
	if not plan:
		raise ApiError(422, "invalid_plan", "Unknown plan.", fields={"planId": "Unknown plan."})
	if plan.billing_interval != interval:
		raise ApiError(
			422,
			"invalid_plan",
			"This plan is billed differently.",
			fields={"billingInterval": "Doesn't match the plan."},
		)

	if subscription_id:
		sub = frappe.db.get_value(
			"GX Subscription",
			{"name": subscription_id, "account": account.name},
			["name", "product"],
			as_dict=True,
		)
		if not sub:
			raise not_found()
		if sub.product != plan.product:
			raise validation_error({"planId": "This plan is for a different product than the subscription."})
	if not plan.gateway_plan_id:
		frappe.log_error(title=f"GrowthX: GX Plan {plan.name} has no gateway_plan_id")
		raise _unavailable()

	try:
		reference = gateway.create_subscription(
			plan.gateway_plan_id, plan.billing_interval, {"gx_account": account.name, "gx_plan": plan.name}
		)
		public_key = gateway.public_key()
	except gateway.GatewayUnavailable:
		raise _unavailable()
	except gateway.GatewayError:
		frappe.log_error(title="GrowthX: gateway create_subscription failed")
		raise _unavailable()

	frappe.cache.set_value(
		_pending_key(reference),
		{"account": account.name, "plan": plan.name, "subscription": subscription_id, "user": current_user()},
		expires_in_sec=PENDING_TTL,
	)
	product_name = frappe.db.get_value("GX Product", plan.product, "product_name")
	return 201, {
		"provider": gateway.provider(),
		"reference": reference,
		"publicKey": public_key,
		"plan": {"id": plan.name, "name": plan.plan_name, "interval": plan.billing_interval},
		"amount": price(plan.price, plan.gst_included),
		"description": f"{product_name} {plan.plan_name} — billed {'yearly' if interval == 'year' else 'monthly'}",
		"prefill": {
			"name": frappe.db.get_value("User", current_user(), "full_name"),
			"email": account.contact_email,
			"phone": account.phone,
		},
	}


@endpoint(methods=("POST",), roles=("customer",))
def verify_checkout():
	data = body()
	get_choice(data, "provider", ("razorpay",), required=True)
	reference = get_str(data, "reference", required=True)
	payload = data.get("payload")
	if not isinstance(payload, dict):
		raise validation_error({"payload": "payload must be the object the gateway SDK returned."})
	account = current_account()

	# Already verified (idempotent repeat)?
	existing = frappe.db.get_value(
		"GX Subscription", {"gateway_subscription_id": reference, "account": account}, "name"
	)
	pending = frappe.cache.get_value(_pending_key(reference))
	if not existing and (not pending or pending.get("account") != account):
		raise not_found()

	try:
		payment_id = gateway.verify_payment(reference, payload)
	except gateway.GatewayUnavailable:
		raise _unavailable()
	except gateway.GatewayError:
		raise ApiError(400, "payment_verification_failed", "We couldn't verify this payment.")

	if existing:
		sub = frappe.get_doc("GX Subscription", existing, for_update=True)
	elif pending.get("subscription"):
		sub = frappe.get_doc("GX Subscription", pending["subscription"], for_update=True)
	else:
		sub = frappe.new_doc("GX Subscription")
		sub.account = account

	if sub.last_payment_id != payment_id:
		plan = frappe.get_doc("GX Plan", pending["plan"] if pending else sub.plan)
		sub.plan = plan.name
		sub.price = plan.price
		sub.gst_included = plan.gst_included
		sub.gst_rate = plan.gst_rate
		sub.status = "active"
		sub.trial_ends_at = None
		start = now_datetime()
		sub.current_period_end = (
			add_years(start, 1) if plan.billing_interval == "year" else add_months(start, 1)
		)
		sub.gateway = gateway.provider()
		sub.gateway_subscription_id = reference
		sub.last_payment_id = payment_id
		sub.save(ignore_permissions=True)
		frappe.cache.delete_value(_pending_key(reference))

	# The Sales Invoice is created by the gateway webhook (Phase 3), so it may not exist yet.
	return {"subscription": s.subscription(sub), "invoice": None}
