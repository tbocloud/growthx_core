"""One-time sign-in links from the control panel to a tenant site (`customer.open_site`).

The link carries an opaque random token, valid for 60 s and usable once. Only its hash is kept,
in Redis. The tenant side (an endpoint in growthx_theme that exchanges the token for a session
and redirects to a clean URL) is still to be built; it will call `redeem`.
"""

import hashlib
import secrets

import frappe

TTL_SECONDS = 60


def _key(token: str) -> str:
	return frappe.cache.make_key("gx:site-login:" + hashlib.sha256(token.encode()).hexdigest())


def issue(tenant: str, user: str) -> str:
	token = secrets.token_urlsafe(32)
	frappe.cache.set(_key(token), frappe.as_json({"tenant": tenant, "user": user}), ex=TTL_SECONDS)
	return token


def redeem(token: str) -> dict | None:
	"""Single use: returns {tenant, user} and deletes the token, or None if unknown/expired/used."""
	if not token:
		return None
	value = frappe.cache.getdel(_key(token))
	return frappe.parse_json(value) if value else None


def login_url(site_url: str, token: str) -> str:
	path = frappe.conf.get("growthx_tenant_login_path") or "/api/method/growthx_theme.sso.login"
	return f"{site_url.rstrip('/')}{path}?token={token}"
