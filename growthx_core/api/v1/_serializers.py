"""Turn GrowthX/ERPNext documents into the JSON shapes defined in openapi.yaml."""

import frappe
from frappe.utils import cint

from growthx_core.api.v1._core import get_role, iso, money, price

STEP_LABELS = {
	"create_site": "Creating your site",
	"wait_for_site": "Setting up the server",
	"install_apps": "Installing apps",
	"configure": "Configuring your business",
	"health_check": "Final checks",
}

INVOICE_STATUS = {
	"Draft": "draft",
	"Unpaid": "open",
	"Partly Paid": "open",
	"Unpaid and Discounted": "open",
	"Partly Paid and Discounted": "open",
	"Submitted": "open",
	"Overdue": "overdue",
	"Overdue and Discounted": "overdue",
	"Paid": "paid",
	"Cancelled": "void",
	"Credit Note Issued": "void",
	"Return": "void",
}


def user(name: str) -> dict:
	from growthx_core import accounts  # accounts → runner → this module

	u = frappe.db.get_value("User", name, ["name", "full_name", "email"], as_dict=True)
	role = get_role(name)
	company = None
	partner_status = None
	if role == "customer":
		company = frappe.db.get_value("GX Account", {"user": name}, "account_name")
	elif role == "partner":
		company, partner_status = frappe.db.get_value(
			"GX Partner Profile", {"user": name}, ["company", "status"]
		) or (None, None)
	return {
		"id": u.name,
		"name": u.full_name or u.email,
		"email": u.email,
		"role": role,
		"company": company,
		"partnerStatus": partner_status,
		"hasPassword": accounts.has_password(name),
		"socialLogins": accounts.social_providers(name),
	}


def auth_session(pair: dict, user_name: str) -> dict:
	return {
		"accessToken": pair["accessToken"],
		"expiresAt": iso(pair["expiresAt"]),
		"refreshToken": pair["refreshToken"],
		"refreshExpiresAt": iso(pair["refreshExpiresAt"]),
		"user": user(user_name),
	}


def product(p) -> dict:
	return {
		"id": p.name,
		"slug": p.slug,
		"name": p.product_name,
		"category": p.category or "",
		"summary": p.summary or "",
		"availability": p.availability,
	}


def plan(p, show_price=True) -> dict:
	return {
		"id": p.name,
		"productSlug": p.product,
		"name": p.plan_name,
		"description": p.description or "",
		"price": price(p.price, p.gst_included) if show_price else None,
		"interval": p.billing_interval,
		"trialDays": cint(p.trial_days),
		"features": [line.strip() for line in (p.features or "").splitlines() if line.strip()],
		"highlighted": bool(cint(p.highlighted)),
	}


def admin_plan(p) -> dict:
	"""A plan as the admin portal's catalogue shows it, including plans not on sale."""
	return {
		**plan(p),
		"gstRate": p.gst_rate,
		"active": bool(cint(p.is_active)),
		"item": p.item or None,
	}


def site(t) -> dict:
	return {
		"id": t.name,
		"productSlug": t.product,
		"origin": t.origin or "provisioned",
		"displayName": t.display_name or t.name,
		"status": t.status,
		"statusMessage": t.status_message or None,
		"subscriptionId": t.subscription or None,
		"createdAt": iso(t.creation),
		"readyAt": iso(t.ready_at),
	}


def _steps(tenant_doc, admin=False) -> list:
	rows = []
	for s in tenant_doc.get("steps") or []:
		row = {
			"key": s.step_key,
			"label": s.label or STEP_LABELS.get(s.step_key, s.step_key),
			"state": s.state,
			"startedAt": iso(s.started_at),
			"finishedAt": iso(s.finished_at),
		}
		if admin:
			row["attempt"] = cint(s.attempt)
			row["error"] = s.error or None
		rows.append(row)
	return rows


def site_detail(tenant_doc) -> dict:
	return {**site(tenant_doc), "steps": _steps(tenant_doc)}


def admin_tenant(t, customer_name: str | None = None) -> dict:
	"""`origin` tells the two kinds apart: `provisioned` is a site Core built for a GrowthX app,
	`imported` an ERPNext site the customer already had (billed on the direct product)."""
	plan_id, sub_status = (
		frappe.db.get_value("GX Subscription", t.subscription, ["plan", "status"])
		if t.subscription
		else (None, None)
	)
	return {
		"id": t.name,
		"customerId": t.account,
		"customerName": customer_name or frappe.db.get_value("GX Account", t.account, "account_name"),
		"productSlug": t.product,
		"origin": t.origin or "provisioned",
		"plan": plan_id,
		"planName": frappe.db.get_value("GX Plan", plan_id, "plan_name") if plan_id else None,
		"subscriptionStatus": sub_status,
		"siteUrl": t.site_url or None,
		"status": t.status,
		"lastError": t.last_error or None,
		"attempts": cint(t.attempts),
		"createdAt": iso(t.creation),
		"updatedAt": iso(t.modified),
	}


