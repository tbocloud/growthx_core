"""Customer support tickets: support.create, support.tickets, support.ticket, support.reply,
support.close. A customer sees only their own account's tickets (another account's id is a 404)
and never the team's internal notes. Rules live in growthx_core/support.py."""

import frappe

from growthx_core import support
from growthx_core.api.v1 import _serializers as s
from growthx_core.api.v1._core import (
	RateLimit,
	audit,
	body,
	client_ip,
	current_user,
	endpoint,
	get_choice,
	get_str,
	not_found,
	page_meta,
	page_params,
	query,
	validation_error,
)
from growthx_core.api.v1.customer import current_account

READ_LIMIT = RateLimit(120, 60, by_user=True)
WRITE_LIMIT = RateLimit(30, 60 * 60, by_user=True)


def _own_ticket(ticket_id, for_update=False):
	"""The caller's own account's ticket; 404 (not 403) for anyone else's."""
	account = current_account()
	if not ticket_id or not frappe.db.exists("GX Support Ticket", {"name": ticket_id, "account": account}):
		raise not_found()
	return support.locked(ticket_id) if for_update else frappe.get_doc("GX Support Ticket", ticket_id)


def _message_body(data) -> str:
	return get_str(data, "message", required=True, max_length=support.BODY_MAX, label="Message")


def _detail(ticket) -> dict:
	rows = frappe.get_all(
		"GX Ticket Message",
		filters={"ticket": ticket.name, "is_internal": 0},
		fields=["name", "author", "author_name", "is_staff", "body", "creation"],
		order_by="creation asc, name asc",
	)
	return {**s.ticket(ticket), "messages": [s.ticket_message(r) for r in rows]}


@endpoint(methods=("POST",), roles=("customer",), rate_limit=RateLimit(10, 60 * 60, by_user=True))
def create():
	account = current_account()
	data = body()
	subject = get_str(data, "subject", required=True, max_length=support.SUBJECT_MAX, label="Subject")
	text = _message_body(data)
	category = get_choice(data, "category", support.CATEGORIES, default="other")
	priority = get_choice(data, "priority", support.CUSTOMER_PRIORITIES, default="normal")
	tenant = get_str(data, "tenantId", max_length=40, label="Site")
	if tenant and not frappe.db.exists("GX Tenant", {"name": tenant, "account": account}):
		raise validation_error({"tenantId": "Choose one of your sites."})
	ticket = support.create_ticket(
		account=account,
		user=current_user(),
		subject=subject,
		body=text,
		category=category,
		priority=priority,
		tenant=tenant,
		ip_address=client_ip(),
	)
	audit("support.create", "GX Support Ticket", ticket.name, "By the customer")
	return 201, _detail(ticket)


@endpoint(methods=("GET",), roles=("customer",), rate_limit=READ_LIMIT)
def tickets():
	account = current_account()
	data = query()
	page, page_size = page_params(data)
	status = get_choice(data, "status", support.STATUSES)
	filters = {"account": account}
	if status:
		filters["status"] = status
	rows = frappe.get_all(
		"GX Support Ticket",
		filters=filters,
		fields=s.TICKET_FIELDS,
		order_by="last_activity_at desc, creation desc",
		limit_start=(page - 1) * page_size,
		limit_page_length=page_size,
	)
	total = frappe.db.count("GX Support Ticket", filters)
	return page_meta(page, page_size, total, [s.ticket(r) for r in rows])


@endpoint(methods=("GET",), roles=("customer",), rate_limit=READ_LIMIT)
def ticket():
	return _detail(_own_ticket(get_str(query(), "id", required=True, max_length=40)))


@endpoint(methods=("POST",), roles=("customer",), rate_limit=WRITE_LIMIT)
def reply():
	data = body()
	ticket = _own_ticket(get_str(data, "id", required=True, max_length=40), for_update=True)
	support.customer_reply(ticket, _message_body(data), current_user())
	audit("support.reply", "GX Support Ticket", ticket.name, "By the customer")
	return _detail(ticket)


@endpoint(methods=("POST",), roles=("customer",), rate_limit=WRITE_LIMIT)
def close():
	ticket = _own_ticket(get_str(body(), "id", required=True, max_length=40), for_update=True)
	support.customer_close(ticket)
	audit("support.close", "GX Support Ticket", ticket.name, "By the customer")
	return _detail(ticket)
