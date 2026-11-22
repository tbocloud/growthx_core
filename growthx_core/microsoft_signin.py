"""Sign in with Microsoft (web): the Next.js server sends Microsoft's authorization code to
`auth.microsoft`, and Core exchanges it with Microsoft Entra ID for the user's email.

The ID token comes straight from Microsoft's token endpoint over TLS, so its claims are checked but
its signature isn't (Microsoft's guidance for the authorization-code flow, as with Google). The app
is registered single-tenant in the back office's directory and the token must come from that
directory, so nobody else's Microsoft account can sign in.

site_config:
- `microsoft_oauth_tenant_id`: the directory (tenant) ID, or `organizations` for any work account.
- `microsoft_oauth_client_id`, `microsoft_oauth_client_secret`: the app registration.
- `microsoft_oauth_redirect_uri`: optional; default `<growthx_web_url>/api/auth/microsoft/callback`.
  It must be the exact URI the website sent the user to Microsoft with.
"""

import base64
import json
import time

import frappe
import requests

from growthx_core.api.v1._core import ApiError, web_url

CALLBACK_PATH = "/api/auth/microsoft/callback"
ISSUER_PREFIX = "https://login.microsoftonline.com/"
ISSUER_SUFFIX = "/v2.0"
CLOCK_SKEW = 60


def _failed():
	return ApiError(400, "microsoft_failed", "Microsoft sign-in failed. Please try again.")


def _unavailable():
	return ApiError(503, "microsoft_unavailable", "Microsoft sign-in isn't available right now.")


def redirect_uri() -> str:
	return frappe.conf.get("microsoft_oauth_redirect_uri") or web_url(CALLBACK_PATH)


def _config() -> tuple[str, str, str]:
	tenant_id = frappe.conf.get("microsoft_oauth_tenant_id")
	client_id = frappe.conf.get("microsoft_oauth_client_id")
	client_secret = frappe.conf.get("microsoft_oauth_client_secret")
	if not tenant_id or not client_id or not client_secret:
		raise _unavailable()
	return tenant_id, client_id, client_secret


def token_url(tenant_id: str) -> str:
	return f"{ISSUER_PREFIX}{tenant_id}/oauth2/v2.0/token"


def verified_identity(code: str, code_verifier: str | None = None) -> dict:
	"""Exchange the code with Microsoft. Returns {email, name, sub} for a member of the directory."""
	tenant_id, client_id, client_secret = _config()
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
		response = requests.post(token_url(tenant_id), data=data, timeout=10)
	except requests.RequestException:
		frappe.log_error(title="Microsoft sign-in: token endpoint unreachable")
		raise _unavailable()
	if response.status_code != 200:
		# e.g. invalid_grant (code used or expired) or an unregistered redirect URI; never the secret.
		error = (response.json() if _is_json(response) else {}).get("error")
		if error != "invalid_grant":
			frappe.log_error(title=f"Microsoft sign-in: {error or response.status_code}")
		raise _failed()

	claims = _claims(response.json().get("id_token"))
	email = claims.get("email") or claims.get("preferred_username")
	if (
		claims.get("aud") != client_id
		or not _from_directory(claims, tenant_id)
		or not isinstance(claims.get("exp"), int)
		or claims["exp"] < time.time() - CLOCK_SKEW
		or not isinstance(email, str)
		or not email
		or not claims.get("sub")
	):
		raise _failed()
	return {
		"email": email.strip().lower(),
		"name": (claims.get("name") or "").strip(),
		"sub": claims["sub"],
	}


def _from_directory(claims: dict, tenant_id: str) -> bool:
	"""The token came from the configured directory, not another one or a personal account."""
	tid = claims.get("tid")
	if not isinstance(tid, str) or not tid:
		return False
	if tenant_id != "organizations" and tid != tenant_id:
		return False
	return claims.get("iss") == f"{ISSUER_PREFIX}{tid}{ISSUER_SUFFIX}"


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
