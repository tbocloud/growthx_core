import frappe
from frappe.query_builder.functions import Count

from growthx_core.api.v1 import _serializers as s
from growthx_core.api.v1._core import (
	ApiError,
	audit,
	body,
	endpoint,
	get_choice,
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
)
from growthx_core.provisioning import runner

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

	items = []
	for r in rows:
		sub = subs.get(r.name)
		items.append(
			{
				"id": r.name,
				"company": r.account_name,
				"contactEmail": r.contact_email,
				"status": r.status,
				"plan": plan_names.get(sub.plan) if sub else None,
				"partnerName": partner_names.get(r.referred_by) if r.referred_by else None,
				"mrr": money(_mrr(sub) if sub and sub.status == "active" else 0),
				"createdAt": iso(r.creation),
			}
		)
	return page_meta(page, page_size, total, items)


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
