"""ERPNext Direct onboarding in the admin panel (served as admin.add_direct_site and
admin.send_payment_link; see docs/api-decisions.md, decision 13).

The team adds a customer's ERPNext site that already runs on our Press, attaches a plan and the customer
pays through GrowthX with PayU. Nothing is installed on, listed from or pulled from the site: its GX Tenant
is `imported`, with no `press_site_id`, so no Press action can ever reach it.
"""

from datetime import date, datetime, time

import frappe
from frappe.utils import add_days, fmt_money, getdate, now_datetime, nowdate

from growthx_core import accounts, direct
from growthx_core.api.v1 import _serializers as s
from growthx_core.api.v1._core import (
	ApiError,
	RateLimit,
	audit,
	body,
	endpoint,
	get_email,
	get_str,
	not_found,
	validation_error,
)
from growthx_core.billing import billing_details, payments
from growthx_core.billing import payu as payu_billing

MAX_START_DAYS = 366


def _plan(plan_id: str):
	plan = frappe.db.get_value("GX Plan", plan_id, "*", as_dict=True) if plan_id else None
	if not plan:
		raise validation_error({"plan": "Unknown plan."})
	if not plan.is_active:
		raise validation_error({"plan": "That plan isn't on sale."})
	if not direct.is_direct_product(plan.product):
		raise validation_error({"plan": "Choose an ERPNext Direct plan."})
	return plan


def _billing_start(data: dict) -> date | None:
	"""None: the first invoice is due now. A date from tomorrow: billing starts then (up to a year ahead)."""
	value = data.get("billingStart", "now")
	if value in (None, "", "now"):
		return None
	try:
		start = date.fromisoformat(value) if isinstance(value, str) and len(value) == 10 else None
	except ValueError:
		start = None
	if not start:
		raise validation_error({"billingStart": "Enter now or a date as YYYY-MM-DD."})
	if start <= getdate(nowdate()):
		return None
	if start > getdate(add_days(nowdate(), MAX_START_DAYS)):
		raise validation_error({"billingStart": "Billing can start at most a year from now."})
	return start


def _paid_offline(data: dict) -> dict | None:
	from growthx_core.api.v1.admin import _paid_on

	paid = data.get("paidOffline")
	if paid in (None, False):
		return None
	if not isinstance(paid, dict):
		raise validation_error({"paidOffline": "Send the payment reference, amount and date."})
	amount = paid.get("amount")
	if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
		raise validation_error({"paidOffline": "Enter the amount paid in paise, a whole number above 0."})
	return {
		"reference": get_str(paid, "reference", required=True, max_length=140, label="Reference"),
		"amount": amount,
		"paid_on": _paid_on(paid.get("paidOn")),
	}


def _billing_inputs(data: dict) -> dict | None:
	"""The optional GSTIN and billing address, for the customer's tax invoices."""
	address = data.get("billingAddress")
	gstin = data.get("gstin")
	if address in (None, {}) and not gstin:
		return None
	if not isinstance(address, dict):
		raise validation_error({"billingAddress": "A GSTIN needs the billing address too."})
	return {**address, "gstin": gstin or None, "billingName": data.get("billingName")}


def _existing_replay(tenant_name: str, email: str, plan) -> dict | None:
	"""The same request sent again (a retry after a timeout): the tenant exists for this very customer."""
	tenant = frappe.get_doc("GX Tenant", tenant_name)
	account = frappe.db.get_value("GX Account", tenant.account, ["name", "contact_email"], as_dict=True)
	sub = frappe.db.get_value("GX Subscription", tenant.subscription, "plan") if tenant.subscription else None
	if tenant.origin == "imported" and account and account.contact_email == email and sub == plan.name:
		return {"tenant": tenant, "account": account.name}
	return None


