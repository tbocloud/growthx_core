from datetime import date, timedelta
from urllib.parse import urlparse

import frappe
from frappe.query_builder.functions import Count
from frappe.utils import (
	add_months,
	add_years,
	fmt_money,
	get_datetime,
	getdate,
	now_datetime,
	nowdate,
	sha256_hash,
)

from growthx_core import accounts, tokens
from growthx_core.api.v1 import _ledger as ledger
from growthx_core.api.v1 import _serializers as s
from growthx_core.api.v1 import appointments as appts
from growthx_core.api.v1._core import (
	ApiError,
	audit,
	body,
	endpoint,
	get_choice,
	get_email,
	get_sort,
	get_str,
	iso,
	money,
	monthly,
	net_of_gst,
	not_found,
	page_meta,
	page_params,
	query,
	validation_error,
	web_url,
)
from growthx_core.billing import manual_invoice, payments, payu
from growthx_core.billing import renewals as renewal_jobs
from growthx_core.billing.payments import set_customer_status as _set_customer_status
from growthx_core.billing.renewal_schedule import format_sent, parse_sent
from growthx_core.partners import commissions as commission_ops
from growthx_core.provisioning import press, runner
from growthx_core.setup.defaults import default_link

CUSTOMER_STATUSES = ("trial", "active", "suspended", "churned")
SITE_STATUSES = ("requested", "provisioning", "ready", "failed", "suspended", "archived")


def _count(doctype, filters, or_filters) -> int:
	if not or_filters:
		return frappe.db.count(doctype, filters)
	return len(frappe.get_all(doctype, filters=filters, or_filters=or_filters, pluck="name"))


def _mrr(sub) -> float:
	"""Monthly recurring revenue of one subscription, excluding GST."""
	return monthly(net_of_gst(sub.price, sub.gst_included, sub.gst_rate), sub.billing_interval)


@endpoint(methods=("GET",), roles=("admin",))
def summary():
	active_subs = frappe.get_all(
		"GX Subscription", {"status": "active"}, ["price", "gst_included", "gst_rate", "billing_interval"]
	)
	account = frappe.qb.DocType("GX Account")
	counts = dict(
		frappe.qb.from_(account).select(account.status, Count(account.name)).groupby(account.status).run()
	)
	return {
		"mrr": money(sum(_mrr(sub) for sub in active_subs)),
		"activeCustomers": counts.get("active", 0),
		"trialCustomers": counts.get("trial", 0),
		"suspendedCustomers": counts.get("suspended", 0),
		"activePartners": frappe.db.count("GX Partner Profile", {"status": "active"}),
	}


@endpoint(methods=("GET",), roles=("admin",))
def customers():
	data = query()
	page, page_size = page_params(data)
	status = get_choice(data, "status", CUSTOMER_STATUSES)
	q = get_str(data, "q", max_length=100)
	order_by = get_sort(data, {"createdAt": "creation", "company": "account_name"}, "-createdAt")

	filters = {"status": status} if status else {}
	or_filters = {"account_name": ("like", f"%{q}%"), "contact_email": ("like", f"%{q}%")} if q else None
	rows = frappe.get_all(
		"GX Account",
		filters=filters,
		or_filters=or_filters,
		fields=["name", "account_name", "contact_email", "status", "referred_by", "creation"],
		order_by=order_by,
		limit_start=(page - 1) * page_size,
		limit_page_length=page_size,
	)
	total = _count("GX Account", filters, or_filters)

	names = [r.name for r in rows]
	subs = {}
	for sub in frappe.get_all(
		"GX Subscription",
		{"account": ("in", names or [""])},
		["account", "plan", "status", "price", "gst_included", "gst_rate", "billing_interval"],
		order_by="creation asc",
	):
		subs[sub.account] = sub  # latest wins
	plan_names = dict(frappe.get_all("GX Plan", fields=["name", "plan_name"], as_list=True))
	partner_names = dict(frappe.get_all("GX Partner Profile", fields=["name", "partner_name"], as_list=True))

	items = [_admin_customer(r, subs.get(r.name), plan_names, partner_names) for r in rows]
	return page_meta(page, page_size, total, items)


def _admin_customer(r, sub, plan_names, partner_names) -> dict:
	return {
		"id": r.name,
		"company": r.account_name,
		"contactEmail": r.contact_email,
		"status": r.status,
		"plan": plan_names.get(sub.plan) if sub else None,
		"partnerName": partner_names.get(r.referred_by) if r.referred_by else None,
		"mrr": money(_mrr(sub) if sub and sub.status == "active" else 0),
		"createdAt": iso(r.creation),
	}


CUSTOMER_FIELDS = ["name", "account_name", "contact_email", "status", "referred_by", "creation", "user"]
SUB_FIELDS = [
	"name",
	"account",
	"plan",
	"status",
	"price",
	"gst_included",
	"gst_rate",
	"billing_interval",
	"trial_ends_at",
	"current_period_end",
]


def _customer_detail(account_id: str) -> dict:
	r = frappe.db.get_value("GX Account", account_id, CUSTOMER_FIELDS, as_dict=True)
	subs = frappe.get_all("GX Subscription", {"account": account_id}, SUB_FIELDS, order_by="creation asc")
	latest = subs[-1] if subs else None
	plan_names = dict(frappe.get_all("GX Plan", fields=["name", "plan_name"], as_list=True))
	partner_name = (
		frappe.db.get_value("GX Partner Profile", r.referred_by, "partner_name") if r.referred_by else None
	)
	result = _admin_customer(r, latest, plan_names, {r.referred_by: partner_name} if r.referred_by else {})
	tenants = frappe.get_all(
		"GX Tenant",
		{"account": account_id},
		["name", "product", "site_url", "status", "country"],
		order_by="creation asc",
	)
	trialing = [sub for sub in subs if sub.status == "trialing" and sub.trial_ends_at]
	result.update(
		{
			"contactName": frappe.db.get_value("User", r.user, "full_name") if r.user else None,
			"country": next((t.country for t in reversed(tenants) if t.country), None),
			"trialEndsAt": iso(trialing[-1].trial_ends_at) if trialing else None,
			"sites": [
				{"id": t.name, "productSlug": t.product, "siteUrl": t.site_url or None, "status": t.status}
				for t in tenants
			],
			"subscriptions": [
				{
					"id": sub.name,
					"plan": plan_names.get(sub.plan) or sub.plan,
					"status": sub.status,
					"currentPeriodEnd": iso(sub.current_period_end),
				}
				for sub in subs
			],
			"history": _history("GX Account", account_id, "customer."),
		}
	)
	return result


def _history(doctype: str, name: str, prefix: str) -> list[dict]:
	return [
		{
			"at": iso(r.creation),
			"actor": r.actor,
			"action": r.action.removeprefix(prefix),
			"details": r.details,
		}
		for r in frappe.get_all(
			"GX Audit Log",
			filters={"reference_doctype": doctype, "reference_name": name},
			fields=["creation", "actor", "action", "details"],
			order_by="creation asc",
		)
	]


@endpoint(methods=("GET",), roles=("admin",))
def customer():
	account_id = get_str(query(), "id", required=True)
	if not frappe.db.exists("GX Account", account_id):
		raise not_found()
	return _customer_detail(account_id)


CUSTOMER_ACTIONS = ("extend_trial", "suspend", "reactivate")
MAX_TRIAL_EXTENSION = 30


@endpoint(methods=("POST",), roles=("admin",), idempotent=True)
def update_customer_status():
	"""extend_trial {days 1-30} (trial only: from now or the trial end, whichever is later),
	suspend {reason} (trial or active: their sites go offline), reactivate (suspended → trial if the
	trial hasn't ended, else active: their sites come back). Returns the customer's details."""
	data = body()
	account_id = get_str(data, "id", required=True)
	action = get_choice(data, "action", CUSTOMER_ACTIONS, required=True)
	reason = get_str(data, "reason", required=action == "suspend", max_length=500, label="Reason")
	days = data.get("days")
	if action == "extend_trial" and (
		isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= MAX_TRIAL_EXTENSION
	):
		raise validation_error({"days": f"Enter a whole number of days from 1 to {MAX_TRIAL_EXTENSION}."})
	if not frappe.db.exists("GX Account", account_id):
		raise not_found()

	account = frappe.get_doc("GX Account", account_id, for_update=True)
	allowed = {"extend_trial": ("trial",), "suspend": ("trial", "active"), "reactivate": ("suspended",)}
	if account.status not in allowed[action]:
		what = {
			"extend_trial": "have their trial extended",
			"suspend": "be suspended",
			"reactivate": "be reactivated",
		}
		raise ApiError(409, "invalid_transition", f"A customer who is {account.status} can't {what[action]}.")

	if action == "reactivate" and frappe.db.exists(
		"GX Subscription", {"account": account.name, "status": "suspended", "suspended_by": "billing"}
	):
		# Reactivating would be undone by the next hourly run: the admin must say until when.
		raise ApiError(409, "billing_suspended", "Use Give extra days for a site suspended for non-payment.")

	if action == "extend_trial":
		details = _extend_trial(account, days)
	elif action == "suspend":
		details = _set_customer_status(account, "suspended", "ready", "suspended", "suspend_site")
	else:
		trial_left = frappe.db.exists(
			"GX Subscription",
			{"account": account.name, "status": "trialing", "trial_ends_at": (">", now_datetime())},
		)
		details = _set_customer_status(
			account, "trial" if trial_left else "active", "suspended", "ready", "resume_site"
		)
	if reason:
		details += f": {reason}"
	audit(f"customer.{action}", "GX Account", account.name, details)
	return _customer_detail(account.name)


