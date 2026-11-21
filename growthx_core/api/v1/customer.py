from datetime import timedelta

import frappe
from frappe.utils import now_datetime

from growthx_core import accounts, tokens
from growthx_core.api.v1 import _serializers as s
from growthx_core.api.v1 import auth
from growthx_core.api.v1._core import (
	ApiError,
	RateLimit,
	audit,
	body,
	current_user,
	endpoint,
	file_response,
	get_choice,
	get_str,
	iso,
	not_found,
	page_meta,
	page_params,
	query,
	validation_error,
)
from growthx_core.billing import billing_details as billing_details_store
from growthx_core.billing import payu, tax_invoice
from growthx_core.billing.doctype.gx_subscription.gx_subscription import sync_account_status
from growthx_core.provisioning import apps, custom_domains, runner, site_login

SITE_FIELDS = [
	"name",
	"product",
	"display_name",
	"status",
	"status_message",
	"subscription",
	"creation",
	"ready_at",
]


def current_account(fields="name"):
	account = frappe.db.get_value(
		"GX Account", {"user": current_user()}, fields, as_dict=isinstance(fields, list)
	)
	if not account:
		raise ApiError(403, "forbidden", "No customer account is linked to this user.")
	return account


def _own_tenant(tenant_id, for_update=False):
	account = current_account()
	if not tenant_id or not frappe.db.exists("GX Tenant", {"name": tenant_id, "account": account}):
		raise not_found()  # 404, not 403, for other people's sites
	return frappe.get_doc("GX Tenant", tenant_id, for_update=for_update)


@endpoint(methods=("GET",), roles=("customer",))
def sites():
	page, page_size = page_params()
	filters = {"account": current_account()}
	rows = frappe.get_all(
		"GX Tenant",
		filters=filters,
		fields=SITE_FIELDS,
		order_by="creation desc",
		limit_start=(page - 1) * page_size,
		limit_page_length=page_size,
	)
	return page_meta(page, page_size, frappe.db.count("GX Tenant", filters), [s.site(r) for r in rows])


@endpoint(methods=("GET",), roles=("customer",))
def site():
	return s.site_detail(_own_tenant(get_str(query(), "id", required=True)))


@endpoint(methods=("POST",), roles=("customer",))
def open_site():
	tenant = _own_tenant(get_str(body(), "id", required=True))
	if tenant.status != "ready" or not tenant.site_url:
		raise ApiError(409, "site_not_ready", "Your site isn't ready yet.")
	audit("open_site", "GX Tenant", tenant.name)  # never log a token
	if not apps.theme_required(tenant.product):
		# A site without growthx_theme has no single sign-on, so its own login page is the way in
		# (the customer's user is set up there, with "Login with Email Link").
		return {
			"url": f"{custom_domains.public_url(tenant).rstrip('/')}/login",
			"expiresAt": iso(now_datetime() + timedelta(seconds=site_login.TTL_SECONDS)),
		}
	token = site_login.issue(tenant.name, current_user())
	return {
		"url": site_login.login_url(custom_domains.public_url(tenant), token),
		"expiresAt": iso(now_datetime() + timedelta(seconds=site_login.TTL_SECONDS)),
	}


@endpoint(methods=("POST",), roles=("customer",), idempotent=True, rate_limit=RateLimit(10, 60, by_user=True))
def add_site():
	"""Add another GrowthX app to this account: a new site on a trial of that app's plan."""
	data = body()
	account = current_account()
	# Same choices and checks as a sign-up (auth._signup_plan/_signup_site): the app must be on sale,
	# and the site gets the country's chart of accounts and the customer's subdomain.
	plan = auth._signup_plan(data)
	if not plan:
		raise validation_error({"productSlug": "Choose an app to add."})
	if frappe.db.exists(
		"GX Tenant", {"account": account, "product": plan.product, "status": ("!=", "archived")}
	):
		message = "You already have this app."
		raise ApiError(409, "already_added", message, fields={"productSlug": message})
	site = auth._signup_site(data)
	# Last, because it calls Press (10 name checks a minute for the whole team).
	if site["subdomain"] and runner.press_name_taken(site["subdomain"]):
		raise auth._subdomain_taken()
	tenant = accounts.start_site(account=account, plan=plan, **site)
	audit("add_site", "GX Tenant", tenant)
	return 201, {"site": s.site(frappe.get_doc("GX Tenant", tenant))}


@endpoint(
	methods=("POST",), roles=("customer",), idempotent=True, rate_limit=RateLimit(5, 3600, by_user=True)
)
def delete_account():
	"""Close the customer's account: cancel its subscriptions, archive its sites (Press backs each one
	up first) and block signing in. Tax invoices are kept: GST needs them, and they carry the name."""
	account = current_account(["name", "contact_email", "user"])
	confirm = get_str(body(), "confirm", required=True, max_length=140)
	if confirm.strip().lower() != (account.contact_email or "").lower():
		message = "Type your email address to confirm."
		raise validation_error({"confirm": message})

	for name in frappe.get_all(
		"GX Subscription",
		filters={"account": account.name, "status": ("in", ("trialing", "active", "past_due", "suspended"))},
		pluck="name",
	):
		frappe.db.set_value("GX Subscription", name, "status", "canceled")
	sync_account_status(account.name)
	for name in frappe.get_all(
		"GX Tenant", filters={"account": account.name, "status": ("!=", "archived")}, pluck="name"
	):
		runner.archive(name)

	tokens.revoke_all(account.user)
	frappe.db.set_value("User", account.user, "enabled", 0)
	audit("delete_account", "GX Account", account.name)
	return {"ok": True}


