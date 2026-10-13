"""Single sign-on from the GrowthX control panel to a customer's tenant site ("Open my app").

    customer.open_site (Core)   -> issue(): a random single-use token, 60 s, only its hash in Redis
    browser -> tenant           -> /api/method/growthx_theme.sso.login?token=...
    tenant  -> Core             -> tenant.redeem_login {token}, header X-GX-Tenant-Key
    Core                        -> checks the tenant key, then redeem(): {tenant, user}, once
    tenant                      -> logs that user in and redirects to the desk (clean URL)

Each tenant has its own random key. Core stores only its hash (`GX Tenant.tenant_key_hash`);
the key itself lives only in the tenant's site_config (`growthx_tenant_key`), written during
provisioning. A token issued for tenant A can't be redeemed by tenant B.
"""

import hashlib
import hmac
import json
import secrets

import frappe

TTL_SECONDS = 60
TENANT_KEY_HEADER = "X-GX-Tenant-Key"
DEFAULT_LOGIN_PATH = "/api/method/growthx_theme.sso.login"


def _hash(value: str) -> str:
	return hashlib.sha256(value.encode()).hexdigest()


def _key(token: str) -> str:
	return frappe.cache.make_key("gx:site-login:" + _hash(token))


# ---------------------------------------------------------------- one-time login tokens


def issue(tenant: str, user: str) -> str:
	token = secrets.token_urlsafe(32)
	frappe.cache.set(_key(token), frappe.as_json({"tenant": tenant, "user": user}), ex=TTL_SECONDS)
	return token


def redeem(token: str) -> dict | None:
	"""Single use: returns {tenant, user} and deletes the token, or None if unknown/expired/used."""
	if not token:
		return None
	value = frappe.cache.getdel(_key(token))
	return json.loads(value) if value else None  # Redis returns bytes


def login_url(site_url: str, token: str) -> str:
	path = frappe.conf.get("growthx_tenant_login_path") or DEFAULT_LOGIN_PATH
	return f"{site_url.rstrip('/')}{path}?token={token}"


# ---------------------------------------------------------------- per-tenant keys


def new_tenant_key(tenant) -> str:
	"""Generate a new key for `tenant` (a GX Tenant doc), store its hash, and return the key.

	The caller must write the key to the tenant's site_config straight away; it can't be
	recovered later. Generating a new key invalidates the old one."""
	key = "gxt_" + secrets.token_urlsafe(32)
	tenant.db_set("tenant_key_hash", _hash(key), update_modified=False)
	return key


def tenant_for_key(key: str | None) -> str | None:
	"""The GX Tenant that owns this key, or None."""
	if not key or not key.startswith("gxt_"):
		return None
	digest = _hash(key)
	name = frappe.db.get_value("GX Tenant", {"tenant_key_hash": digest}, "name")
	if not name:
		return None
	# Constant-time comparison, so the check doesn't leak timing about the stored hash.
	stored = frappe.db.get_value("GX Tenant", name, "tenant_key_hash") or ""
	return name if hmac.compare_digest(stored, digest) else None


def core_url() -> str:
	"""The URL tenants use to reach this Core site (server to server)."""
	return (frappe.conf.get("growthx_core_url") or frappe.utils.get_url()).rstrip("/")