def _extend_trial(account, days: int) -> str:
	sub = frappe.get_all(
		"GX Subscription",
		{"account": account.name, "status": "trialing"},
		["name", "trial_ends_at", "due_date", "tenant"],
		order_by="creation desc",
		limit=1,
	)
	if not sub:
		raise ApiError(409, "invalid_transition", "This customer has no trial subscription to extend.")
	old = sub[0].trial_ends_at
	new = max(now_datetime(), get_datetime(old) if old else now_datetime()) + timedelta(days=days)
	# A new end date: the renewal job reminds again before it (the reminders sent were for the old one).
	values = {"trial_ends_at": new, "notified_stages": None, "final_warning_on": None}
	direct = sub[0].tenant and frappe.db.get_value("GX Tenant", sub[0].tenant, "origin") == "imported"
	if direct and sub[0].due_date:
		# ERPNext Direct "billing starts later": the start date (its day 0 and first period) moves as much.
		shift = getdate(new) - getdate(old) if old else None
		values["due_date"] = getdate(sub[0].due_date) + shift if shift is not None else getdate(new)
	frappe.db.set_value("GX Subscription", sub[0].name, values)
	details = f"+{days} days: trial ends {iso(new)} (was {iso(old) if old else 'not set'})"
	if "due_date" in values:
		details += (
			f", billing starts {values['due_date'].isoformat()} (was {getdate(sub[0].due_date).isoformat()})"
		)
	return details


@endpoint(methods=("POST",), roles=("admin",), idempotent=True)
def record_payment():
	"""A payment made to us directly (bank transfer) while there's no payment gateway: the customer's
	subscription for the plan's product becomes active on that plan for one billing period, and the
	account becomes active (a suspended customer's sites come back). Where the period starts is
	payments._period_start: a renewal paid early or in grace continues from the current period end, one
	paid after a suspension starts on paidOn, a first payment starts on its due date; a period that would
	already be over on paidOn starts on paidOn. A reference already recorded on the subscription is
	refused. A one-time (lifetime) plan has no period end and can't be paid again.
	A GST tax invoice, paid by a Payment Entry for the transfer, is made when GST invoicing and
	site_config growthx_bank_account are set up (billing.manual_invoice); the audit line names it, or why
	there is none."""
	data = body()
	account_id = get_str(data, "id", required=True)
	plan_id = get_str(data, "plan", required=True)
	reference = get_str(data, "reference", required=True, max_length=140, label="Reference")
	amount = data.get("amount")
	if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
		raise validation_error({"amount": "Enter the amount paid in paise, a whole number above 0."})
	paid_on = _paid_on(data.get("paidOn"))
	if not frappe.db.exists("GX Account", account_id):
		raise not_found()
	if not frappe.db.exists("GX Plan", plan_id):
		raise validation_error({"plan": "Unknown plan."})
	plan = frappe.get_doc("GX Plan", plan_id)

	account = frappe.get_doc("GX Account", account_id, for_update=True)
	_record_manual_payment(account, plan, reference, amount, paid_on)
	return _customer_detail(account.name)


def _record_manual_payment(account, plan, reference: str, amount: int, paid_on: date):
	"""Apply a payment made to us directly and audit it (record_payment, and add_direct_site's
	"already paid offline"). `account` is the locked GX Account document."""
	# The customer mustn't also pay online for what's recorded here (409 if a payment is under way).
	payu.cancel_open_checkouts(account.name, plan.product)
	invoice, no_invoice = manual_invoice.make(account, plan, reference, amount / 100, paid_on)
	_sub, details, period_end = payments.apply_payment(
		account, plan, reference, paid_on, "manual", amount=amount / 100, sales_invoice=invoice
	)
	audit(
		"customer.record_payment",
		"GX Account",
		account.name,
		f"{details}: {fmt_money(amount / 100, currency='INR')} for {plan.plan_name}"
		f" (plan price {payments.plan_price(plan)}) paid on {paid_on.isoformat()}, reference {reference},"
		f" active {f'until {period_end.isoformat()}' if period_end else 'for life'}"
		+ (f", invoice {invoice}" if invoice else f", no GST invoice ({no_invoice})" if no_invoice else ""),
	)


def _paid_on(value) -> date:
	try:
		paid_on = date.fromisoformat(value) if isinstance(value, str) else None
	except ValueError:
		paid_on = None
	if not paid_on or len(value) != 10:
		raise validation_error({"paidOn": "Enter the date paid as YYYY-MM-DD."})
	if paid_on.isoformat() > nowdate():
		raise validation_error({"paidOn": "The date paid can't be in the future."})
	return paid_on


@endpoint(methods=("GET",), roles=("admin",))
def tenants():
	data = query()
	page, page_size = page_params(data)
	status = get_choice(data, "status", SITE_STATUSES)
	q = get_str(data, "q", max_length=100)
	order_by = get_sort(data, {"createdAt": "creation", "updatedAt": "modified"}, "-createdAt")

	filters = {"status": status} if status else {}
	or_filters = None
	if q:
		matching_accounts = frappe.get_all("GX Account", {"account_name": ("like", f"%{q}%")}, pluck="name")
		or_filters = {
			"display_name": ("like", f"%{q}%"),
			"site_url": ("like", f"%{q}%"),
			"product": ("like", f"%{q}%"),
		}
		if matching_accounts:
			or_filters["account"] = ("in", matching_accounts)

	fields = [
		"name",
		"account",
		"product",
		"origin",
		"subscription",
		"site_url",
		"status",
		"last_error",
		"attempts",
		"creation",
		"modified",
	]
	rows = frappe.get_all(
		"GX Tenant",
		filters=filters,
		or_filters=or_filters,
		fields=fields,
		order_by=order_by,
		limit_start=(page - 1) * page_size,
		limit_page_length=page_size,
	)
	total = _count("GX Tenant", filters, or_filters)
	account_names = dict(
		frappe.get_all(
			"GX Account",
			{"name": ("in", [r.account for r in rows] or [""])},
			["name", "account_name"],
			as_list=True,
		)
	)
	return page_meta(page, page_size, total, [s.admin_tenant(r, account_names.get(r.account)) for r in rows])


@endpoint(methods=("GET",), roles=("admin",))
def tenant():
	tenant_id = get_str(query(), "id", required=True)
	if not frappe.db.exists("GX Tenant", tenant_id):
		raise not_found()
	return s.admin_tenant_detail(frappe.get_doc("GX Tenant", tenant_id))


@endpoint(methods=("POST",), roles=("admin",), idempotent=True)
def retry_provisioning():
	tenant_id = get_str(body(), "id", required=True)
	if not frappe.db.exists("GX Tenant", tenant_id):
		raise not_found()
	status = frappe.db.get_value("GX Tenant", tenant_id, "status")
	if status in ("requested", "provisioning"):
		# Already running: return its current state, don't start another job.
		return 202, s.admin_tenant_detail(frappe.get_doc("GX Tenant", tenant_id))
	if not runner.retry(tenant_id):
		raise ApiError(409, "not_retryable", "Only a failed site can be retried.")
	audit("retry_provisioning", "GX Tenant", tenant_id)
	return 202, s.admin_tenant_detail(frappe.get_doc("GX Tenant", tenant_id))


@endpoint(methods=("POST",), roles=("admin",), idempotent=True)
def archive_tenant():
	"""Archive a failed site (its Press site, if any, is backed up and deleted)."""
	tenant_id = get_str(body(), "id", required=True)
	if not frappe.db.exists("GX Tenant", tenant_id):
		raise not_found()
	if not runner.archive_failed(tenant_id):
		raise ApiError(409, "not_archivable", "Only a failed site can be archived.")
	audit("archive_tenant", "GX Tenant", tenant_id)
	return s.admin_tenant_detail(frappe.get_doc("GX Tenant", tenant_id))


# ---------------------------------------------------------------- tenants: plans and termination

PLAN_INTERVALS = ("month", "year", "one_time")