@endpoint(methods=("GET",), roles=("customer",))
def custom_domain():
	tenant = _own_tenant(get_str(query(), "id", required=True))
	return {"customDomain": custom_domains.serialize(tenant)}


@endpoint(methods=("POST",), roles=("customer",), idempotent=True)
def add_custom_domain():
	data = body()
	tenant = _own_tenant(get_str(data, "id", required=True), for_update=True)
	custom_domains.add(tenant, get_str(data, "domain", required=True, max_length=253))
	audit("add_custom_domain", "GX Tenant", tenant.name, tenant.custom_domain)
	return 201, custom_domains.serialize(tenant)


@endpoint(methods=("POST",), roles=("customer",), rate_limit=RateLimit(6, 60, by_field="id"))
def check_custom_domain():
	tenant = _own_tenant(get_str(body(), "id", required=True), for_update=True)
	if not tenant.custom_domain:
		raise not_found()
	custom_domains.check(tenant)
	return custom_domains.serialize(tenant)


@endpoint(methods=("POST",), roles=("customer",), idempotent=True)
def remove_custom_domain():
	tenant = _own_tenant(get_str(body(), "id", required=True), for_update=True)
	domain = tenant.custom_domain
	if not custom_domains.remove(tenant):
		raise not_found()
	audit("remove_custom_domain", "GX Tenant", tenant.name, domain)
	return {"ok": True}


@endpoint(methods=("GET",), roles=("customer",))
def subscriptions():
	page, page_size = page_params()
	filters = {"account": current_account()}
	rows = frappe.get_all(
		"GX Subscription",
		filters=filters,
		fields=["*"],
		order_by="creation desc",
		limit_start=(page - 1) * page_size,
		limit_page_length=page_size,
	)
	products = dict(frappe.get_all("GX Product", fields=["name", "product_name"], as_list=True))
	plans = dict(frappe.get_all("GX Plan", fields=["name", "plan_name"], as_list=True))
	items = [s.subscription(r, products.get(r.product), plans.get(r.plan)) for r in rows]
	return page_meta(page, page_size, frappe.db.count("GX Subscription", filters), items)


INVOICE_FILTER = {
	"draft": ["Draft"],
	"open": ["Unpaid", "Partly Paid", "Unpaid and Discounted", "Partly Paid and Discounted", "Submitted"],
	"overdue": ["Overdue", "Overdue and Discounted"],
	"paid": ["Paid"],
	"void": ["Cancelled", "Credit Note Issued"],
}


@endpoint(methods=("GET",), roles=("customer",))
def invoices():
	data = query()
	page, page_size = page_params(data)
	status = get_choice(data, "status", tuple(INVOICE_FILTER))
	customer = current_account(["customer"]).customer
	if not customer:
		return page_meta(page, page_size, 0, [])

	filters = {"customer": customer, "docstatus": ("in", [1, 2]), "is_return": 0}
	if status:
		filters["status"] = ("in", INVOICE_FILTER[status])
	rows = frappe.get_all(
		"Sales Invoice",
		filters=filters,
		fields=[
			"name",
			"status",
			"net_total",
			"total_taxes_and_charges",
			"grand_total",
			"posting_date",
			"due_date",
		],
		order_by="posting_date desc, creation desc",
		limit_start=(page - 1) * page_size,
		limit_page_length=page_size,
	)
	return page_meta(page, page_size, frappe.db.count("Sales Invoice", filters), [s.invoice(r) for r in rows])


# Only issued invoices have a PDF: not drafts, and not cancelled ones.
PDF_STATUSES = INVOICE_FILTER["open"] + INVOICE_FILTER["overdue"] + INVOICE_FILTER["paid"]


@endpoint(methods=("GET",), roles=("customer",), rate_limit=RateLimit(60, 3600, by_user=True))
def invoice_pdf():
	invoice = get_str(query(), "id", required=True)
	customer = current_account(["customer"]).customer
	name = customer and frappe.db.get_value(
		"Sales Invoice",
		{
			"name": invoice,
			"customer": customer,
			"docstatus": 1,
			"is_return": 0,
			"status": ("in", PDF_STATUSES),
		},
		"name",
	)
	if not name:
		raise not_found()
	return file_response(tax_invoice.pdf(name), tax_invoice.filename(name), "application/pdf")


# ---------------------------------------------------------------- billing details (GST invoices)

BILLING_ACCOUNT_FIELDS = ["name", "account_name", "contact_email", "phone", "customer"]


@endpoint(methods=("GET",), roles=("customer",))
def billing_details():
	return billing_details_store.get(current_account(BILLING_ACCOUNT_FIELDS))


@endpoint(methods=("POST",), roles=("customer",), rate_limit=RateLimit(30, 3600, by_user=True))
def update_billing_details():
	data = body()
	account = current_account(BILLING_ACCOUNT_FIELDS)
	if not billing_details_store.available():
		raise billing_details_store.unavailable()
	before = billing_details_store.get(account)
	payu.ensure_customer(account)
	after = billing_details_store.save(account, data)
	if after != before:
		audit(
			"customer.billing_details_updated",
			"GX Account",
			account.name,
			_billing_summary(after) + (f" (was: {_billing_summary(before)})" if before["complete"] else ""),
		)
	return after


def _billing_summary(details: dict) -> str:
	a = details["address"]
	name = details["billingName"] or "company name"
	return f"{name}, {a['city']}, {a['state']} {a['pincode']}, GSTIN {details['gstin'] or 'none'}"
