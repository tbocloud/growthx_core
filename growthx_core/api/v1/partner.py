import frappe
from frappe.query_builder.functions import Sum
from frappe.utils import add_months, flt, getdate, nowdate

from growthx_core.api.v1._core import ApiError, current_user, endpoint, money, monthly, net_of_gst
from growthx_core.partners.referrals import referral_url


def current_partner():
	partner = frappe.db.get_value(
		"GX Partner Profile",
		{"user": current_user()},
		["name", "referral_code", "sales_partner", "status"],
		as_dict=True,
	)
	if not partner:
		raise ApiError(403, "forbidden", "No partner profile is linked to this user.")
	return partner


@endpoint(methods=("GET",), roles=("partner",))
def summary():
	partner = current_partner()
	accounts = frappe.get_all("GX Account", {"referred_by": partner.name}, ["name", "status"])
	active = [a.name for a in accounts if a.status == "active"]

	rate = (
		flt(frappe.db.get_value("Sales Partner", partner.sales_partner, "commission_rate"))
		if partner.sales_partner
		else 0
	)
	# Commission rules (base, recurring or not) are pending D8: for now, the monthly rate x the
	# active subscriptions' monthly price excluding GST.
	recurring = 0.0
	if active and rate:
		for sub in frappe.get_all(
			"GX Subscription",
			{"account": ("in", active), "status": "active"},
			["price", "gst_included", "gst_rate", "billing_interval"],
		):
			recurring += (
				monthly(net_of_gst(sub.price, sub.gst_included, sub.gst_rate), sub.billing_interval)
				* rate
				/ 100
			)

	# No payouts exist yet (GX Payout is Phase 3), so all commission on paid invoices is pending.
	pending = 0.0
	if partner.sales_partner:
		si = frappe.qb.DocType("Sales Invoice")
		pending = flt(
			frappe.qb.from_(si)
			.select(Sum(si.total_commission))
			.where((si.sales_partner == partner.sales_partner) & (si.docstatus == 1) & (si.status == "Paid"))
			.run()[0][0]
		)

	return {
		"referralCode": partner.referral_code,
		"referralUrl": referral_url(partner.referral_code),
		"activeCustomers": len(active),
		"referredSignups": len(accounts),
		"openLeads": None,
		"monthlyRecurringCommission": money(recurring),
		"pendingPayout": money(pending),
	}


@endpoint(methods=("GET",), roles=("partner",))
def commission_history():
	partner = current_partner()
	first_month = getdate(add_months(nowdate(), -11)).replace(day=1)
	months = [add_months(first_month, i).strftime("%Y-%m") for i in range(12)]
	totals = dict.fromkeys(months, 0.0)

	if partner.sales_partner:
		rows = frappe.db.sql(
			"""
			select date_format(posting_date, '%%Y-%%m') as month, sum(total_commission) as amount
			from `tabSales Invoice`
			where sales_partner = %s and docstatus = 1 and posting_date >= %s
			group by month
			""",
			(partner.sales_partner, first_month),
			as_dict=True,
		)
		for row in rows:
			if row.month in totals:
				totals[row.month] = flt(row.amount)

	return {"items": [{"month": m, "amount": money(totals[m])} for m in months]}