def _paise(data: dict, key: str) -> float:
	"""An API amount (paise, whole number) as rupees."""
	value = data.get(key)
	if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
		raise validation_error({key: "Enter the amount in paise, a whole number above 0."})
	return value / 100


def _plan_id(product: str, plan_name: str, interval: str) -> str:
	from frappe.utils import slug

	suffix = "" if interval == "month" else f" {interval.replace('_', '-')}"
	return slug(f"{product} {plan_name}{suffix}") or slug(plan_name) or "plan"


@endpoint(methods=("GET",), roles=("admin",))
def products():
	"""Admin → Plans and Products: every app, including the `hidden` ones the public catalogue leaves out
	(ERPNext Direct is sold only by the team), so an admin can add a plan to them and see their plans."""
	rows = frappe.get_all(
		"GX Product",
		fields=["name", "slug", "product_name", "category", "summary", "availability"],
		order_by="sort_order asc, product_name asc",
	)
	return {"items": [s.product(r) for r in rows]}


@endpoint(methods=("POST",), roles=("admin",), idempotent=True)
def create_plan():
	"""A new plan on the catalogue: what a customer can be sold and what `assign_plan` bills. Its
	billing Item is made too, without which a GST invoice for the plan can't be raised."""
	from growthx_core.setup import seed

	data = body()
	product = get_str(data, "product", required=True)
	plan_name = get_str(data, "planName", required=True, label="Plan name")
	interval = get_choice(data, "interval", PLAN_INTERVALS, required=True)
	plan_price = _paise(data, "price")
	if not frappe.db.exists("GX Product", product):
		raise validation_error({"product": "That app doesn't exist."})
	plan_id = get_str(data, "id") or _plan_id(product, plan_name, interval)
	if frappe.db.exists("GX Plan", plan_id):
		raise ApiError(409, "plan_exists", "A plan with that id already exists.")
	trial_days = data.get("trialDays", 0)
	if isinstance(trial_days, bool) or not isinstance(trial_days, int) or not 0 <= trial_days <= 90:
		raise validation_error({"trialDays": "Enter a whole number of days from 0 to 90."})
	gst_rate = data.get("gstRate", 18)
	if isinstance(gst_rate, bool) or not isinstance(gst_rate, int | float) or not 0 <= gst_rate <= 28:
		raise validation_error({"gstRate": "Enter a GST rate from 0 to 28."})
	plan = frappe.get_doc(
		{
			"doctype": "GX Plan",
			"plan_id": plan_id,
			"product": product,
			"plan_name": plan_name,
			"billing_interval": interval,
			"price": plan_price,
			"gst_included": 1 if data.get("gstIncluded") else 0,
			"gst_rate": gst_rate,
			"trial_days": trial_days,
			"description": get_str(data, "description", max_length=2000),
			"features": get_str(data, "features", max_length=2000),
			"highlighted": 1 if data.get("highlighted") else 0,
			"is_active": 1,
		}
	).insert(ignore_permissions=True)
	seed.plan_items()
	plan.reload()
	audit(
		"plan.create",
		"GX Plan",
		plan.name,
		f"{plan.plan_name} for {product}: {fmt_money(plan.price, currency='INR')} per {interval}",
	)
	return 201, s.admin_plan(plan)


def _move_subscription_to_plan(sub, plan):
	"""Bill `sub` on `plan` from now: the new price, GST and interval. A monthly or yearly period that
	hasn't run out keeps its end (a plan change isn't a new payment); a one-time plan has no end."""
	if plan.billing_interval == "one_time" or (sub.status == "past_due" and not sub.current_period_end):
		period_end = None  # (a first invoice not yet paid has no period to keep)
	else:
		today = getdate(nowdate())
		start = max(getdate(sub.current_period_end), today) if sub.current_period_end else today
		period_end = add_years(start, 1) if plan.billing_interval == "year" else add_months(start, 1)
	sub.update(
		{
			"plan": plan.name,
			"billing_interval": plan.billing_interval,
			"price": plan.price,
			"gst_included": plan.gst_included,
			"gst_rate": plan.gst_rate,
			"current_period_end": period_end,
			"cancel_at_period_end": 0,
			# A moved period: the renewal job's reminders and warnings start again for the new end.
			"notified_stages": None,
			"final_warning_on": None,
		}
	)
	sub.save(ignore_permissions=True)


@endpoint(methods=("POST",), roles=("admin",), idempotent=True)
def assign_plan():
	"""Change the plan a site is billed on. The plan must be of the same app: another app means other
	apps on the site, which is a new site. Billing only; nothing on the site changes."""
	data = body()
	tenant_id = get_str(data, "id", required=True)
	plan_id = get_str(data, "plan", required=True)
	if not frappe.db.exists("GX Tenant", tenant_id):
		raise not_found()
	tenant = frappe.get_doc("GX Tenant", tenant_id)
	if tenant.status == "archived":
		raise ApiError(409, "tenant_archived", "A terminated site can't change plan.")
	if tenant.status in ("requested", "provisioning", "failed"):
		raise ApiError(409, "invalid_transition", "A site that isn't live yet can't change plan.")
	if not tenant.subscription:
		raise ApiError(409, "no_subscription", "This site has no subscription to change.")
	if not frappe.db.exists("GX Plan", plan_id):
		raise validation_error({"plan": "Unknown plan."})
	plan = frappe.get_doc("GX Plan", plan_id)
	if not plan.is_active:
		raise validation_error({"plan": "That plan isn't on sale."})
	# Account first, then the subscription: the order the renewal job and payments lock in.
	account_name = frappe.db.get_value("GX Subscription", tenant.subscription, "account")
	if account_name:
		frappe.db.get_value("GX Account", account_name, "name", for_update=True)
	sub = frappe.get_doc("GX Subscription", tenant.subscription, for_update=True)
	if sub.status == "canceled":
		raise ApiError(409, "no_subscription", "This site's subscription is canceled.")
	if plan.product != sub.product:
		raise validation_error({"plan": "Choose a plan of the same app."})
	if sub.plan != plan.name:
		old = frappe.db.get_value("GX Plan", sub.plan, "plan_name") or sub.plan
		_move_subscription_to_plan(sub, plan)
		audit(
			"tenant.assign_plan",
			"GX Subscription",
			sub.name,
			f"{old} → {plan.plan_name} ({tenant.name}): {fmt_money(plan.price, currency='INR')}"
			f" per {plan.billing_interval}",
		)
	return s.admin_tenant_detail(frappe.get_doc("GX Tenant", tenant_id))


@endpoint(methods=("POST",), roles=("admin",), idempotent=True)
def terminate_tenant():
	"""End a site and its billing for good. A site Core built is backed up and deleted on Press; an
	imported ERPNext site is the customer's own, so it is left running and only billing stops."""
	tenant_id = get_str(body(), "id", required=True)
	if not frappe.db.exists("GX Tenant", tenant_id):
		raise not_found()
	tenant = frappe.get_doc("GX Tenant", tenant_id)
	if tenant.status == "archived":
		raise ApiError(409, "already_archived", "This site is already terminated.")
	if tenant.status in ("requested", "provisioning"):
		raise ApiError(409, "invalid_transition", "This site is still being set up. Wait for it to finish.")
	if tenant.origin == "imported" and tenant.status == "suspended":
		# Once archived it leaves the Press-side job's list, so nothing would ever switch the site back on.
		raise ApiError(
			409,
			"site_suspended",
			"Reactivate or give extra days first, so the site is running again before billing ends.",
		)

	details = f"{tenant.status} → archived, site {tenant.site_url or '-'}"
	if tenant.origin == "imported":
		frappe.db.set_value("GX Tenant", tenant.name, {"status": "archived", "status_message": None})
		details += " (imported: the site itself is untouched)"
	else:
		try:
			runner.archive(tenant.name)
		except press.PressError:
			raise ApiError(502, "press_unavailable", "Press could not be reached right now.") from None
	subscriptions = set(
		frappe.get_all(
			"GX Subscription",
			{"tenant": tenant.name, "status": ("!=", "canceled")},
			pluck="name",
		)
	)
	if tenant.subscription:
		subscriptions.add(tenant.subscription)
	for name in subscriptions:
		frappe.db.set_value(
			"GX Subscription",
			name,
			{"status": "canceled", "cancel_at_period_end": 0, "current_period_end": nowdate()},
		)
		details += f", subscription {name} canceled"
	audit("tenant.terminate", "GX Tenant", tenant.name, details)
	return s.admin_tenant_detail(frappe.get_doc("GX Tenant", tenant.name))


# ---------------------------------------------------------------- renewals

MAX_GRACE_DAYS = 60
# Sync states (Press-side job's report) that need an admin: the site wasn't found, was held back, or failed.
SYNC_PROBLEMS = ("Not found", "Not on platform", "held", "error")


