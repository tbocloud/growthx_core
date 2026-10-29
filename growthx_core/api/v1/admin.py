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
	validation_error,
)
from growthx_core.api.v1 import appointments as appts
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


# ---------------------------------------------------------------- appointments ("Talk to an expert")

APPOINTMENT_STATUSES = {"confirmed": "Confirmed", "completed": "Completed", "cancelled": "Cancelled", "no_show": "No Show"}
APPOINTMENT_FIELDS = [
	"name", "status", "start", "end", "full_name", "email", "phone", "company", "app", "notes",
	"expert", "meeting_url", "lead", "creation",
]


def _day(data: dict, key: str):
	value = get_str(data, key, max_length=10)
	if not value:
		return None
	try:
		return frappe.utils.getdate(value)
	except Exception:
		raise validation_error({key: "Use a date like 2026-10-14."})


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
		filters={"status": "Confirmed", "start": (">=", frappe.utils.now_datetime()), "expert": ("in", experts or [""])},
		pluck="expert",
	)
	names = dict(frappe.get_all("User", {"name": ("in", experts or [""])}, ["name", "full_name"], as_list=True))
	return {"items": [{"email": e, "name": names.get(e) or e, "upcoming": upcoming.count(e)} for e in experts]}
