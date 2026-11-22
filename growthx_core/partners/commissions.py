"""Partner commissions and payouts: the ledger behind GX Commission and GX Payout.

Money. DocTypes hold rupees (Currency, 2 decimals) like the rest of ERPNext, the API speaks integer
paise (`_core.money`). Every calculation here is done in whole paise with Decimal and ROUND_HALF_UP,
so a commission, a payout total and the TDS on it never disagree by a paisa:

    commission = round(invoice amount excl. GST x rate)             per payment
    gross      = sum of the commissions in the payout (already whole paise)
    tds        = round(gross x TDS rate)                            once per payout, never per line
    net        = gross - tds

Lifecycle. A payment for a referred customer creates one `GX Commission` (pending). An admin approves
it (not before `approvable_from`, which is the payment date plus the program's hold days), and a
payout run gathers a partner's approved, unpaid commissions into a draft `GX Payout` if they reach the
minimum. Marking the payout paid makes its commissions paid. A refund voids a commission that isn't
paid yet. Every step that changes money is guarded by a status check under a row lock, so repeating
a request can't pay a commission twice.
"""

import hashlib
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

import frappe
from frappe.utils import add_days, flt, fmt_money, getdate, now_datetime

from growthx_core.partners import program

PAISA = Decimal(1)
# Commission rows an admin or a refund may still void; a paid one is final.
VOIDABLE = ("pending", "approved")


# ---------------------------------------------------------------- money (whole paise)


def to_paise(rupees) -> int:
	"""Rupees (ERPNext decimal) -> whole paise, half up."""
	return int((Decimal(str(rupees or 0)) * 100).quantize(PAISA, rounding=ROUND_HALF_UP))


def to_rupees(paise: int) -> float:
	return float(Decimal(int(paise)) / 100)


def percent_of(paise: int, percent) -> int:
	"""`percent` % of an amount in paise, rounded half up to a whole paisa."""
	return int((Decimal(int(paise)) * Decimal(str(percent)) / 100).quantize(PAISA, rounding=ROUND_HALF_UP))


def net_of_gst_paise(gross_paise: int, gst_rate) -> int:
	"""The amount excluding GST from one that includes it, half up."""
	return int(
		(Decimal(int(gross_paise)) * 100 / (100 + Decimal(str(gst_rate or 0)))).quantize(
			PAISA, rounding=ROUND_HALF_UP
		)
	)


def tds_paise(gross_paise: int, tds_rate) -> int:
	return percent_of(gross_paise, tds_rate)


def payout_totals(commission_paise: list[int], tds_rate) -> tuple[int, int, int]:
	"""(gross, tds, net) in paise for a payout of these commissions."""
	gross = sum(commission_paise)
	tds = tds_paise(gross, tds_rate)
	return gross, tds, gross - tds


# ---------------------------------------------------------------- creating commissions


def active_clients(partner: str) -> int:
	return frappe.db.count("GX Account", {"referred_by": partner, "status": "active"})


def payment_key(account: str, reference: str) -> str:
	"""One commission per payment. Hashed because a reference can be up to 140 characters."""
	return hashlib.sha1(f"{account}:{reference}".encode()).hexdigest()


def plan_gross_paise(plan) -> int:
	"""What a customer pays for the plan, GST included."""
	base = to_paise(plan.price)
	if frappe.utils.cint(plan.gst_included):
		return base
	return base + percent_of(base, plan.gst_rate or 0)


def record_for_payment(
	account,
	plan,
	reference: str,
	paid_on: date,
	gross_amount=None,
	gateway: str | None = None,
	sales_invoice: str | None = None,
	subscription: str | None = None,
) -> str | None:
	"""A customer paid: create the commission of the partner who referred them, once. Returns the
	GX Commission name (the existing one when called again for the same payment), or None when there
	is nothing to pay. Never raises, so a problem here can't stop the customer's plan from activating
	(it goes to the Error Log and an admin can see the missing row)."""
	if not account.referred_by:
		return None
	try:
		frappe.db.savepoint("gx_commission")
		return _record(account, plan, reference, paid_on, gross_amount, gateway, sales_invoice, subscription)
	except Exception:
		frappe.db.rollback(save_point="gx_commission")
		frappe.log_error(title=f"GrowthX: commission for {account.name} / {reference} not recorded")
		return None