def _renewal_item(sub, tenant, accounts_by_id: dict, plan_names: dict, job) -> dict:
	"""One row of Admin → Renewals. `sub` may be None (an imported site with no subscription)."""
	info = (sub and renewal_jobs.renewal_info(sub, tenant, _job=job)) or {}
	site = tenant and (tenant.platform_site or urlparse(tenant.site_url or "").hostname or tenant.site_url)
	account = sub.account if sub else tenant.account
	return {
		"subscriptionId": sub.name if sub else None,
		"accountId": account,
		"accountName": accounts_by_id.get(account),
		"site": site or None,
		"origin": (tenant.origin if tenant else None) or "provisioned",
		"planName": plan_names.get(sub.plan) if sub else None,
		"stage": info.get("stage"),
		"expiresAt": info.get("expiresAt"),
		"suspendsAt": info.get("suspendsAt"),
		"archivesAt": info.get("archivesAt"),
		"syncStatus": (tenant.sync_status if tenant else None) or None,
		"syncMessage": (tenant.sync_message if tenant else None) or None,
	}


TENANT_FIELDS = [
	"name",
	"account",
	"origin",
	"status",
	"site_url",
	"subscription",
	"platform_site",
	"sync_status",
	"sync_message",
]


@endpoint(methods=("GET",), roles=("admin",))
def renewals():
	"""Admin → Renewals: `expiring` (active, ending within 7 days; 30 for a yearly plan), `pastDue`,
	`suspended` (with the day a paused trial is deleted), `syncProblems` (ERPNext Direct sites the
	Press-side job couldn't find or act on, or hasn't matched to a Press site yet), `resumePending` (paid,
	but Press refused to switch the site back on: `resume_pending`, which the job retries each hour; a site
	an admin suspended is never in it) and `unscheduled` (live subscriptions with no date to renew from,
	which the job can't follow). Sites that aren't built or are archived are left out, as the job leaves
	them alone."""
	job = renewal_jobs.new_job()
	subs = frappe.get_all(
		"GX Subscription", {"status": ("in", renewal_jobs.LIVE)}, ["*"], order_by="creation asc"
	)
	tenants = {
		t.name: t
		for t in frappe.get_all(
			"GX Tenant",
			{"name": ("in", [x.tenant for x in subs if x.tenant] or [""])},
			TENANT_FIELDS,
		)
	}
	imported = frappe.get_all(
		"GX Tenant",
		{"origin": "imported", "status": ("not in", ("archived", *renewal_jobs.SITES_NOT_LIVE))},
		TENANT_FIELDS,
	)
	accounts_by_id = dict(frappe.get_all("GX Account", fields=["name", "account_name"], as_list=True))
	plan_names = dict(frappe.get_all("GX Plan", fields=["name", "plan_name"], as_list=True))
	lists = {
		"expiring": [],
		"pastDue": [],
		"suspended": [],
		"syncProblems": [],
		"resumePending": [],
		"unscheduled": [],
	}
	by_sub = {x.name: x for x in subs}
	for sub in subs:
		tenant = tenants.get(sub.tenant)
		if not tenant or tenant.status in ("archived", *renewal_jobs.SITES_NOT_LIVE):
			continue
		if renewal_jobs.resume_pending(sub, tenant):
			lists["resumePending"].append(_renewal_item(sub, tenant, accounts_by_id, plan_names, job))
			continue
		info = renewal_jobs.renewal_info(sub, tenant, _job=job)
		if not info:
			if not (sub.billing_interval == "one_time" and sub.status == "active"):
				# No period end, due date or trial end (e.g. a site billed outside Core): nothing to follow.
				lists["unscheduled"].append(_renewal_item(sub, tenant, accounts_by_id, plan_names, job))
			continue
		item = _renewal_item(sub, tenant, accounts_by_id, plan_names, job)
		stage = info["stage"]
		if stage in ("suspended", "trial_ended") and (
			sub.status == "suspended" or tenant.status == "suspended"
		):
			lists["suspended"].append(item)
		elif stage in ("first_due", "expired"):
			lists["pastDue"].append(item)
		elif (
			sub.status == "active"
			and stage in ("paid", "due_soon")
			and info["daysLeft"] <= (30 if sub.billing_interval == "year" else 7)
		):
			lists["expiring"].append(item)
	for tenant in imported:
		if tenant.sync_status in SYNC_PROBLEMS or not tenant.platform_site:
			sub = by_sub.get(tenant.subscription)
			lists["syncProblems"].append(_renewal_item(sub, tenant, accounts_by_id, plan_names, job))
	for rows in lists.values():
		rows.sort(key=lambda i: (i["suspendsAt"] or i["expiresAt"] or "", i["accountName"] or ""))
	return lists


def _grace_until(value) -> date:
	"""`until`: a day after today, at most MAX_GRACE_DAYS away."""
	try:
		until = date.fromisoformat(value) if isinstance(value, str) and len(value) == 10 else None
	except ValueError:
		until = None
	if not until:
		raise validation_error({"until": "Enter the date as YYYY-MM-DD."})
	today = renewal_jobs._today()
	if until <= today:
		raise validation_error({"until": "The new date must be in the future."})
	if until > today + timedelta(days=MAX_GRACE_DAYS):
		raise validation_error({"until": f"The new date can be at most {MAX_GRACE_DAYS} days away."})
	return until


@endpoint(methods=("POST",), roles=("admin",), idempotent=True)
def extend_grace():
	"""Give extra days: the subscription is not suspended before `until` (`grace_until`; up to 60 days
	ahead). If the job suspended it for non-payment, it is brought back (a trial is `trialing` again, a
	paid plan `past_due`) and its site is resumed, so it runs until `until`. The reminders and warnings
	already sent stay recorded: the customer was warned, and the suspension email goes out at the end (a trial's trial_ended email
	goes out again, since it is the notice of the second pause).
	Returns the subscription's Renewals row."""
	data = body()
	sub_id = get_str(data, "id", required=True)
	until = _grace_until(data.get("until"))
	account_name = frappe.db.get_value("GX Subscription", sub_id, "account")
	if not account_name:
		raise not_found()
	# Account first, then the subscription: the order the renewal job and payments lock in.
	frappe.db.get_value("GX Account", account_name, "name", for_update=True)
	sub = frappe.get_doc("GX Subscription", sub_id, for_update=True)
	if sub.status not in renewal_jobs.LIVE:
		raise ApiError(409, "invalid_transition", "A canceled subscription has no suspension to delay.")
	tenant = frappe.db.get_value("GX Tenant", sub.tenant, TENANT_FIELDS, as_dict=True) if sub.tenant else None
	if tenant and tenant.status == "archived":
		raise ApiError(409, "tenant_archived", "This site is terminated: it has no suspension to delay.")
	restored = sub.status == "suspended" and sub.suspended_by == "billing"
	trial = restored and bool(tenant and tenant.origin != "imported") and renewal_jobs.was_trial(sub)
	values = {"grace_until": until}
	if restored:
		values.update(
			{
				"status": "trialing" if trial else "past_due",
				"suspended_by": None,
				# set again, afresh, if the site is suspended again
				"archive_due_on": None,
				"archive_warning_on": None,
			}
		)
		# A paid plan keeps `suspended_at`: paid now, it is a renewal after a suspension, so the new period
		# starts on the payment day (payments._period_start). A trial has no period to continue.
		if trial:
			values["suspended_at"] = None
	if restored and trial:
		# The trial_ended email goes out again just before the second pause; the reminders stay recorded.
		values["notified_stages"] = format_sent(parse_sent(sub.notified_stages) - {0}) or None
	sub.update(values)
	sub.save(ignore_permissions=True)  # on_update keeps the account's status in step
	if restored:
		renewal_jobs.resume_subscription_site(sub.name)
		if frappe.db.get_value("GX Tenant", sub.tenant, "status") == "suspended":
			# Press refused (logged by the resume): undo the status change rather than leave them out of step
			raise ApiError(502, "press_unavailable", "Press could not resume the site right now. Try again.")
	audit(
		"renewal.extend_grace",
		"GX Subscription",
		sub.name,
		f"not suspended before {until.isoformat()}" + (", site resumed" if restored else ""),
	)
	job = renewal_jobs.new_job()
	accounts_by_id = {account_name: frappe.db.get_value("GX Account", account_name, "account_name")}
	plan_names = {sub.plan: frappe.db.get_value("GX Plan", sub.plan, "plan_name")}
	tenant = frappe.db.get_value("GX Tenant", sub.tenant, TENANT_FIELDS, as_dict=True) if sub.tenant else None
	return _renewal_item(sub, tenant, accounts_by_id, plan_names, job)


# ---------------------------------------------------------------- audit log