@endpoint(methods=("POST",), roles=("admin",), idempotent=True)
def add_direct_site():
	"""Add a customer's existing ERPNext site (ERPNext Direct) and bill it. Creates, or reuses by email,
	the customer's GX Account, Customer and login (no password: they get an email to set one), their
	subscription (awaiting payment, or `trialing` until a later billing start) and an `imported` GX Tenant
	that has nothing installed. The domain is typed by the admin; `confirmOnPress` (default true) checks it
	with one exact-name lookup on Press: not found refuses it, Press unreachable allows it with a warning.
	`paidOffline` {reference, amount, paidOn} records a payment already received. Sending the same request
	again returns the tenant already added (200)."""
	from growthx_core.api.v1 import admin

	data = body()
	company = get_str(data, "company", required=True, max_length=140, label="Company name")
	contact = get_str(data, "contactName", required=True, max_length=140, label="Contact name")
	email = get_email(data)
	phone = get_str(data, "phone", max_length=20, label="Phone")
	host = direct.normalize_site(data.get("site"))
	plan = _plan(get_str(data, "plan", required=True))
	start = _billing_start(data)
	offline = _paid_offline(data)
	billing = _billing_inputs(data)
	confirm = data.get("confirmOnPress", True)
	if not isinstance(confirm, bool):
		raise validation_error({"confirmOnPress": "Must be true or false."})
	if offline and start:
		raise validation_error(
			{"paidOffline": "A payment already received can't have a later billing start."}
		)

	taken = direct.site_taken(host)
	if taken:
		again = _existing_replay(taken, email, plan)
		if again:
			return 200, _result(again["tenant"], again["account"], created=False)
		raise ApiError(
			409, "site_exists", "This site is already added in GrowthX.", fields={"site": "Already added."}
		)

	account_name = direct.existing_customer(email)
	if account_name and accounts.live_site(account_name, plan.product):
		raise ApiError(
			409,
			"already_added",
			"This customer already has a site on that product. Use another ERPNext Direct product for a second site.",
			fields={"plan": "Already has a site on this product."},
		)

	warnings = []
	if plan.gst_rate and not plan.gst_included:
		# Online GST invoices need GST-inclusive prices (billing.tax_invoice.apply): PayU would answer 503.
		warnings.append("plan_not_payable_online")
	press_check = "not_checked"
	if confirm:
		press_check = direct.check_on_press(host)
		if press_check == "not_found":
			raise validation_error(
				{
					"site": "Press has no site with this name. Check the domain, or add it without the Press check."
				}
			)
		if press_check == "unreachable":
			warnings.append("press_unreachable")

	created_account = not account_name
	if created_account:
		user, account_name = direct.new_customer(
			email=email, contact_name=contact, company=company, phone=phone
		)
	account = frappe.get_doc("GX Account", account_name, for_update=True)
	user = account.user

	if billing:
		if billing_details.available():
			payu_billing.ensure_customer(account)
			billing_details.save(account, billing)
		else:
			warnings.append("billing_details_not_saved")

	sub = frappe.get_doc(
		{
			"doctype": "GX Subscription",
			"account": account.name,
			"plan": plan.name,
			"status": "trialing" if start else "past_due",
			"price": plan.price,
			"gst_included": plan.gst_included,
			"gst_rate": plan.gst_rate,
			"billing_interval": plan.billing_interval,
			"trial_ends_at": datetime.combine(start, time.min) if start else None,
			# What the first payment's period starts from (an offline payment already made has its own date).
			"due_date": None if offline else (start or getdate(nowdate())),
		}
	).insert(ignore_permissions=True)
	# No `press_site_id`, no apps, no steps: Core never provisions, suspends or deletes this site.
	tenant = frappe.get_doc(
		{
			"doctype": "GX Tenant",
			"account": account.name,
			"product": plan.product,
			"origin": "imported",
			"subscription": sub.name,
			"display_name": account.account_name,
			"site_url": f"https://{host}",
			"status": "ready",
			"ready_at": now_datetime(),
		}
	).insert(ignore_permissions=True)
	sub.db_set("tenant", tenant.name, update_modified=False)

	if offline:
		admin._record_manual_payment(
			account, plan, offline["reference"], offline["amount"], offline["paid_on"]
		)

	billing_note = (
		f"already paid offline ({fmt_money(offline['amount'] / 100, currency='INR')},"
		f" reference {offline['reference']})"
		if offline
		else f"billing starts {start.isoformat()}"
		if start
		else "first invoice due now"
	)
	audit(
		"tenant.add_direct_site",
		"GX Tenant",
		tenant.name,
		f"{host} for {account.account_name} ({'new customer' if created_account else 'existing customer'}),"
		f" {plan.plan_name} {payments.plan_price(plan)}, {billing_note}; Press check: {press_check}",
	)

	invite = created_account or not accounts.has_password(user)
	email_sent = direct.send_invite(account, host, plan, user) if invite else None
	if invite and not email_sent:
		warnings.append("invite_not_sent")
	return 201, _result(
		tenant,
		account.name,
		created=True,
		account_created=created_account,
		invite_sent=email_sent,
		press_check=press_check,
		warnings=warnings,
	)


def _result(tenant, account_name: str, *, created: bool, **extra) -> dict:
	from growthx_core.api.v1 import admin

	return {
		"customer": admin._customer_detail(account_name),
		"tenant": s.admin_tenant_detail(frappe.get_doc("GX Tenant", tenant.name)),
		"created": created,
		"accountCreated": extra.get("account_created", False),
		"inviteSent": extra.get("invite_sent"),
		"pressCheck": extra.get("press_check", "not_checked"),
		"warnings": extra.get("warnings", []),
	}


@endpoint(methods=("POST",), roles=("admin",), rate_limit=RateLimit(5, 3600, by_field="id"))
def send_payment_link():
	"""Email the customer a link to their billing page (and a set-password link if they have not set a
	password yet) for the site's subscription. Not for a terminated site, a canceled subscription or a
	lifetime plan that is already paid."""
	tenant_id = get_str(body(), "id", required=True)
	if not frappe.db.exists("GX Tenant", tenant_id):
		raise not_found()
	tenant = frappe.get_doc("GX Tenant", tenant_id)
	if tenant.status == "archived":
		raise ApiError(409, "tenant_archived", "A terminated site has nothing to pay.")
	if not tenant.subscription:
		raise ApiError(409, "no_subscription", "This site has no subscription.")
	sub = frappe.db.get_value(
		"GX Subscription", tenant.subscription, ["plan", "status", "billing_interval"], as_dict=True
	)
	if sub.status == "canceled":
		raise ApiError(409, "no_subscription", "This site's subscription is canceled.")
	if sub.status == "active" and sub.billing_interval == "one_time":
		raise ApiError(409, "nothing_due", "This site is paid for life.")
	account = frappe.get_doc("GX Account", tenant.account)
	plan = frappe.get_doc("GX Plan", sub.plan)
	site = (tenant.site_url or tenant.display_name or tenant.name).removeprefix("https://")
	if not direct.send_payment_link(
		account, site, plan, account.user, with_password=not accounts.has_password(account.user)
	):
		raise ApiError(502, "email_failed", "The email could not be sent. Check the outgoing email account.")
	audit(
		"tenant.send_payment_link", "GX Tenant", tenant.name, f"to {account.contact_email}, {plan.plan_name}"
	)
	return {"sent": True, "to": account.contact_email}