def _record(account, plan, reference, paid_on, gross_amount, gateway, sales_invoice, subscription):
	key = payment_key(account.name, reference)
	existing = frappe.db.get_value("GX Commission", {"payment_key": key})
	if existing:
		return existing
	partner = account.referred_by
	if not frappe.db.exists("GX Partner Profile", partner):
		return None

	gross = to_paise(gross_amount) if gross_amount is not None else plan_gross_paise(plan)
	net = None
	if sales_invoice:
		# The invoice knows its own GST split; use it rather than dividing the total again.
		net_total = frappe.db.get_value("Sales Invoice", sales_invoice, "base_net_total")
		net = to_paise(net_total) if net_total else None
	if net is None:
		net = net_of_gst_paise(gross, plan.gst_rate)
	if net <= 0:
		return None

	rules = program.settings()
	clients = active_clients(partner)
	tier, rate = program.tier_for(clients, rules["tiers"])
	first = not frappe.db.exists("GX Commission", {"account": account.name, "status": ("!=", "void")})
	paid_on = getdate(paid_on)
	try:
		doc = frappe.get_doc(
			{
				"doctype": "GX Commission",
				"partner": partner,
				"account": account.name,
				"status": "pending",
				"commission_type": "first_month" if first else "recurring",
				"paid_on": paid_on,
				"invoice_amount": to_rupees(net),
				"rate": float(rate),
				"amount": to_rupees(percent_of(net, rate)),
				"payment_key": key,
				"payment_reference": (reference or "")[:140],
				"gateway": gateway,
				"sales_invoice": sales_invoice,
				"subscription": subscription,
				"plan": plan.name,
				"gross_amount": to_rupees(gross),
				"tier": tier,
				"active_clients": clients,
				"approvable_from": add_days(paid_on, rules["hold_days"]),
			}
		).insert(ignore_permissions=True)
	except frappe.DuplicateEntryError:
		# A concurrent request recorded the same payment first.
		return frappe.db.get_value("GX Commission", {"payment_key": key})

	if frappe.db.get_value("GX Partner Profile", partner, "tier") != tier:
		frappe.db.set_value("GX Partner Profile", partner, "tier", tier, update_modified=False)
	_notify_earned(doc, account)
	return doc.name


def _notify_earned(commission, account):
	from growthx_core.partners.notifications import notify

	user = frappe.db.get_value("GX Partner Profile", commission.partner, "user")
	if user:
		notify(
			user,
			"commission",
			"Commission earned",
			f"{fmt_money(commission.amount, currency='INR')} from {account.account_name}'s payment."
			" It is under review.",
			deep_link_type="commission",
			deep_link_id=commission.name,
			dedupe_key=f"commission-earned:{commission.name}",
		)


# ---------------------------------------------------------------- approving and voiding


def approve(names: list[str], approver: str) -> tuple[list[str], list[dict]]:
	"""Approve pending commissions. Returns (approved names, [{"id", "reason"}] for the ones skipped:
	not pending, still in the hold period)."""
	approved, skipped = [], []
	today = getdate()
	for name in names:
		c = _locked(name)
		if not c:
			skipped.append({"id": name, "reason": "not_found"})
		elif c.status != "pending":
			skipped.append({"id": name, "reason": f"already_{c.status}"})
		elif c.approvable_from and getdate(c.approvable_from) > today:
			skipped.append({"id": name, "reason": "on_hold"})
		else:
			frappe.db.set_value(
				"GX Commission",
				name,
				{"status": "approved", "approved_at": now_datetime(), "approved_by": approver},
				update_modified=True,
			)
			approved.append(name)
	return approved, skipped


