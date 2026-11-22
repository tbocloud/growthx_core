"""The partner program's rules, read from the `GX Partner Program Settings` Single so the business can
change them without a deploy. The published program is the source of truth for the defaults:

    tiers            Seed 20 % (from 0 active clients), Sprout 25 % (10), Canopy 30 % (30)
    minimum payout   Rs 1,000 (approved, unpaid commission, before TDS; smaller balances carry over)
    payout day       the 15th of each month
    TDS              2 % (Sec. 194H), withheld from each payout. PENDING BUSINESS CONFIRMATION.
    hold days        0: a commission is approvable as soon as the customer has paid. Raise it to
                     keep commission in review for a refund window.

An empty tier table means the published tiers, so a fresh site works before anyone opens the form.
"""

from datetime import date
from decimal import Decimal

import frappe
from frappe.utils import cint, flt, getdate

SETTINGS = "GX Partner Program Settings"
DEFAULT_TIERS = (("Seed", 0, Decimal("20")), ("Sprout", 10, Decimal("25")), ("Canopy", 30, Decimal("30")))
DEFAULT_MINIMUM_PAYOUT = Decimal("1000")
DEFAULT_PAYOUT_DAY = 15
DEFAULT_TDS_RATE = Decimal("2")
DEFAULT_HOLD_DAYS = 0


def _decimal(value, default: Decimal) -> Decimal:
	return default if value is None or value == "" else Decimal(str(value))


def settings() -> dict:
	"""{"tiers": [(name, min_active_clients, rate %)] ascending, "minimum_payout": Decimal rupees,
	"payout_day": int, "tds_rate": Decimal %, "hold_days": int}."""
	doc = frappe.get_single(SETTINGS)
	tiers = sorted(
		((t.tier_name, cint(t.min_active_clients), _decimal(t.rate, Decimal(0))) for t in doc.tiers),
		key=lambda t: t[1],
	)
	return {
		"tiers": tiers or list(DEFAULT_TIERS),
		"minimum_payout": _decimal(doc.minimum_payout, DEFAULT_MINIMUM_PAYOUT),
		"payout_day": min(max(cint(doc.payout_day) or DEFAULT_PAYOUT_DAY, 1), 28),
		"tds_rate": _decimal(doc.tds_rate, DEFAULT_TDS_RATE),
		"hold_days": max(cint(doc.hold_days), 0),
	}


def tier_for(active_clients: int, tiers) -> tuple[str, Decimal]:
	"""(name, rate %) of the highest tier whose threshold the partner has reached."""
	chosen = tiers[0]
	for tier in tiers:
		if active_clients >= tier[1]:
			chosen = tier
	return chosen[0], chosen[2]


def next_payout_date(today: date, payout_day: int) -> date:
	"""This month's payout day, or next month's once it has passed."""
	today = getdate(today)
	if today.day <= payout_day:
		return today.replace(day=payout_day)
	year, month = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
	return date(year, month, payout_day)


def ensure_defaults():
	"""After migrate: write the published tiers into the form the first time, so the business sees
	(and can edit) the rules in use. Never overwrites a table someone has filled in."""
	doc = frappe.get_single(SETTINGS)
	if doc.tiers:
		return
	for name, minimum, rate in DEFAULT_TIERS:
		doc.append("tiers", {"tier_name": name, "min_active_clients": minimum, "rate": flt(rate)})
	doc.flags.ignore_permissions = True
	doc.save()
