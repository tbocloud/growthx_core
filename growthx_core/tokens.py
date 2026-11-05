"""GrowthX API tokens (decision 1 in docs/api-decisions.md).

Opaque random tokens: `gxa_...` (access, 1 hour) and `gxr_...` (refresh, 30 days, rotated on use).
Only their SHA-256 hashes are stored, in `GX Auth Token`. Frappe calls `authenticate_request`
for every request (`auth_hooks` in hooks.py) and we set the session user when the token is valid.
"""

import hashlib
import secrets
from datetime import timedelta

import frappe
from frappe.utils import now_datetime

ACCESS_PREFIX = "gxa_"
REFRESH_PREFIX = "gxr_"
ACCESS_TTL = timedelta(hours=1)
REFRESH_TTL = timedelta(days=30)


def _hash(token: str) -> str:
	return hashlib.sha256(token.encode()).hexdigest()


def issue(user: str, client: str = "web") -> dict:
	"""Create a token pair for `user`. Returns the raw tokens; they are not retrievable later."""
	access = ACCESS_PREFIX + secrets.token_urlsafe(32)
	refresh = REFRESH_PREFIX + secrets.token_urlsafe(48)
	now = now_datetime()
	doc = frappe.get_doc(
		{
			"doctype": "GX Auth Token",
			"user": user,
			"client": client if client in ("web", "mobile") else "web",
			"status": "Active",
			"access_hash": _hash(access),
			"access_expires_at": now + ACCESS_TTL,
			"refresh_hash": _hash(refresh),
			"refresh_expires_at": now + REFRESH_TTL,
		}
	).insert(ignore_permissions=True)
	return {
		"accessToken": access,
		"expiresAt": doc.access_expires_at,
		"refreshToken": refresh,
		"refreshExpiresAt": doc.refresh_expires_at,
		"_name": doc.name,
	}


def authenticate_request():
	"""`auth_hooks` entry point: accept `Authorization: Bearer gxa_...`."""
	header = frappe.get_request_header("Authorization") or ""
	scheme, _, token = header.partition(" ")
	token = token.strip()
	if scheme.lower() != "bearer" or not token.startswith(ACCESS_PREFIX):
		return

	row = frappe.db.get_value(
		"GX Auth Token",
		{"access_hash": _hash(token)},
		["name", "user", "status", "access_expires_at"],
		as_dict=True,
	)
	if not row or row.status != "Active" or row.access_expires_at <= now_datetime():
		return
	if not frappe.db.get_value("User", row.user, "enabled"):
		return

	form_dict = frappe.local.form_dict  # set_user() clears it
	frappe.set_user(row.user)
	frappe.local.form_dict = form_dict
	frappe.local.gx_token = row.name


def rotate(refresh_token: str, client: str | None = None) -> dict | None:
	"""Exchange a refresh token for a new pair. Returns None if the token is unusable."""
	if not refresh_token or not refresh_token.startswith(REFRESH_PREFIX):
		return None
	row = frappe.db.get_value(
		"GX Auth Token",
		{"refresh_hash": _hash(refresh_token)},
		["name", "user", "status", "client", "refresh_expires_at"],
		as_dict=True,
		for_update=True,
	)
	if not row or row.status != "Active" or row.refresh_expires_at <= now_datetime():
		return None
	if not frappe.db.get_value("User", row.user, "enabled"):
		return None
	revoke(row.name)
	pair = issue(row.user, client or row.client)
	pair["_user"] = row.user
	return pair


def revoke(name: str):
	frappe.db.set_value("GX Auth Token", name, {"status": "Revoked", "revoked_at": now_datetime()})


def revoke_all(user: str, keep: str | None = None) -> int:
	"""Revoke the user's active tokens, except `keep` (a token pair's name). Returns how many."""
	names = frappe.get_all("GX Auth Token", {"user": user, "status": "Active"}, pluck="name")
	names = [n for n in names if n != keep]
	for name in names:
		revoke(name)
	return len(names)


def delete_expired_tokens():
	"""Daily: remove tokens whose refresh token expired (or that were revoked) over 7 days ago."""
	cutoff = now_datetime() - timedelta(days=7)
	frappe.db.delete("GX Auth Token", {"refresh_expires_at": ("<", cutoff)})
	frappe.db.delete("GX Auth Token", {"status": "Revoked", "revoked_at": ("<", cutoff)})