def void(names: list[str], reason: str) -> tuple[list[str], list[dict]]:
	"""Void commissions that aren't paid yet. One in a draft payout leaves it, and the payout is
	recalculated (or cancelled when nothing is left in it)."""
	voided, skipped = [], []
	for name in names:
		c = _locked(name)
		if not c:
			skipped.append({"id": name, "reason": "not_found"})
		elif c.status not in VOIDABLE:
			skipped.append({"id": name, "reason": f"already_{c.status}"})
		else:
			frappe.db.set_value(
				"GX Commission",
				name,
				{"status": "void", "void_reason": reason, "voided_at": now_datetime(), "payout": None},
			)
			voided.append(name)
			if c.payout:
				recalculate_payout(c.payout)
	return voided, skipped


def void_for_payment(account: str, reference: str, reason: str) -> list[str]:
	"""A payment was refunded or cancelled: void its commission unless it is already paid (then an
	admin must claw it back from the partner's next payout; the caller audits that)."""
	name = frappe.db.get_value("GX Commission", {"payment_key": payment_key(account, reference)})
	if not name:
		return []
	return void([name], reason)[0]


def _locked(name: str):
	rows = frappe.db.get_values(
		"GX Commission",
		{"name": name},
		["name", "status", "payout", "approvable_from"],
		as_dict=True,
		for_update=True,
	)
	return rows[0] if rows else None


# ---------------------------------------------------------------- payouts


def recalculate_payout(name: str):
	"""Re-add a draft payout's commissions after one left it."""
	payout = frappe.db.get_value("GX Payout", name, ["status", "tds_rate"], as_dict=True)
	if not payout or payout.status != "draft":
		return
	rows = frappe.db.get_values(
		"GX Commission", {"payout": name, "status": "approved"}, ["amount"], as_dict=True, for_update=True
	)
	if not rows:
		frappe.db.set_value("GX Payout", name, {"status": "cancelled", "commission_count": 0})
		return
	gross, tds, net = payout_totals([to_paise(r.amount) for r in rows], payout.tds_rate)
	frappe.db.set_value(
		"GX Payout",
		name,
		{
			"commission_count": len(rows),
			"gross": to_rupees(gross),
			"tds": to_rupees(tds),
			"net": to_rupees(net),
		},
	)


def valid_period(period) -> tuple[date, date]:
	"""(first day, last day) of a YYYY-MM month, or ValueError."""
	if not isinstance(period, str) or len(period) != 7 or period[4] != "-":
		raise ValueError(period)
	first = date(int(period[:4]), int(period[5:]), 1)
	nxt = date(first.year + (first.month == 12), first.month % 12 + 1, 1)
	return first, add_days(nxt, -1)


def create_payouts(period: str, creator: str) -> tuple[list[dict], list[dict]]:
	"""For every partner with approved, unpaid commission on payments up to the end of `period` that
	adds up to the minimum payout, make a draft payout and take those commissions into it. Returns
	(created [{"name", "partner", ...}], skipped [{"partner", "reason", "amount"}]). Safe to repeat:
	commissions already in a payout are not picked up again."""
	_first, last = valid_period(period)
	rules = program.settings()
	minimum = to_paise(rules["minimum_payout"])
	rows = frappe.db.get_values(
		"GX Commission",
		{"status": "approved", "payout": ("is", "not set"), "paid_on": ("<=", last)},
		["name", "partner", "amount"],
		order_by="paid_on asc, creation asc",
		as_dict=True,
		for_update=True,
	)
	by_partner: dict[str, list] = {}
	for r in rows:
		by_partner.setdefault(r.partner, []).append(r)

	created, skipped = [], []
	for partner, items in by_partner.items():
		status = frappe.db.get_value("GX Partner Profile", partner, "status")
		amounts = [to_paise(r.amount) for r in items]
		if status != "active":
			skipped.append({"partner": partner, "reason": "partner_not_active", "amount": sum(amounts)})
			continue
		if sum(amounts) < minimum:
			skipped.append({"partner": partner, "reason": "below_minimum", "amount": sum(amounts)})
			continue
		gross, tds, net = payout_totals(amounts, rules["tds_rate"])
		payout = frappe.get_doc(
			{
				"doctype": "GX Payout",
				"partner": partner,
				"period": period,
				"status": "draft",
				"commission_count": len(items),
				"gross": to_rupees(gross),
				"tds_rate": float(rules["tds_rate"]),
				"tds": to_rupees(tds),
				"net": to_rupees(net),
				"created_by": creator,
			}
		).insert(ignore_permissions=True)
		frappe.db.set_value("GX Commission", {"name": ("in", [r.name for r in items])}, "payout", payout.name)
		created.append(payout)
	return created, skipped