def admin_tenant_detail(tenant_doc) -> dict:
	return {**admin_tenant(tenant_doc), "steps": _steps(tenant_doc, admin=True)}


def subscription(s, product_name: str | None = None, plan_name: str | None = None) -> dict:
	from growthx_core.billing import renewals  # (renewals imports the runner, which imports this module)

	return {
		"id": s.name,
		"productSlug": s.product or None,
		"planId": s.plan,
		"productName": product_name or frappe.db.get_value("GX Product", s.product, "product_name"),
		"planName": plan_name or frappe.db.get_value("GX Plan", s.plan, "plan_name"),
		"status": s.status,
		"price": price(s.price, s.gst_included),
		"interval": s.billing_interval,
		"trialEndsAt": iso(s.trial_ends_at),
		"currentPeriodEnd": iso(s.current_period_end),
		"cancelAtPeriodEnd": bool(cint(s.cancel_at_period_end)),
		"siteId": s.tenant or None,
		# no countdown while renewals are switched off: nothing is enforced, so nothing is promised
		"renewal": renewals.renewal_info(s)
		if cint(frappe.db.get_single_value("GX Billing Settings", "enabled"))
		else None,
	}


def invoice(i) -> dict:
	return {
		"id": i.name,
		"number": i.name,
		"status": INVOICE_STATUS.get(i.status, "open"),
		"subtotal": money(i.net_total),
		"tax": money(i.total_taxes_and_charges),
		"total": money(i.grand_total),
		"issuedAt": iso(i.posting_date),
		"dueAt": iso(i.due_date),
	}


APPOINTMENT_STATUS = {
	"Confirmed": "confirmed",
	"Completed": "completed",
	"Cancelled": "cancelled",
	"No Show": "no_show",
}


def admin_appointment(row) -> dict:
	return {
		"id": row.name,
		"status": APPOINTMENT_STATUS.get(row.status, "confirmed"),
		"start": iso(row.start),
		"end": iso(row.end),
		"name": row.full_name,
		"email": row.email,
		"phone": row.phone,
		"company": row.company or None,
		"app": row.app or None,
		"notes": row.notes or None,
		"expert": row.expert or None,
		"meetingUrl": row.meeting_url or None,
		"leadId": row.lead or None,
		"createdAt": iso(row.creation),
	}


def admin_appointment_detail(doc, history: list) -> dict:
	return {
		**admin_appointment(doc),
		"adminNotes": doc.admin_notes or None,
		"cancelledBy": (doc.cancelled_by or "").lower() or None,
		"cancelReason": doc.cancel_reason or None,
		"history": history,
	}


# ---------------------------------------------------------------- support tickets

TICKET_FIELDS = [
	"name",
	"subject",
	"category",
	"priority",
	"status",
	"account",
	"customer_user",
	"tenant",
	"assigned_to",
	"creation",
	"modified",
	"last_activity_at",
	"first_response_at",
	"resolved_at",
	"closed_at",
]


def ticket(row) -> dict:
	"""What the customer sees of a ticket (no assignee, no staff details)."""
	return {
		"id": row.name,
		"subject": row.subject,
		"category": row.category,
		"priority": row.priority,
		"status": row.status,
		"tenantId": row.tenant or None,
		"createdAt": iso(row.creation),
		"updatedAt": iso(row.last_activity_at or row.modified),
		"resolvedAt": iso(row.resolved_at),
		"closedAt": iso(row.closed_at),
	}


def ticket_message(row, admin=False) -> dict:
	out = {
		"id": row.name,
		"authorName": row.author_name or ("GrowthX support" if row.is_staff else "Customer"),
		"fromStaff": bool(cint(row.is_staff)),
		"body": row.body,
		"createdAt": iso(row.creation),
	}
	if admin:
		out["internal"] = bool(cint(row.is_internal))
		out["authorId"] = row.author
	return out


def admin_ticket(row, names: dict) -> dict:
	"""`names` maps user id -> full name and account id -> company, for the rows being shown."""
	assignee = row.assigned_to
	return {
		**ticket(row),
		"accountId": row.account,
		"company": names.get(("account", row.account)),
		"customerName": names.get(("user", row.customer_user)),
		"customerEmail": row.customer_user,
		"assignee": {"id": assignee, "name": names.get(("user", assignee)) or assignee} if assignee else None,
		"firstResponseAt": iso(row.first_response_at),
	}
