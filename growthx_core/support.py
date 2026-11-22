"""Support tickets: the rules shared by the customer endpoints (api/v1/support.py) and the admin
ones (api/v1/admin_support.py). Messages are plain text; they are escaped wherever they are put in
an email and the web portal renders them as text.

Status flow: open -> pending_customer (staff replied) -> open (customer replied) -> resolved ->
closed. A customer reply reopens a resolved ticket; a closed ticket takes no more messages.
"""

import frappe
from frappe.utils import escape_html, now_datetime

from growthx_core.api.v1._core import ApiError, web_url

SUPPORT_EMAIL = "support@tbogrowthx.com"
CATEGORIES = ("billing", "technical", "site", "other")
PRIORITIES = ("low", "normal", "high", "urgent")
CUSTOMER_PRIORITIES = ("low", "normal", "high")  # urgent is the team's call
STATUSES = ("open", "pending_customer", "resolved", "closed")
SUBJECT_MAX = 140
BODY_MAX = 5000

# status -> statuses the team may move it to
TRANSITIONS = {
	"open": ("pending_customer", "resolved", "closed"),
	"pending_customer": ("open", "resolved", "closed"),
	"resolved": ("open", "pending_customer", "closed"),
	"closed": ("open",),
}


def support_inbox() -> str:
	return frappe.conf.get("growthx_support_email") or SUPPORT_EMAIL


def one_line(value) -> str:
	"""Collapse line breaks and runs of whitespace: text in an email header must not span lines."""
	return " ".join((value or "").split())


def locked(ticket_id: str):
	"""The ticket, locked for this request's changes."""
	return frappe.get_doc("GX Support Ticket", ticket_id, for_update=True)


def _add_message(ticket, body: str, author: str, *, staff: bool, internal: bool = False):
	now = now_datetime()
	message = frappe.get_doc(
		{
			"doctype": "GX Ticket Message",
			"ticket": ticket.name,
			"author": author,
			"author_name": frappe.db.get_value("User", author, "full_name") or author,
			"is_staff": 1 if staff else 0,
			"is_internal": 1 if internal else 0,
			"body": body,
		}
	).insert(ignore_permissions=True)
	ticket.last_activity_at = now
	return message, now


def create_ticket(*, account, user, subject, body, category, priority, tenant=None, ip_address=None):
	ticket = frappe.get_doc(
		{
			"doctype": "GX Support Ticket",
			"subject": subject,
			"category": category,
			"priority": priority,
			"status": "open",
			"account": account,
			"customer_user": user,
			"tenant": tenant,
			"ip_address": ip_address,
		}
	)
	ticket.insert(ignore_permissions=True)
	message, _ = _add_message(ticket, body, user, staff=False)
	ticket.save(ignore_permissions=True)
	email_new_ticket(ticket, message)
	return ticket


def customer_reply(ticket, body: str, user: str):
	"""A customer's message. Reopens a resolved ticket and moves a pending one back to open."""
	if ticket.status == "closed":
		raise ApiError(409, "ticket_closed", "This ticket is closed. Please open a new one.")
	message, _ = _add_message(ticket, body, user, staff=False)
	ticket.status = "open"
	ticket.resolved_at = None
	ticket.save(ignore_permissions=True)
	email_customer_reply(ticket, message)
	return message


def staff_reply(ticket, body: str, user: str):
	"""A reply the customer sees. Sets the ticket to pending_customer and records the first response."""
	if ticket.status == "closed":
		raise ApiError(409, "ticket_closed", "This ticket is closed. Reopen it first.")
	message, now = _add_message(ticket, body, user, staff=True)
	if not ticket.first_response_at:
		ticket.first_response_at = now
	if not ticket.assigned_to:
		ticket.assigned_to = user
	ticket.status = "pending_customer"
	ticket.resolved_at = None
	ticket.save(ignore_permissions=True)
	email_staff_reply(ticket, message)
	return message


def internal_note(ticket, body: str, user: str):
	"""A note only the team sees. Changes nothing but the activity time; never emailed."""
	message, _ = _add_message(ticket, body, user, staff=True, internal=True)
	ticket.save(ignore_permissions=True)
	return message


def customer_close(ticket):
	if ticket.status == "closed":
		raise ApiError(409, "invalid_transition", "This ticket is already closed.")
	_set_status(ticket, "closed")
	ticket.save(ignore_permissions=True)


