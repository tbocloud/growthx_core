from datetime import timedelta

import frappe
from frappe.utils import now_datetime

from growthx_core.api.v1 import _serializers as s
from growthx_core.api.v1._core import (
	ApiError,
	audit,
	body,
	current_user,
	endpoint,
	get_choice,
	get_str,
	iso,
	not_found,
	page_meta,
	page_params,
	query,
)
from growthx_core.provisioning import site_login

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
	token = site_login.issue(tenant.name, current_user())
	audit("open_site", "GX Tenant", tenant.name)  # never log the token
	return {
		"url": site_login.login_url(tenant.site_url, token),
		"expiresAt": iso(now_datetime() + timedelta(seconds=site_login.TTL_SECONDS)),
	}


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
