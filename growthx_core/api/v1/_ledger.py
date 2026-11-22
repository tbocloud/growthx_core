"""Commission and payout shapes shared by the partner and admin endpoints."""

import frappe

from growthx_core.api.v1._core import iso, money

COMMISSION_FIELDS = [
	"name",
	"partner",
	"account",
	"status",
	"commission_type",
	"paid_on",
	"invoice_amount",
	"rate",
	"amount",
	"payment_reference",
	"sales_invoice",
	"tier",
	"active_clients",
	"approvable_from",
	"approved_at",
	"void_reason",
	"payout",
	"creation",
]
PAYOUT_FIELDS = [
	"name",
	"partner",
	"period",
	"status",
	"commission_count",
	"gross",
	"tds_rate",
	"tds",
	"net",
	"paid_on",
	"reference",
	"method",
	"creation",
]


def account_names(rows) -> dict:
	names = list({r.account for r in rows})
	if not names:
		return {}
	return dict(frappe.get_all("GX Account", {"name": ("in", names)}, ["name", "account_name"], as_list=True))


def partner_names(rows) -> dict:
	names = list({r.partner for r in rows})
	if not names:
		return {}
	return dict(
		frappe.get_all("GX Partner Profile", {"name": ("in", names)}, ["name", "partner_name"], as_list=True)
	)


def commission(c, customers: dict) -> dict:
	"""A GX Commission row as the partner sees it (the API contract calls a void one "reversed")."""
	status = "reversed" if c.status == "void" else c.status
	return {
		"id": c.name,
		"customerId": c.account,
		"customerName": customers.get(c.account),
		"invoiceId": c.sales_invoice or c.payment_reference or None,
		"type": c.commission_type,
		"base": money(c.invoice_amount),
		"rate": round(c.rate / 100, 6),
		"amount": money(c.amount),
		"status": status,
		"earnedAt": iso(c.paid_on),
		"reviewUntil": iso(c.approvable_from) if c.status == "pending" else None,
		"payoutId": c.payout or None,
	}


def admin_commission(c, customers: dict, partners: dict) -> dict:
	return {
		**commission(c, customers),
		"status": c.status,
		"partnerId": c.partner,
		"partnerName": partners.get(c.partner),
		"tier": c.tier or None,
		"activeClients": c.active_clients,
		"approvedAt": iso(c.approved_at),
		"voidReason": c.void_reason or None,
		"createdAt": iso(c.creation),
	}


def payout(p, partners: dict | None = None) -> dict:
	result = {
		"id": p.name,
		"reference": p.reference or None,
		"period": p.period,
		"status": p.status,
		"paidAt": iso(p.paid_on),
		"method": p.method or None,
		"commissionCount": p.commission_count,
		"gross": money(p.gross),
		"tdsRate": round(p.tds_rate / 100, 6),
		"tds": money(p.tds),
		"net": money(p.net),
	}
	if partners is not None:
		result.update(
			{"partnerId": p.partner, "partnerName": partners.get(p.partner), "createdAt": iso(p.creation)}
		)
	return result
