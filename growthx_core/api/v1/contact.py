import frappe
from frappe.utils import escape_html, get_url_to_form

from growthx_core.api.v1._core import (
	RateLimit,
	body,
	client_ip,
	endpoint,
	get_choice,
	get_email,
	get_str,
	ok,
)

# Where new contact requests are emailed. site_config `growthx_support_email` overrides it.
SUPPORT_EMAIL = "support@tbogrowthx.com"
SOURCES = ("contact", "support")


@endpoint(methods=("POST",), auth=False, rate_limit=RateLimit(5, 60 * 60))
def submit():
	"""The website's Contact form (source "contact") and the customer Support form (source "support",
	sent with the customer's token). Saved as a GX Contact Request and emailed to support. The customer
	comes only from a signed-in caller's token, never from the body."""
	data = body()
	source = get_choice(data, "source", SOURCES, default="contact")
	doc = frappe.get_doc(
		{
			"doctype": "GX Contact Request",
			"full_name": get_str(data, "name", required=True, label="Name"),
			"email": get_email(data),
			"company": get_str(data, "company", label="Company"),
			"source": source,
			"customer": _signed_in_customer(),
			"message": get_str(data, "message", required=True, max_length=5000, label="Message"),
			"ip_address": client_ip(),
		}
	).insert(ignore_permissions=True)
	_email_support(doc)
	return ok(202)


def _signed_in_customer():
	"""The caller's GX Account when they send a GrowthX bearer token, else None."""
	if not getattr(frappe.local, "gx_token", None):
		return None
	return frappe.db.get_value("GX Account", {"user": frappe.session.user}) or None


def _one_line(value):
	"""Collapse line breaks and runs of whitespace: submitted text must not span lines in a header."""
	return " ".join((value or "").split())


def _email_support(doc):
	try:
		esc = escape_html
		text = esc(doc.message).replace("\n", "<br>")
		frappe.sendmail(
			recipients=[frappe.conf.get("growthx_support_email") or SUPPORT_EMAIL],
			reply_to=doc.email,
			subject=f"{'Support' if doc.source == 'support' else 'Contact'} request from {_one_line(doc.full_name)}"
			+ (f" ({_one_line(doc.company)})" if _one_line(doc.company) else ""),
			message=(
				f"<p><b>Name:</b> {esc(doc.full_name)}<br>"
				f"<b>Email:</b> {esc(doc.email)}<br>"
				f"<b>Company:</b> {esc(doc.company or '-')}"
				+ (f"<br><b>Customer:</b> {esc(doc.customer)}" if doc.customer else "")
				+ "</p>"
				f"<p>{text}</p>"
				"<p>Reply to this email to answer the sender.</p>"
				f'<p><a href="{get_url_to_form(doc.doctype, doc.name)}">Open {esc(doc.name)} in Core</a> '
				"(needs a Core login).</p>"
			),
			reference_doctype=doc.doctype,
			reference_name=doc.name,
		)
	except Exception:
		# The request is saved either way; admins see the failure in the Error Log.
		frappe.log_error(title=f"GrowthX: could not email contact request {doc.name}")
