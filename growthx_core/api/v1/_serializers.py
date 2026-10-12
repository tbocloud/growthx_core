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


def plan(p) -> dict:
	return {
		"id": p.name,
		"productSlug": p.product,
		"name": p.plan_name,
		"description": p.description or "",
		"price": price(p.price, p.gst_included),
		"interval": p.billing_interval,
		"trialDays": cint(p.trial_days),
		"features": [line.strip() for line in (p.features or "").splitlines() if line.strip()],
		"highlighted": bool(cint(p.highlighted)),
	}


def site(t) -> dict:
	return {
		"id": t.name,
		"productSlug": t.product,
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
	return {
		"id": t.name,
		"customerId": t.account,
		"customerName": customer_name or frappe.db.get_value("GX Account", t.account, "account_name"),
		"productSlug": t.product,
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
	return {
		"id": s.name,
		"productName": product_name or frappe.db.get_value("GX Product", s.product, "product_name"),
		"planName": plan_name or frappe.db.get_value("GX Plan", s.plan, "plan_name"),
		"status": s.status,
		"price": price(s.price, s.gst_included),
		"interval": s.billing_interval,
		"trialEndsAt": iso(s.trial_ends_at),
		"currentPeriodEnd": iso(s.current_period_end),
		"cancelAtPeriodEnd": bool(cint(s.cancel_at_period_end)),
		"siteId": s.tenant or None,
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