ENTITY_DOCTYPES = {
	"customer": "GX Account",
	"partner": "GX Partner Profile",
	"tenant": "GX Tenant",
	"appointment": "GX Appointment",
	"content": "GX Website Post",
}
# Website content spans three doctypes; entityType=content lists them all.
CONTENT_DOCTYPES = ("GX Website Post", "GX Content Site", "GX Content Page")
ENTITY_LABELS = {
	"GX Website Post": "title",
	"GX Content Site": "site_name",
	"GX Content Page": "path",
	"GX Account": "account_name",
	"GX Partner Profile": "partner_name",
	"GX Tenant": "display_name",
	"GX Appointment": "full_name",
}


@endpoint(methods=("GET",), roles=("admin",))
def audit_log():
	"""Every recorded admin and system action, newest first."""
	data = query()
	page, page_size = page_params(data)
	entity_type = get_choice(data, "entityType", tuple(ENTITY_DOCTYPES))
	entity_id = get_str(data, "entityId")
	actor = get_str(data, "actor")
	start, end = _day(data, "from"), _day(data, "to")
	if start and end and start > end:
		raise validation_error({"from": "from must be on or before to."})

	filters = {}
	if entity_type == "content":
		filters["reference_doctype"] = ("in", CONTENT_DOCTYPES)
	elif entity_type:
		filters["reference_doctype"] = ENTITY_DOCTYPES[entity_type]
	if entity_id:
		filters["reference_name"] = entity_id
	if actor:
		filters["actor"] = actor
	if start and end:
		filters["creation"] = ("between", [f"{start} 00:00:00", f"{end} 23:59:59.999999"])
	elif start:
		filters["creation"] = (">=", f"{start} 00:00:00")
	elif end:
		filters["creation"] = ("<=", f"{end} 23:59:59.999999")

	rows = frappe.get_all(
		"GX Audit Log",
		filters=filters,
		fields=["name", "creation", "actor", "action", "reference_doctype", "reference_name", "details"],
		order_by="creation desc",
		limit_start=(page - 1) * page_size,
		limit_page_length=page_size,
	)
	total = frappe.db.count("GX Audit Log", filters)
	type_of = {doctype: kind for kind, doctype in ENTITY_DOCTYPES.items()}
	type_of.update({doctype: "content" for doctype in CONTENT_DOCTYPES})
	labels = {}
	for doctype, field in ENTITY_LABELS.items():
		names = [r.reference_name for r in rows if r.reference_doctype == doctype and r.reference_name]
		if names:
			labels[doctype] = dict(
				frappe.get_all(doctype, {"name": ("in", names)}, ["name", field], as_list=True)
			)
	items = []
	for r in rows:
		kind = type_of.get(r.reference_doctype)
		items.append(
			{
				"id": r.name,
				"at": iso(r.creation),
				"actor": r.actor,
				"action": r.action.removeprefix(f"{kind}.") if kind else r.action,
				"entityType": kind,
				"entityId": r.reference_name,
				"entityLabel": labels.get(r.reference_doctype, {}).get(r.reference_name),
				"details": r.details,
			}
		)
	return page_meta(page, page_size, total, items)


# ---------------------------------------------------------------- partners

PARTNER_STATUSES = ("pending_approval", "active", "suspended")
# action: (statuses it applies to, new status)
PARTNER_ACTIONS = {
	"approve": (("pending_approval",), "active"),
	"suspend": (("pending_approval", "active"), "suspended"),
	"reactivate": (("suspended",), "active"),
}
PARTNER_FIELDS = ["name", "partner_name", "user", "company", "referral_code", "status", "tier", "creation"]


def _partner_numbers(partner_names: list[str]) -> dict:
	"""{partner: {referred, paying, mrr}} from the customers each partner referred."""
	numbers = {name: {"referred": 0, "paying": 0, "mrr": 0.0} for name in partner_names}
	if not numbers:
		return numbers
	# Never filter on `in [""]`: Frappe matches NULL with it, so customers with no partner came back.
	accounts = [
		a
		for a in frappe.get_all(
			"GX Account", {"referred_by": ("in", partner_names)}, ["name", "referred_by", "status"]
		)
		if a.referred_by in numbers
	]
	if not accounts:
		return numbers
	for a in accounts:
		numbers[a.referred_by]["referred"] += 1
		if a.status == "active":
			numbers[a.referred_by]["paying"] += 1
	partner_of = {a.name: a.referred_by for a in accounts}
	for sub in frappe.get_all(
		"GX Subscription",
		{"account": ("in", list(partner_of)), "status": "active"},
		["account", "price", "gst_included", "gst_rate", "billing_interval"],
	):
		numbers[partner_of[sub.account]]["mrr"] += _mrr(sub)
	return numbers


def _admin_partner(p, email, numbers) -> dict:
	return {
		"id": p.name,
		"name": p.partner_name,
		"email": email,
		"company": p.company or None,
		"referralCode": p.referral_code,
		"status": p.status,
		"tier": p.tier or None,
		"joinedAt": iso(p.creation),
		"referredCustomers": numbers["referred"],
		"payingCustomers": numbers["paying"],
		"mrr": money(numbers["mrr"]),
	}


def _emails(users) -> dict:
	return dict(
		frappe.get_all(
			"User", {"name": ("in", [u for u in users if u] or [""])}, ["name", "email"], as_list=True
		)
	)


@endpoint(methods=("GET",), roles=("admin",))
def partners():
	data = query()
	page, page_size = page_params(data)
	status = get_choice(data, "status", PARTNER_STATUSES)
	q = get_str(data, "q", max_length=100)
	order_by = get_sort(data, {"joinedAt": "creation", "name": "partner_name"}, "-joinedAt")

	filters = {"status": status} if status else {}
	or_filters = None
	if q:
		or_filters = {
			"partner_name": ("like", f"%{q}%"),
			"company": ("like", f"%{q}%"),
			"referral_code": ("like", f"%{q}%"),
			"user": ("like", f"%{q}%"),
		}
	rows = frappe.get_all(
		"GX Partner Profile",
		filters=filters,
		or_filters=or_filters,
		fields=PARTNER_FIELDS,
		order_by=order_by,
		limit_start=(page - 1) * page_size,
		limit_page_length=page_size,
	)
	total = _count("GX Partner Profile", filters, or_filters)
	numbers = _partner_numbers([r.name for r in rows])
	emails = _emails([r.user for r in rows])
	return page_meta(
		page, page_size, total, [_admin_partner(r, emails.get(r.user), numbers[r.name]) for r in rows]
	)


def _partner_detail(partner_id: str) -> dict:
	p = frappe.db.get_value(
		"GX Partner Profile", partner_id, [*PARTNER_FIELDS, "phone", "city", "gstin"], as_dict=True
	)
	result = _admin_partner(p, _emails([p.user]).get(p.user), _partner_numbers([p.name])[p.name])
	result.update({"phone": p.phone or None, "city": p.city or None, "gstin": p.gstin or None})

	accounts = frappe.get_all(
		"GX Account",
		{"referred_by": p.name},
		["name", "account_name", "contact_email", "status", "creation"],
		order_by="creation desc",
	)
	subs = {}
	for sub in frappe.get_all(
		"GX Subscription",
		{"account": ("in", [a.name for a in accounts] or [""])},
		["account", "plan", "status", "price", "gst_included", "gst_rate", "billing_interval"],
		order_by="creation asc",
	):
		subs[sub.account] = sub  # latest wins
	plan_names = dict(frappe.get_all("GX Plan", fields=["name", "plan_name"], as_list=True))
	result["customers"] = [
		{
			"id": a.name,
			"company": a.account_name,
			"contactEmail": a.contact_email,
			"status": a.status,
			"plan": plan_names.get(subs[a.name].plan) if a.name in subs else None,
			"mrr": money(_mrr(subs[a.name]) if a.name in subs and subs[a.name].status == "active" else 0),
			"createdAt": iso(a.creation),
		}
		for a in accounts
	]
	result["history"] = [
		{
			"at": iso(r.creation),
			"actor": r.actor,
			"action": r.action.removeprefix("partner."),
			"details": r.details,
		}
		for r in frappe.get_all(
			"GX Audit Log",
			filters={"reference_doctype": "GX Partner Profile", "reference_name": p.name},
			fields=["creation", "actor", "action", "details"],
			order_by="creation asc",
		)
	]
	return result


@endpoint(methods=("GET",), roles=("admin",))
def partner():
	partner_id = get_str(query(), "id", required=True)
	if not frappe.db.exists("GX Partner Profile", partner_id):
		raise not_found()
	return _partner_detail(partner_id)


