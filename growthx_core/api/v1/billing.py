"""Online checkout with PayU (growthx_core.billing.payu has the flow and the site_config switches).

Without `growthx_payu_gateway_account` in site_config, create_checkout answers 503
payment_unavailable: online payment is off and customers pay by bank transfer (admin.record_payment).
"""

import frappe

from growthx_core import direct
from growthx_core.api.v1 import _serializers as s
from growthx_core.api.v1._core import (
	ApiError,
	RateLimit,
	body,
	current_user,
	endpoint,
	get_choice,
	get_str,
	not_found,
	price,
	query,
	validation_error,
)
from growthx_core.api.v1.customer import current_account
from growthx_core.billing import billing_details, payu

ACCOUNT_FIELDS = ["name", "account_name", "status", "user", "contact_email", "phone", "customer"]
INVOICE_FIELDS = [
	"name",
	"status",
	"net_total",
	"total_taxes_and_charges",
	"grand_total",
	"posting_date",
	"due_date",
]


def _unavailable(reason: Exception):
	# Deferred: the request is rolled back after an error response, and the log must survive that.
	frappe.log_error(title="GrowthX: online payment unavailable", message=str(reason), defer_insert=True)
	return ApiError(
		503, "payment_unavailable", "Payments are not available right now. Please try again later."
	)


@endpoint(methods=("POST",), roles=("customer",), idempotent=True, rate_limit=RateLimit(10, 60, by_user=True))
def create_checkout():
	data = body()
	plan_id = get_str(data, "planId", required=True)
	interval = get_choice(data, "billingInterval", ("month", "year", "one_time"))
	account = current_account(ACCOUNT_FIELDS)

	plan = frappe.db.get_value("GX Plan", {"name": plan_id, "is_active": 1}, "*", as_dict=True)
	if not plan:
		raise ApiError(422, "invalid_plan", "Unknown plan.", fields={"planId": "Unknown plan."})
	if interval and plan.billing_interval != interval:
		raise ApiError(
			422,
			"invalid_plan",
			"This plan is billed differently.",
			fields={"billingInterval": "Doesn't match the plan."},
		)

	# ERPNext Direct: only the plan the team assigned, and only while billing hasn't been terminated.
	direct.require_assigned_plan(account.name, plan)

	if not billing_details.available():
		raise _unavailable(Exception("india_compliance isn't installed: GST invoices can't be made"))
	if not billing_details.is_complete(account):
		raise ApiError(
			422,
			"billing_details_required",
			"Add your billing address before paying; it goes on your tax invoice.",
		)

	try:
		checkout, form = payu.create_checkout(account, plan)
	except payu.Unavailable as e:
		raise _unavailable(e)

	product_name = frappe.db.get_value("GX Product", plan.product, "product_name")
	invoice = frappe.db.get_value("Sales Invoice", checkout.sales_invoice, INVOICE_FIELDS, as_dict=True)
	return 201, {
		"provider": "payu",
		"reference": checkout.name,
		"plan": {"id": plan.name, "name": plan.plan_name, "interval": plan.billing_interval},
		"amount": price(invoice.grand_total, 1),
		"invoice": s.invoice(invoice),
		"description": f"{product_name} {plan.plan_name}",
		"prefill": {
			"name": frappe.db.get_value("User", current_user(), "full_name"),
			"email": account.contact_email,
			"phone": account.phone,
		},
		# The browser submits these fields to `url` as a normal form POST (a full-page navigation).
		"redirect": {"url": form["url"], "method": "POST", "fields": form["params"]},
	}


@endpoint(methods=("GET",), roles=("customer",), rate_limit=RateLimit(60, 60, by_user=True))
def checkout_status():
	"""After PayU sends the customer back (`?payu_txnid=…` on the billing page), or with the
	checkout `reference`. Only the customer's own checkouts are found."""
	data = query()
	txnid = get_str(data, "txnid", max_length=40)
	reference = get_str(data, "reference", max_length=40)
	if not txnid and not reference:
		raise validation_error({"txnid": "Send txnid (from PayU's return) or reference."})
	name = payu.checkout_for_txnid(txnid) if txnid else reference
	account = current_account()
	if not name or frappe.db.get_value("GX Checkout", name, "account") != account:
		raise not_found()

	checkout = frappe.get_doc("GX Checkout", name)
	if (
		checkout.payment_entry
		and frappe.db.get_value("Payment Entry", checkout.payment_entry, "docstatus") == 2
	):
		status = "reversed"  # the payment was cancelled or refunded after it was received
	elif checkout.status == "paid":
		status = "paid"
	elif checkout.status == "needs_attention":
		status = "attention"  # we have the money; our team activates the plan
	elif checkout.status == "cancelled":
		status = "failed"
	else:
		pr = checkout.payment_request
		attempt = payu.last_attempt_status(pr) if pr else None
		# A payment that may still go through (an Authorized attempt, even with a newer failed one) is
		# pending: the customer must not be told to pay again.
		in_flight = bool(pr) and payu.attempt_in_flight(pr)
		status = "failed" if attempt in ("Failed", "Cancelled") and not in_flight else "pending"

	invoice = (
		frappe.db.get_value("Sales Invoice", checkout.sales_invoice, INVOICE_FIELDS, as_dict=True)
		if checkout.sales_invoice
		else None
	)
	subscription = None
	if status == "paid" and checkout.subscription:
		subscription = s.subscription(frappe.get_doc("GX Subscription", checkout.subscription))
	plan_name = frappe.db.get_value("GX Plan", checkout.plan, "plan_name")
	return {
		"reference": checkout.name,
		"status": status,
		"plan": {"id": checkout.plan, "name": plan_name},
		"invoice": s.invoice(invoice) if invoice else None,
		"subscription": subscription,
	}
