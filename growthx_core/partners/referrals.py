"""Referral codes and attribution.

Business rules here are placeholders until D8 (commission and attribution rules) is decided:
- a code must belong to an `active` partner,
- unknown codes and self-referrals are ignored (sign-up never fails because of a code),
- attribution is recorded once, at sign-up.
"""

import re
import secrets

import frappe

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I


def generate_code(name: str) -> str:
	"""e.g. 'Chidi Okafor' -> 'OKAFOR-7Q2'."""
	words = re.findall(r"[A-Za-z0-9]+", name or "")
	stem = (words[-1] if words else "GX").upper()[:10]
	for _ in range(20):
		code = f"{stem}-{''.join(secrets.choice(CODE_ALPHABET) for _ in range(3))}"
		if not frappe.db.exists("GX Partner Profile", {"referral_code": code}):
			return code
	return f"GX-{secrets.token_hex(4).upper()}"


def resolve(code: str | None, signup_email: str) -> dict | None:
	"""The active partner a referral code belongs to, or None if it should be ignored."""
	if not code:
		return None
	partner = frappe.db.get_value(
		"GX Partner Profile",
		{"referral_code": code.strip().upper(), "status": "active"},
		["name", "user", "sales_partner"],
		as_dict=True,
	)
	if not partner:
		return None
	if (partner.user or "").lower() == signup_email.lower():
		return None  # self-referral
	return partner


def referral_url(code: str) -> str:
	from growthx_core.api.v1._core import web_url

	# The website's welcome page ("<partner> invited you"); it records the code and leads to sign-up.
	return web_url(f"/r/{code}")