@endpoint(methods=("POST",), roles=("admin",), idempotent=True)
def update_partner_status():
	"""approve (pending → active: creates the ERPNext Sales Partner and emails the partner),
	suspend {reason} (pending or active → suspended: their referral code stops working),
	reactivate (suspended → active). Returns the partner's details."""
	data = body()
	partner_id = get_str(data, "id", required=True)
	action = get_choice(data, "action", tuple(PARTNER_ACTIONS), required=True)
	reason = get_str(data, "reason", required=action == "suspend", max_length=500, label="Reason")
	if not frappe.db.exists("GX Partner Profile", partner_id):
		raise not_found()

	doc = frappe.get_doc("GX Partner Profile", partner_id, for_update=True)
	allowed_from, new_status = PARTNER_ACTIONS[action]
	if doc.status not in allowed_from:
		done = {"approve": "approved", "suspend": "suspended", "reactivate": "reactivated"}[action]
		state = doc.status.replace("_", " ")
		raise ApiError(409, "not_changeable", f"A partner who is {state} can't be {done}.")
	previous = doc.status
	doc.status = new_status
	doc.save(ignore_permissions=True)
	audit(
		f"partner.{action}",
		"GX Partner Profile",
		doc.name,
		f"{previous} → {new_status}" + (f": {reason}" if reason else ""),
	)
	if action == "approve":
		_email_partner_approved(doc)
	return _partner_detail(doc.name)


def _email_partner_approved(doc):
	from growthx_core.api.v1._core import web_url

	email = frappe.db.get_value("User", doc.user, "email") if doc.user else None
	if not email:
		return
	esc = frappe.utils.escape_html
	login_url = web_url("/login")
	try:
		frappe.sendmail(
			recipients=[email],
			subject="Your GrowthX partner account is approved",
			message=(
				f"<p>Hi {esc(doc.partner_name)},</p>"
				"<p>Your GrowthX partner account is approved.</p>"
				f"<p>Your referral code: <b>{esc(doc.referral_code)}</b></p>"
				f'<p><a href="{esc(login_url)}">Sign in</a> to see your referral link and the customers you refer.</p>'
			),
		)
	except Exception:
		# The approval stands; admins see the failure in the Error Log.
		frappe.log_error(title=f"GrowthX: could not send partner-approved email for {doc.name}")


# ---------------------------------------------------------------- partner commissions and payouts

COMMISSION_STATUSES = ("pending", "approved", "paid", "void")
PAYOUT_STATUSES = ("draft", "paid", "cancelled")
PAYOUT_METHODS = ("bank_transfer", "upi", "other")
MAX_BULK = 200


def _month_range(data: dict, key: str = "month"):
	value = get_str(data, key, max_length=7)
	if not value:
		return None
	try:
		return commission_ops.valid_period(value)
	except (ValueError, TypeError):
		raise validation_error({key: "Use a month as YYYY-MM."})


def _ids(data: dict) -> list[str]:
	ids = data.get("ids")
	if (
		not isinstance(ids, list)
		or not ids
		or len(ids) > MAX_BULK
		or not all(isinstance(i, str) and i for i in ids)
	):
		raise validation_error({"ids": f"Send between 1 and {MAX_BULK} commission ids."})
	return list(dict.fromkeys(ids))


@endpoint(methods=("GET",), roles=("admin",))
def commissions():
	data = query()
	page, page_size = page_params(data)
	status = get_choice(data, "status", COMMISSION_STATUSES)
	partner_id = get_str(data, "partner", max_length=140)
	month = _month_range(data)
	order_by = get_sort(data, {"earnedAt": "paid_on", "amount": "amount"}, "-earnedAt")

	filters = {}
	if partner_id:
		filters["partner"] = partner_id
	if month:
		filters["paid_on"] = ("between", [month[0], month[1]])
	list_filters = {**filters, **({"status": status} if status else {})}
	rows = frappe.get_all(
		"GX Commission",
		filters=list_filters,
		fields=ledger.COMMISSION_FIELDS,
		order_by=f"{order_by}, creation desc",
		limit_start=(page - 1) * page_size,
		limit_page_length=page_size,
	)
	# Totals per status for the same partner/month filter, so the page can show how much is waiting.
	totals = dict.fromkeys(COMMISSION_STATUSES, 0)
	for r in frappe.get_all("GX Commission", filters=filters, fields=["status", "amount"]):
		totals[r.status] += commission_ops.to_paise(r.amount)
	customers, partners_ = ledger.account_names(rows), ledger.partner_names(rows)
	result = page_meta(
		page,
		page_size,
		frappe.db.count("GX Commission", list_filters),
		[ledger.admin_commission(r, customers, partners_) for r in rows],
	)
	result["totals"] = {k: {"amount": v, "currency": "INR"} for k, v in totals.items()}
	return result


def _bulk_result(done: list[str], skipped: list[dict]) -> dict:
	return {"updated": len(done), "ids": done, "skipped": skipped}


@endpoint(methods=("POST",), roles=("admin",), idempotent=True)
def approve_commissions():
	"""Approve pending commissions (up to 200 ids). A commission that isn't pending, or whose hold
	period (Partner Program Settings) hasn't ended, is skipped and named in `skipped`. Audited."""
	ids = _ids(body())
	done, skipped = commission_ops.approve(ids, frappe.session.user)
	for name in done:
		audit("commission.approve", "GX Commission", name, "pending → approved")
	_notify_approved(done)
	return _bulk_result(done, skipped)


def _notify_approved(names: list[str]):
	from growthx_core.partners.notifications import notify

	if not names:
		return
	totals: dict[str, list] = {}
	for r in frappe.get_all("GX Commission", {"name": ("in", names)}, ["partner", "amount"]):
		totals.setdefault(r.partner, []).append(commission_ops.to_paise(r.amount))
	for partner_id, amounts in totals.items():
		user = frappe.db.get_value("GX Partner Profile", partner_id, "user")
		if user:
			notify(
				user,
				"commission",
				"Commission approved",
				f"{fmt_money(commission_ops.to_rupees(sum(amounts)), currency='INR')} approved"
				f" ({len(amounts)} payment{'s' if len(amounts) != 1 else ''}), to be paid in the next payout.",
				deep_link_type="earnings",
			)


@endpoint(methods=("POST",), roles=("admin",), idempotent=True)
def void_commissions():
	"""Void commissions that aren't paid yet (up to 200 ids), with a reason (e.g. a refund). One in a
	draft payout leaves it and the payout is recalculated. Paid ones are skipped. Audited."""
	data = body()
	ids = _ids(data)
	reason = get_str(data, "reason", required=True, max_length=500, label="Reason")
	done, skipped = commission_ops.void(ids, reason)
	for name in done:
		audit("commission.void", "GX Commission", name, reason)
	return _bulk_result(done, skipped)


@endpoint(methods=("GET",), roles=("admin",))
def payouts():
	data = query()
	page, page_size = page_params(data)
	status = get_choice(data, "status", PAYOUT_STATUSES)
	partner_id = get_str(data, "partner", max_length=140)
	period = get_str(data, "period", max_length=7)
	if period:
		try:
			commission_ops.valid_period(period)
		except (ValueError, TypeError):
			raise validation_error({"period": "Use a month as YYYY-MM."})
	filters = {k: v for k, v in {"status": status, "partner": partner_id, "period": period}.items() if v}
	rows = frappe.get_all(
		"GX Payout",
		filters=filters,
		fields=ledger.PAYOUT_FIELDS,
		order_by="creation desc",
		limit_start=(page - 1) * page_size,
		limit_page_length=page_size,
	)
	names = ledger.partner_names(rows)
	return page_meta(
		page, page_size, frappe.db.count("GX Payout", filters), [ledger.payout(r, names) for r in rows]
	)


@endpoint(methods=("POST",), roles=("admin",), idempotent=True)
def create_payouts():
	"""Payout run for a month `{period: "YYYY-MM"}`: each active partner whose approved, unpaid
	commission on payments up to the end of that month reaches the minimum payout gets a draft payout
	(gross, TDS, net). Smaller balances are listed in `skipped` and carry over. Repeating it never
	includes a commission twice."""
	period = get_str(body(), "period", required=True, max_length=7, label="Period")
	try:
		commission_ops.valid_period(period)
	except (ValueError, TypeError):
		raise validation_error({"period": "Use a month as YYYY-MM."})
	if period > nowdate()[:7]:
		raise validation_error({"period": "A payout can't be made for a month that hasn't started."})
	created, skipped = commission_ops.create_payouts(period, frappe.session.user)
	for p in created:
		audit(
			"payout.create",
			"GX Payout",
			p.name,
			f"{p.period}: {p.commission_count} commissions, gross {fmt_money(p.gross, currency='INR')},"
			f" TDS {fmt_money(p.tds, currency='INR')} ({p.tds_rate:g}%), net {fmt_money(p.net, currency='INR')}",
		)
	names = ledger.partner_names(created)
	return {
		"created": [ledger.payout(p, names) for p in created],
		"skipped": [{**r, "amount": {"amount": r["amount"], "currency": "INR"}} for r in skipped],
	}