def set_status(ticket, status: str):
	"""Move to `status` if the flow allows it (the same status is a no-op)."""
	if status == ticket.status:
		return
	if status not in TRANSITIONS[ticket.status]:
		raise ApiError(409, "invalid_transition", f"A {ticket.status} ticket can't be moved to {status}.")
	_set_status(ticket, status)


def _set_status(ticket, status: str):
	now = now_datetime()
	ticket.status = status
	if status == "resolved":
		ticket.resolved_at = now
	elif status == "closed":
		ticket.closed_at = now
		ticket.resolved_at = ticket.resolved_at or now
	else:
		ticket.resolved_at = None
		ticket.closed_at = None
	ticket.last_activity_at = now


def staff_users() -> list[dict]:
	"""Enabled users who hold the GrowthX Admin role: who a ticket can be assigned to."""
	names = frappe.get_all(
		"Has Role", filters={"role": "GrowthX Admin", "parenttype": "User"}, pluck="parent"
	)
	if not names:
		return []
	rows = frappe.get_all(
		"User",
		filters={"name": ("in", names), "enabled": 1},
		fields=["name", "full_name"],
		order_by="full_name asc",
	)
	return [{"id": r.name, "name": r.full_name or r.name} for r in rows]


def is_staff_user(user: str) -> bool:
	return any(u["id"] == user for u in staff_users())


# ---------------------------------------------------------------- emails
# Each is best effort: the ticket and message are saved either way, and a failure goes to the
# Error Log. Text from people is escaped; headers (subject) are collapsed to one line. Nothing
# secret is ever sent: only the ticket id, subject, message text and a link to the portal.


def _subject(ticket, prefix="") -> str:
	return f"{prefix}[{ticket.name}] {one_line(ticket.subject)}"[:200]


def _text(value) -> str:
	return escape_html(value or "").replace("\r\n", "\n").replace("\n", "<br>")


def _send(ticket, **kwargs):
	try:
		frappe.sendmail(reference_doctype="GX Support Ticket", reference_name=ticket.name, **kwargs)
	except Exception:
		frappe.log_error(title=f"GrowthX: could not email support ticket {ticket.name}")


def email_new_ticket(ticket, message):
	account_name = frappe.db.get_value("GX Account", ticket.account, "account_name")
	email = frappe.db.get_value("User", ticket.customer_user, "email")
	_send(
		ticket,
		recipients=[support_inbox()],
		reply_to=email,
		subject=_subject(ticket, "New ticket "),
		message=(
			f"<p><b>Customer:</b> {escape_html(account_name or ticket.account)} "
			f"({escape_html(email or '-')})<br>"
			f"<b>Category:</b> {escape_html(ticket.category)} &middot; "
			f"<b>Priority:</b> {escape_html(ticket.priority)}</p>"
			f"<p>{_text(message.body)}</p>"
			f'<p><a href="{web_url("/admin/support/" + ticket.name)}">Open {escape_html(ticket.name)} '
			"in the admin portal</a> to reply.</p>"
		),
	)


def email_staff_reply(ticket, message):
	email = frappe.db.get_value("User", ticket.customer_user, "email")
	if not email:
		return
	_send(
		ticket,
		recipients=[email],
		reply_to=support_inbox(),
		subject=_subject(ticket, "Re: "),
		message=(
			f"<p>{escape_html(message.author_name)} from GrowthX support replied to your ticket "
			f"{escape_html(ticket.name)}:</p>"
			f"<p>{_text(message.body)}</p>"
			f'<p><a href="{web_url("/customer/support/" + ticket.name)}">View the conversation '
			"and reply</a> (sign in required).</p>"
		),
	)


def email_customer_reply(ticket, message):
	assignee_email = ticket.assigned_to and frappe.db.get_value("User", ticket.assigned_to, "email")
	_send(
		ticket,
		recipients=[assignee_email or support_inbox()],
		reply_to=frappe.db.get_value("User", ticket.customer_user, "email"),
		subject=_subject(ticket, "Customer reply "),
		message=(
			f"<p>{escape_html(message.author_name)} replied to {escape_html(ticket.name)}:</p>"
			f"<p>{_text(message.body)}</p>"
			f'<p><a href="{web_url("/admin/support/" + ticket.name)}">Open the ticket</a> '
			"in the admin portal to reply.</p>"
		),
	)