def cancel_payout(name: str) -> None:
	"""A draft payout is dropped; its commissions go back to approved and unpaid."""
	frappe.db.set_value("GX Commission", {"payout": name}, "payout", None)
	frappe.db.set_value("GX Payout", name, {"status": "cancelled", "commission_count": 0})


def mark_paid(name: str, reference: str, paid_on: date, method: str, paid_by: str) -> int:
	"""The payout was transferred: it and its commissions become paid. Caller holds the payout's row
	lock and has checked it is a draft. Returns how many commissions were paid."""
	rows = frappe.db.get_values(
		"GX Commission",
		{"payout": name, "status": "approved"},
		["name", "amount"],
		as_dict=True,
		for_update=True,
	)
	frappe.db.set_value(
		"GX Commission",
		{"name": ("in", [r.name for r in rows] or [""]), "status": "approved"},
		{"status": "paid", "paid_at": now_datetime()},
	)
	frappe.db.set_value(
		"GX Payout",
		name,
		{
			"status": "paid",
			"paid_on": paid_on,
			"reference": reference,
			"method": method,
			"paid_by": paid_by,
			"commission_count": len(rows),
		},
	)
	return len(rows)


def notify_payout_paid(payout) -> None:
	"""Tell the partner: always in the app, and by email when they opted in to commission updates."""
	from growthx_core.partners.notifications import notify

	profile = frappe.db.get_value(
		"GX Partner Profile",
		payout.partner,
		["user", "partner_name", "notify_commissions"],
		as_dict=True,
	)
	if not profile or not profile.user:
		return
	net = fmt_money(payout.net, currency="INR")
	notify(
		profile.user,
		"payout",
		"Payout sent",
		f"{net} for {payout.period} was paid on {getdate(payout.paid_on).isoformat()}"
		f" (reference {payout.reference}).",
		deep_link_type="payout",
		deep_link_id=payout.name,
		dedupe_key=f"payout-paid:{payout.name}",
	)
	if not profile.notify_commissions:
		return
	email = frappe.db.get_value("User", profile.user, "email")
	if not email:
		return
	esc = frappe.utils.escape_html
	try:
		frappe.sendmail(
			recipients=[email],
			subject=f"Your GrowthX payout for {payout.period} has been sent",
			message=(
				f"<p>Hi {esc(profile.partner_name)},</p>"
				f"<p>We've paid <b>{esc(net)}</b> to you for {esc(payout.period)}"
				f" (commission {esc(fmt_money(payout.gross, currency='INR'))}"
				f" less TDS {esc(fmt_money(payout.tds, currency='INR'))}).</p>"
				f"<p>Reference: {esc(payout.reference)}, paid on {esc(getdate(payout.paid_on).isoformat())}.</p>"
			),
		)
	except Exception:
		frappe.log_error(title=f"GrowthX: could not send payout email for {payout.name}")


# ---------------------------------------------------------------- partner totals


def earnings_paise(partner: str, today: date) -> dict:
	"""Whole-paise totals of one partner's commissions (void ones excluded), for partner.earnings."""
	this_month = today.replace(day=1)
	last_month = this_month.replace(
		year=this_month.year - (this_month.month == 1), month=this_month.month - 1 or 12
	)
	totals = dict.fromkeys(("pending", "approved", "paid", "this_month", "last_month"), 0)
	for c in frappe.get_all(
		"GX Commission",
		{"partner": partner, "status": ("!=", "void")},
		["status", "amount", "paid_on"],
	):
		paise = to_paise(c.amount)
		totals[c.status] += paise
		day = getdate(c.paid_on)
		if day >= this_month:
			totals["this_month"] += paise
		elif day >= last_month:
			totals["last_month"] += paise
	return totals
