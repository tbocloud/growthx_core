"""Support tickets for the team (role admin; re-exported from admin.py, so the paths are
`growthx_core.api.v1.admin.support_*`): inbox with filters, a ticket with its internal notes,
reply (customer sees it), internal note, and status / priority / assignee changes. Every change is
audit-logged. Rules live in growthx_core/support.py."""

import frappe
from frappe.query_builder.functions import Count

from growthx_core import support
from growthx_core.api.v1 import _serializers as s
from growthx_core.api.v1._core import (
	audit,
	body,
	current_user,
	endpoint,
	get_choice,
	get_sort,
	get_str,
	not_found,
	page_meta,
	page_params,
	query,
	validation_error,
)

_UNSET = object()


def _ticket(ticket_id, for_update=False):
	if not ticket_id or not frappe.db.exists("GX Support Ticket", ticket_id):
		raise not_found()
	return support.locked(ticket_id) if for_update else frappe.get_doc("GX Support Ticket", ticket_id)


def _names(rows) -> dict:
	users = {r.customer_user for r in rows} | {r.assigned_to for r in rows if r.assigned_to}
	accounts = {r.account for r in rows}
	names = {}
	if users:
		for u in frappe.get_all("User", {"name": ("in", list(users))}, ["name", "full_name"]):
			names[("user", u.name)] = u.full_name or u.name
	if accounts:
		for a in frappe.get_all("GX Account", {"name": ("in", list(accounts))}, ["name", "account_name"]):
			names[("account", a.name)] = a.account_name
	return names


def _detail(ticket) -> dict:
	messages = frappe.get_all(
		"GX Ticket Message",
		filters={"ticket": ticket.name},
		fields=["name", "author", "author_name", "is_staff", "is_internal", "body", "creation"],
		order_by="creation asc, name asc",
	)
	return {
		**s.admin_ticket(ticket, _names([ticket])),
		"messages": [s.ticket_message(m, admin=True) for m in messages],
	}


def _text(data, key="message") -> str:
	return get_str(data, key, required=True, max_length=support.BODY_MAX, label="Message")


@endpoint(methods=("GET",), roles=("admin",))
def support_summary():
	"""Counts for the dashboard, and the staff a ticket can be assigned to."""
	ticket = frappe.qb.DocType("GX Support Ticket")
	counts = dict(
		frappe.qb.from_(ticket).select(ticket.status, Count(ticket.name)).groupby(ticket.status).run()
	)
	open_filters = {"status": ("in", ["open", "pending_customer"])}
	return {
		"open": counts.get("open", 0),
		"pendingCustomer": counts.get("pending_customer", 0),
		"resolved": counts.get("resolved", 0),
		"closed": counts.get("closed", 0),
		"unassigned": frappe.db.count(
			"GX Support Ticket", {**open_filters, "assigned_to": ("is", "not set")}
		),
		"urgent": frappe.db.count("GX Support Ticket", {**open_filters, "priority": "urgent"}),
		"staff": support.staff_users(),
	}


@endpoint(methods=("GET",), roles=("admin",))
def support_tickets():
	data = query()
	page, page_size = page_params(data)
	status = get_choice(data, "status", support.STATUSES)
	priority = get_choice(data, "priority", support.PRIORITIES)
	category = get_choice(data, "category", support.CATEGORIES)
	assignee = get_str(data, "assignee", max_length=140)  # a user id, "me" or "unassigned"
	q = get_str(data, "q", max_length=100)
	order_by = get_sort(
		data,
		{"activity": "last_activity_at", "createdAt": "creation", "priority": "priority"},
		"-activity",
	)

	filters = {}
	if status:
		filters["status"] = status
	if priority:
		filters["priority"] = priority
	if category:
		filters["category"] = category
	if assignee == "unassigned":
		filters["assigned_to"] = ("is", "not set")
	elif assignee:
		filters["assigned_to"] = current_user() if assignee == "me" else assignee
	or_filters = None
	if q:
		like = f"%{q}%"
		matching = frappe.get_all("GX Account", {"account_name": ("like", like)}, pluck="name")
		or_filters = [
			["subject", "like", like],
			["name", "like", like],
			["customer_user", "like", like],
		]
		if matching:
			or_filters.append(["account", "in", matching])
	rows = frappe.get_all(
		"GX Support Ticket",
		filters=filters,
		or_filters=or_filters,
		fields=s.TICKET_FIELDS,
		order_by=order_by,
		limit_start=(page - 1) * page_size,
		limit_page_length=page_size,
	)
	total = len(frappe.get_all("GX Support Ticket", filters=filters, or_filters=or_filters, pluck="name"))
	names = _names(rows)
	return page_meta(page, page_size, total, [s.admin_ticket(r, names) for r in rows])


@endpoint(methods=("GET",), roles=("admin",))
def support_ticket():
	return _detail(_ticket(get_str(query(), "id", required=True, max_length=40)))


@endpoint(methods=("POST",), roles=("admin",))
def support_reply():
	"""A reply the customer sees (and is emailed). Sets the ticket to pending_customer."""
	data = body()
	ticket = _ticket(get_str(data, "id", required=True, max_length=40), for_update=True)
	support.staff_reply(ticket, _text(data), current_user())
	audit("support.reply", "GX Support Ticket", ticket.name, "Reply to the customer")
	return _detail(ticket)


@endpoint(methods=("POST",), roles=("admin",))
def support_note():
	"""An internal note: never shown to the customer, never emailed."""
	data = body()
	ticket = _ticket(get_str(data, "id", required=True, max_length=40), for_update=True)
	support.internal_note(ticket, _text(data, "note"), current_user())
	audit("support.note", "GX Support Ticket", ticket.name, "Internal note")
	return _detail(ticket)


@endpoint(methods=("POST",), roles=("admin",))
def support_update():
	"""Change any of status, priority, assigneeId (null unassigns). Only the keys sent change."""
	data = body()
	ticket = _ticket(get_str(data, "id", required=True, max_length=40), for_update=True)
	status = get_choice(data, "status", support.STATUSES)
	priority = get_choice(data, "priority", support.PRIORITIES)
	assignee = _UNSET
	if "assigneeId" in data:
		assignee = get_str(data, "assigneeId", max_length=140, label="Assignee")
		if assignee and not support.is_staff_user(assignee):
			raise validation_error({"assigneeId": "Choose a GrowthX team member."})
	if status is None and priority is None and assignee is _UNSET:
		raise validation_error({"status": "Send status, priority or assigneeId."})

	changes = []
	if status and status != ticket.status:
		changes.append(f"status {ticket.status} -> {status}")
		support.set_status(ticket, status)
	if priority and priority != ticket.priority:
		changes.append(f"priority {ticket.priority} -> {priority}")
		ticket.priority = priority
	if assignee is not _UNSET and (assignee or None) != (ticket.assigned_to or None):
		changes.append(f"assignee {ticket.assigned_to or '-'} -> {assignee or '-'}")
		ticket.assigned_to = assignee
	if changes:
		ticket.save(ignore_permissions=True)
		audit("support.update", "GX Support Ticket", ticket.name, "; ".join(changes))
	return _detail(ticket)
