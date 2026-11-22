"""Sign in with Microsoft (web): the Next.js server sends Microsoft's authorization code to
`auth.microsoft`, and Core exchanges it with Microsoft Entra ID for the user's email.

One multi-tenant app registration ("accounts in any organizational directory and personal Microsoft
accounts", authority `common`). Whose token it is decides what happens (see `auth.microsoft`):
- a member of the staff directory (`microsoft_oauth_staff_tenant_id`, teambackoffice.com) is
  back-office staff and becomes a GrowthX Admin;
- anyone else is a customer, who signs in or signs up like with Google. Their email counts as
  verified only for a personal Microsoft account, or a work account whose token carries the optional
  claim `xms_edov` (the app registration must add the optional claims `email` and `xms_edov`).

The ID token comes straight from Microsoft's token endpoint over TLS, so its claims are checked but
its signature isn't (Microsoft's guidance for the authorization-code flow, as with Google). The
issuer is checked against the token's own `tid`.

site_config:
- `microsoft_oauth_client_id`, `microsoft_oauth_client_secret`: the app registration.
- `microsoft_oauth_staff_tenant_id`: the staff directory (tenant) ID. The old
  `microsoft_oauth_tenant_id` is read when it isn't set (a GUID only; `organizations` means no staff).
- `microsoft_oauth_redirect_uri`: optional; default `<growthx_web_url>/api/auth/microsoft/callback`.
  It must be the exact URI the website sent the user to Microsoft with.
"""

import base64
import json
import re
import time

import frappe
import requests

from growthx_core.api.v1._core import ApiError, web_url

CALLBACK_PATH = "/api/auth/microsoft/callback"
AUTHORITY = "common"
PERSONAL_TENANT = "9188040d-6c67-4c5b-b112-36a304b66dad"  # personal Microsoft accounts (MSA)
GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
ISSUER_PREFIX = "https://login.microsoftonline.com/"
ISSUER_SUFFIX = "/v2.0"
CLOCK_SKEW = 60


def _failed():
	return ApiError(400, "microsoft_failed", "Microsoft sign-in failed. Please try again.")


def _unavailable():
	return ApiError(503, "microsoft_unavailable", "Microsoft sign-in isn't available right now.")


def redirect_uri() -> str:
	return frappe.conf.get("microsoft_oauth_redirect_uri") or web_url(CALLBACK_PATH)


def staff_tenant() -> str | None:
	value = frappe.conf.get("microsoft_oauth_staff_tenant_id") or frappe.conf.get("microsoft_oauth_tenant_id")
	if not isinstance(value, str) or not GUID.match(value.strip()):
		return None
	return value.strip().lower()


def _config() -> tuple[str, str]:
	client_id = frappe.conf.get("microsoft_oauth_client_id")
	client_secret = frappe.conf.get("microsoft_oauth_client_secret")
	if not client_id or not client_secret:
		raise _unavailable()
	return client_id, client_secret


def token_url() -> str:
	return f"{ISSUER_PREFIX}{AUTHORITY}/oauth2/v2.0/token"


def verified_identity(code: str, code_verifier: str | None = None) -> dict:
	"""Exchange the code with Microsoft. Returns
	`{provider, id, email, name, sub, tid, staff, emailVerified}`."""
	client_id, client_secret = _config()
	data = {
		"code": code,
		"client_id": client_id,
		"client_secret": client_secret,
		"redirect_uri": redirect_uri(),
		"grant_type": "authorization_code",
		"scope": "openid profile email",
	}
	if code_verifier:
		data["code_verifier"] = code_verifier
	try:
		response = requests.post(token_url(), data=data, timeout=10)
	except requests.RequestException:
		frappe.log_error(title="Microsoft sign-in: token endpoint unreachable")
		raise _unavailable()
	if response.status_code != 200:
		# e.g. invalid_grant (code used or expired) or an unregistered redirect URI; never the secret.
		error = (response.json() if _is_json(response) else {}).get("error")
		if error != "invalid_grant":
			frappe.log_error(title=f"Microsoft sign-in: {error or response.status_code}")
		raise _failed()

	if not _is_json(response):
		raise _failed()
	claims = _claims(response.json().get("id_token"))
	tid = claims.get("tid")
	upn = claims.get("preferred_username")
	email = claims.get("email") or upn
	if (
		claims.get("aud") != client_id
		or not isinstance(tid, str)
		or not GUID.match(tid)
		or claims.get("iss") != f"{ISSUER_PREFIX}{tid}{ISSUER_SUFFIX}"
		or not isinstance(claims.get("exp"), int)
		or claims["exp"] < time.time() - CLOCK_SKEW
		or (isinstance(claims.get("nbf"), int) and claims["nbf"] > time.time() + CLOCK_SKEW)
		or not isinstance(email, str)
		or "@" not in email
		or not claims.get("sub")
	):
		raise _failed()
	tid = tid.lower()
	# `oid` is the user's id in their directory; with `tid` it is stable (`sub` is per app).
	oid = claims.get("oid") if isinstance(claims.get("oid"), str) and claims.get("oid") else None
	ident = f"{tid}.{oid.lower()}" if oid else f"{tid}.{claims['sub']}"
	staff = tid == staff_tenant() and "#EXT#" not in (upn if isinstance(upn, str) else "").upper()
	return {
		"provider": "microsoft",
		"id": ident,
		"email": email.strip().lower(),
		"name": (claims.get("name") or "").strip(),
		"sub": claims["sub"],
		"tid": tid,
		# A guest of the staff directory (`#EXT#` in the user name) is not staff.
		"staff": staff,
		"emailVerified": staff or _email_verified(claims, tid),
	}


def _email_verified(claims: dict, tid: str) -> bool:
	"""A personal Microsoft account's email is its sign-in name, which Microsoft verified; a work
	account's `email` is only as good as the directory's say-so, which `xms_edov` reports."""
	if tid == PERSONAL_TENANT:
		return True
	return claims.get("xms_edov") in (True, 1, "1", "true", "True")


def _claims(id_token) -> dict:
	try:
		payload = id_token.split(".")[1]
		claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
	except Exception:
		raise _failed()
	if not isinstance(claims, dict):
		raise _failed()
	return claims


def _is_json(response) -> bool:
	try:
		return isinstance(response.json(), dict)
	except ValueError:
		return False