def _draft_payout(payout_id: str):
	if not frappe.db.exists("GX Payout", payout_id):
		raise not_found()
	doc = frappe.get_doc("GX Payout", payout_id, for_update=True)
	if doc.status != "draft":
		raise ApiError(409, "not_draft", f"This payout is already {doc.status}.")
	return doc


@endpoint(methods=("POST",), roles=("admin",), idempotent=True)
def mark_payout_paid():
	"""Record that a draft payout was transferred `{id, reference, paidOn, method}`: the payout and
	its commissions become paid and the partner is told (in the app, and by email if they opted in to
	commission updates). A payout that is already paid or cancelled answers 409."""
	data = body()
	payout_id = get_str(data, "id", required=True)
	reference = get_str(data, "reference", required=True, max_length=140, label="UTR / reference")
	paid_on = _paid_on(data.get("paidOn"))
	method = get_choice(data, "method", PAYOUT_METHODS, required=True)
	doc = _draft_payout(payout_id)
	count = commission_ops.mark_paid(doc.name, reference, paid_on, method, frappe.session.user)
	audit(
		"payout.mark_paid",
		"GX Payout",
		doc.name,
		f"{fmt_money(doc.net, currency='INR')} net ({count} commissions) paid on {paid_on.isoformat()}"
		f" by {method}, reference {reference}",
	)
	doc.reload()
	commission_ops.notify_payout_paid(doc)
	return ledger.payout(doc, ledger.partner_names([doc]))


@endpoint(methods=("POST",), roles=("admin",), idempotent=True)
def cancel_payout():
	"""Drop a draft payout `{id}`: its commissions go back to approved, ready for the next run."""
	doc = _draft_payout(get_str(body(), "id", required=True))
	commission_ops.cancel_payout(doc.name)
	audit("payout.cancel", "GX Payout", doc.name, f"{doc.period} draft cancelled")
	doc.reload()
	return ledger.payout(doc, ledger.partner_names([doc]))


# ---------------------------------------------------------------- appointments ("Talk to an expert")

APPOINTMENT_STATUSES = {
	"confirmed": "Confirmed",
	"completed": "Completed",
	"cancelled": "Cancelled",
	"no_show": "No Show",
}
APPOINTMENT_FIELDS = [
	"name",
	"status",
	"start",
	"end",
	"full_name",
	"email",
	"phone",
	"company",
	"app",
	"notes",
	"expert",
	"meeting_url",
	"lead",
	"creation",
]


def _day(data: dict, key: str):
	value = get_str(data, key, max_length=10)
	if not value:
		return None
	try:
		return frappe.utils.getdate(value)
	except Exception:
		raise validation_error({key: "Use a date like 2026-10-14."})


CONTACT_SOURCES = ("contact", "support")


@endpoint(methods=("GET",), roles=("admin",))
def contact_requests():
	"""Admin → Messages: Contact and Support form messages, newest first. Read-only; replies and
	"handled" live in the support mailbox."""
	data = query()
	page, page_size = page_params(data)
	source = get_choice(data, "source", CONTACT_SOURCES)
	q = get_str(data, "q", max_length=100)
	start, end = _day(data, "from"), _day(data, "to")
	if start and end and start > end:
		raise validation_error({"from": "from must be on or before to."})

	filters = {}
	if source:
		filters["source"] = source
	if start and end:
		filters["creation"] = ("between", [f"{start} 00:00:00", f"{end} 23:59:59.999999"])
	elif start:
		filters["creation"] = (">=", f"{start} 00:00:00")
	elif end:
		filters["creation"] = ("<=", f"{end} 23:59:59.999999")
	or_filters = (
		{field: ("like", f"%{q}%") for field in ("full_name", "email", "company", "message")} if q else None
	)
	rows = frappe.get_all(
		"GX Contact Request",
		filters=filters,
		or_filters=or_filters,
		fields=["name", "full_name", "email", "company", "message", "source", "customer", "creation"],
		order_by="creation desc",
		limit_start=(page - 1) * page_size,
		limit_page_length=page_size,
	)
	items = [
		{
			"id": r.name,
			"name": r.full_name,
			"email": r.email,
			"company": r.company or None,
			"message": r.message,
			"source": r.source or "contact",
			"customerId": r.customer or None,
			"createdAt": iso(r.creation),
		}
		for r in rows
	]
	return page_meta(page, page_size, _count("GX Contact Request", filters, or_filters), items)


@endpoint(methods=("GET",), roles=("admin",))
def appointments():
	"""Bookings, filtered by status, expert, date range (site timezone, inclusive) and search."""
	data = query()
	page, page_size = page_params(data)
	status = get_choice(data, "status", tuple(APPOINTMENT_STATUSES))
	expert = get_str(data, "expert", max_length=140)
	q = get_str(data, "q", max_length=100)
	date_from, date_to = _day(data, "from"), _day(data, "to")
	order_by = get_sort(data, {"start": "start", "createdAt": "creation"}, "start")

	filters = {}
	if status:
		filters["status"] = APPOINTMENT_STATUSES[status]
	if expert:
		filters["expert"] = expert
	if date_from and date_to:
		filters["start"] = ("between", [f"{date_from} 00:00:00", f"{date_to} 23:59:59"])
	elif date_from:
		filters["start"] = (">=", f"{date_from} 00:00:00")
	elif date_to:
		filters["start"] = ("<=", f"{date_to} 23:59:59")
	or_filters = (
		{field: ("like", f"%{q}%") for field in ("full_name", "email", "company", "phone")} if q else None
	)
	rows = frappe.get_all(
		"GX Appointment",
		filters=filters,
		or_filters=or_filters,
		fields=APPOINTMENT_FIELDS,
		order_by=order_by,
		limit_start=(page - 1) * page_size,
		limit_page_length=page_size,
	)
	total = _count("GX Appointment", filters, or_filters)
	return page_meta(page, page_size, total, [s.admin_appointment(r) for r in rows])


@endpoint(methods=("GET",), roles=("admin",))
def appointment():
	appointment_id = get_str(query(), "id", required=True)
	if not frappe.db.exists("GX Appointment", appointment_id):
		raise not_found()
	doc = frappe.get_doc("GX Appointment", appointment_id)
	return s.admin_appointment_detail(doc, appts.history(doc))


@endpoint(methods=("POST",), roles=("admin",), idempotent=True)
def update_appointment():
	"""One workflow action on a booking: reschedule {start}, reassign {expert}, cancel {reason?},
	complete, no_show, note {note}. Returns the updated booking."""
	data = body()
	appointment_id = get_str(data, "id", required=True)
	if not frappe.db.exists("GX Appointment", appointment_id):
		raise not_found()
	action = get_choice(data, "action", appts.ACTIONS, required=True)
	raw_start = get_str(data, "start", max_length=40, label="Start")
	doc = frappe.get_doc("GX Appointment", appointment_id)
	appts.change(
		doc,
		action,
		by="Team",
		start=appts._parse_start(raw_start) if raw_start else None,
		expert=get_str(data, "expert", max_length=140, label="Expert"),
		reason=get_str(data, "reason", max_length=500, label="Reason"),
		note=get_str(data, "note", max_length=2000, label="Note"),
	)
	doc.reload()
	return s.admin_appointment_detail(doc, appts.history(doc))


@endpoint(methods=("GET",), roles=("admin",))
def appointment_experts():
	"""Experts from GX Appointment Settings, with their upcoming confirmed calls."""
	experts = frappe.get_single("GX Appointment Settings").expert_list()
	upcoming = frappe.get_all(
		"GX Appointment",
		filters={
			"status": "Confirmed",
			"start": (">=", frappe.utils.now_datetime()),
			"expert": ("in", experts or [""]),
		},
		pluck="expert",
	)
	names = dict(
		frappe.get_all("User", {"name": ("in", experts or [""])}, ["name", "full_name"], as_list=True)
	)
	return {
		"items": [{"email": e, "name": names.get(e) or e, "upcoming": upcoming.count(e)} for e in experts]
	}


# Website content for the /content portal lives in its own module; its endpoints are admin.*.
from growthx_core.api.v1.admin_content import (
	content_post,
	content_posts,
	delete_content_post,
	save_content_post,
)
from growthx_core.api.v1.admin_content_media import upload_content_image
from growthx_core.api.v1.admin_content_seo import (
	content_pages,
	content_sites,
	delete_content_page,
	save_content_page,
	update_content_site,
)
from growthx_core.api.v1.admin_support import (
	support_note,
	support_reply,
	support_summary,
	support_ticket,
	support_tickets,
	support_update,
)

# ---------------------------------------------------------------- team (staff)

