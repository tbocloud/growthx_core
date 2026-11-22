"""Sign-up tokens for social sign-in (Google, Microsoft, GitHub).

A new social user still has to fill in the sign-up form (account type, company, site). The token
carries the provider's identity to `auth.signup`; only its hash is stored, and it works for one
account. The identity is `{provider, id, email, name, emailVerified}`; a token issued before this
module existed (Google only: `{email, name, sub}`) still reads as a Google identity.
"""

import json

import frappe
from frappe.utils import sha256_hash

SIGNUP_TTL = 30 * 60  # time to fill in the sign-up form after choosing an account


def _cache_key(token: str) -> str:
	return f"growthx:social_signup:{sha256_hash(token)}"


def _legacy_key(token: str) -> str:
	return f"growthx:google_signup:{sha256_hash(token)}"


def issue(identity: dict) -> str:
	token = "gxs_" + frappe.generate_hash(length=43)
	frappe.cache.set_value(_cache_key(token), json.dumps(identity), expires_in_sec=SIGNUP_TTL)
	return token


def identity(token: str) -> dict | None:
	"""The identity behind a sign-up token, or None if it's unknown, expired or used."""
	value = frappe.cache.get_value(_cache_key(token)) or frappe.cache.get_value(_legacy_key(token))
	if not value:
		return None
	found = json.loads(value)
	found.setdefault("provider", "google")
	found.setdefault("id", found.get("sub"))
	found.setdefault("emailVerified", True)
	return found


def use(token: str):
	"""Once the account exists; kept until then so a form error (e.g. a taken subdomain) can be fixed."""
	frappe.cache.delete_value(_cache_key(token))
	frappe.cache.delete_value(_legacy_key(token))