STAFF_ROLES = {"admin": "GrowthX Admin", "content_editor": "GrowthX Content Editor"}
SHARED_ROLES = ("GrowthX Customer", "GrowthX Partner")  # roles of an account that isn't only staff
PROTECTED_USERS = ("Administrator", "Guest")  # never listed, changed or removed here


def _staff_names() -> list[str]:
	names = frappe.get_all(
		"Has Role",
		{"parenttype": "User", "role": ("in", list(STAFF_ROLES.values()))},
		pluck="parent",
		distinct=True,
	)
	return [n for n in names if n not in PROTECTED_USERS]


def _staff_role(user: str) -> str | None:
	"""admin or content_editor (admin wins), from the user's own role rows."""
	roles = set(frappe.get_all("Has Role", {"parenttype": "User", "parent": user}, pluck="role"))
	for key, role in STAFF_ROLES.items():
		if role in roles:
			return key
	return None


def _staff_member(user: str) -> dict:
	row = frappe.db.get_value(
		"User", user, ["name", "email", "full_name", "enabled", "last_login", "creation"], as_dict=True
	)
	# Frappe adds its own "frappe" row (the site's login), which isn't a way into GrowthX.
	providers = frappe.get_all(
		"User Social Login",
		{"parent": user, "parenttype": "User", "provider": ("!=", "frappe")},
		pluck="provider",
		order_by="idx asc",
	)
	return {
		"id": row.name,
		"email": row.email or row.name,
		"name": row.full_name or row.name,
		"role": _staff_role(user),
		"enabled": bool(row.enabled),
		"lastLogin": iso(row.last_login),
		"createdAt": iso(row.creation),
		"signIn": {"password": accounts.has_password(user), "providers": sorted(set(providers))},
		"isYou": user == frappe.session.user,
		"hasOtherRoles": _has_shared_roles(user),
	}


def _has_shared_roles(user: str) -> bool:
	return bool(
		frappe.db.exists(
			"Has Role", {"parenttype": "User", "parent": user, "role": ("in", list(SHARED_ROLES))}
		)
	)


def _enabled_admins() -> set[str]:
	admins = frappe.get_all(
		"Has Role", {"parenttype": "User", "role": STAFF_ROLES["admin"]}, pluck="parent", distinct=True
	)
	if not admins:
		return set()
	enabled = frappe.get_all("User", {"name": ("in", admins), "enabled": 1}, pluck="name")
	return {n for n in enabled if n not in PROTECTED_USERS}


def _staff_target(data: dict) -> str:
	"""The staff member `id` names (their email); 404 for anyone else, and always for Administrator/Guest."""
	user_id = get_str(data, "id", required=True, label="Id")
	user_id = frappe.db.get_value("User", {"name": user_id}, "name")
	if not user_id or user_id in PROTECTED_USERS or not _staff_role(user_id):
		raise not_found()
	return user_id


def _guard_admin_loss(user: str, what: str):
	"""An admin can't lock themselves out, and the last enabled admin can't go."""
	if user not in _enabled_admins():
		return
	if user == frappe.session.user:
		raise ApiError(409, "self_lockout", f"You can't {what} your own account.")
	if _enabled_admins() == {user}:
		raise ApiError(409, "last_admin", f"This is the last enabled admin, so you can't {what} them.")


def _save_user(doc):
	doc.flags.no_welcome_mail = True
	doc.save(ignore_permissions=True)


def _set_staff_role(doc, key: str | None):
	"""Keep the user's other roles; give them exactly this staff role (None removes both)."""
	staff = set(STAFF_ROLES.values())
	doc.set("roles", [r for r in doc.get("roles") if r.role not in staff])
	if key:
		doc.append("roles", {"role": STAFF_ROLES[key]})


def _send_staff_invite(user: str, name: str, role: str) -> bool:
	key = frappe.generate_hash()
	frappe.db.set_value(
		"User",
		user,
		{"reset_password_key": sha256_hash(key), "last_reset_password_key_generated_on": now_datetime()},
	)
	esc = frappe.utils.escape_html
	link = web_url(f"/reset-password?key={key}")
	try:
		frappe.sendmail(
			recipients=[user],
			subject="You're invited to the GrowthX team",
			now=True,
			message=(
				f"<p>Hi {esc(name)},</p>"
				f"<p>You've been added to GrowthX as {esc(role)}.</p>"
				f'<p><a href="{esc(link)}">Set your password</a>. The link works once and expires in 24 hours. '
				'If it has expired, use "Forgot password" on the sign-in page.</p>'
			),
		)
	except Exception:
		frappe.log_error(title=f"GrowthX: could not send staff invite to {user}")
		return False
	return True


@endpoint(methods=("GET",), roles=("admin",))
def staff():
	"""Everyone with the GrowthX Admin or Content Editor role, with how they sign in."""
	members = [_staff_member(n) for n in _staff_names()]
	members.sort(key=lambda m: (m["name"].lower(), m["email"]))
	return page_meta(1, max(len(members), 1), len(members), members)


@endpoint(methods=("POST",), roles=("admin",), idempotent=True)
def add_staff():
	"""Add a team member {email, name, role admin|content_editor}. A new person gets an account with no
	password and an email to set one (invited true unless sending failed); an existing user (e.g. a
	customer) just gets the role. 409 already_staff / user_disabled."""
	data = body()
	email = get_email(data)
	name = get_str(data, "name", required=True, max_length=140, label="Name")
	role = get_choice(data, "role", tuple(STAFF_ROLES), required=True)
	existing = frappe.db.get_value("User", {"email": email}, "name")
	if existing in PROTECTED_USERS:
		raise validation_error({"email": "Enter a valid email address."})
	invited = False
	if existing:
		if _staff_role(existing):
			raise ApiError(409, "already_staff", "This person is already on the team.")
		if not frappe.db.get_value("User", existing, "enabled"):
			raise ApiError(409, "user_disabled", "This account is disabled. Enable it before adding a role.")
		doc = frappe.get_doc("User", existing, for_update=True)
		_set_staff_role(doc, role)
		_save_user(doc)
		user = doc.name
	else:
		first, _, last = name.partition(" ")
		doc = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": first,
				"last_name": last or None,
				"user_type": "Website User",
				"send_welcome_email": 0,
				"roles": [{"role": STAFF_ROLES[role]}],
			}
		)
		doc.flags.no_welcome_mail = True
		doc.insert(ignore_permissions=True)
		user = doc.name
		accounts.mark_no_password(user)
		invited = _send_staff_invite(user, name, STAFF_ROLES[role])
	audit("staff.add", "User", user, f"{role}" + (", invited" if invited else ""))
	return 201, {**_staff_member(user), "invited": invited}


@endpoint(methods=("POST",), roles=("admin",), idempotent=True)
def update_staff():
	"""Change a team member's role and/or enable or disable them: {id, role?, enabled?}. Disabling or
	demoting revokes their tokens. You can't disable or demote yourself, nor the last enabled admin."""
	data = body()
	user = _staff_target(data)
	role = get_choice(data, "role", tuple(STAFF_ROLES))
	enabled = data.get("enabled")
	if enabled is not None and not isinstance(enabled, bool):
		raise validation_error({"enabled": "enabled must be true or false."})
	if role is None and enabled is None:
		raise validation_error({"role": "Send a role or enabled."})

	doc = frappe.get_doc("User", user, for_update=True)
	current = _staff_role(user)
	changes = []
	revoke = False
	if role and role != current:
		if current == "admin":
			_guard_admin_loss(user, "demote")
		_set_staff_role(doc, role)
		changes.append(f"role {current} → {role}")
		revoke = True
	if enabled is not None and enabled != bool(doc.enabled):
		if not enabled:
			if _has_shared_roles(user):
				raise ApiError(
					409,
					"shared_account",
					"This person also has a customer or partner account, and disabling would lock that out. "
					"Remove them from the team instead.",
				)
			_guard_admin_loss(user, "disable")
		doc.enabled = 1 if enabled else 0
		changes.append("enabled" if enabled else "disabled")
		revoke = revoke or not enabled
	if changes:
		_save_user(doc)
		if revoke:
			tokens.revoke_all(user)
		audit("staff.update", "User", user, ", ".join(changes))
	return _staff_member(user)


@endpoint(methods=("POST",), roles=("admin",), idempotent=True)
def remove_staff():
	"""Take the team roles off {id} and revoke their tokens. The user itself (and any customer or
	partner role) is kept."""
	user = _staff_target(body())
	_guard_admin_loss(user, "remove")
	doc = frappe.get_doc("User", user, for_update=True)
	_set_staff_role(doc, None)
	_save_user(doc)
	tokens.revoke_all(user)
	audit("staff.remove", "User", user, "staff roles removed")
	return {"ok": True}


# ERPNext Direct onboarding (add a customer's existing ERPNext site, send the payment link).
from growthx_core.api.v1.admin_direct import add_direct_site, send_payment_link
