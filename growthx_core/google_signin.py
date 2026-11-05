"""Sign in with Google (web): the Next.js server sends Google's authorization code to `auth.google`,
and Core exchanges it with Google for the user's verified email.

The ID token comes straight from Google's token endpoint over TLS, so its claims are checked but its
signature isn't (Google's guidance for the authorization-code flow). The Flutter app will send an ID
token instead, which will need a signature check against the Android and iOS client IDs.

site_config:
- `google_oauth_client_id`, `google_oauth_client_secret`: the web client (set by DevOps).
- `google_oauth_redirect_uri`: optional; default `<growthx_web_url>/api/auth/google/callback`. It
  must be the exact URI the website sent the user to Google with.
"""

import base64
import json
import time

import frappe
import requests
from frappe.utils import sha256_hash

from growthx_core.api.v1._core import ApiError, web_url

TOKEN_URL = "https://oauth2.googleapis.com/token"
ISSUERS = ("accounts.google.com", "https://accounts.google.com")
CALLBACK_PATH = "/api/auth/google/callback"
SIGNUP_TTL = 30 * 60  # time to fill in the sign-up form after choosing a Google account
CLOCK_SKEW = 60


def _failed():
	return ApiError(400, "google_failed", "Google sign-in failed. Please try again.")


def redirect_uri() -> str:
	return frappe.conf.get("google_oauth_redirect_uri") or web_url(CALLBACK_PATH)


def verified_identity(code: str, code_verifier: str | None = None) -> dict:
	"""Exchange the code with Google. Returns {email, name, sub} for a verified email."""
	client_id = frappe.conf.get("google_oauth_client_id")
	client_secret = frappe.conf.get("google_oauth_client_secret")
	if not client_id or not client_secret:
		raise ApiError(503, "google_unavailable", "Google sign-in isn't available right now.")

	data = {
		"code": code,
		"client_id": client_id,
		"client_secret": client_secret,
		"redirect_uri": redirect_uri(),
		"grant_type": "authorization_code",
	}
	if code_verifier:
		data["code_verifier"] = code_verifier
	try:
		response = requests.post(TOKEN_URL, data=data, timeout=10)
	except requests.RequestException:
		frappe.log_error(title="Google sign-in: token endpoint unreachable")
		raise ApiError(503, "google_unavailable", "Google sign-in isn't available right now.")
	if response.status_code != 200:
		# e.g. invalid_grant (code used or expired) or redirect_uri_mismatch; never the secret.
		error = (response.json() if _is_json(response) else {}).get("error")
		if error != "invalid_grant":
			frappe.log_error(title=f"Google sign-in: {error or response.status_code}")
		raise _failed()

	claims = _claims(response.json().get("id_token"))
	if (
		claims.get("aud") != client_id
		or claims.get("iss") not in ISSUERS
		or not isinstance(claims.get("exp"), int)
		or claims["exp"] < time.time() - CLOCK_SKEW
		or not claims.get("email")
		or not claims.get("sub")
	):
		raise _failed()
	if claims.get("email_verified") is not True:
		raise ApiError(403, "email_not_verified", "Your Google account's email isn't verified.")
	return {
		"email": claims["email"].strip().lower(),
		"name": (claims.get("name") or "").strip(),
		"sub": claims["sub"],
	}


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


# ---------------------------------------------------------------- sign-up tokens
# A new Google user still has to fill in the sign-up form (account type, company, site). The token
# carries the verified email to `auth.signup`; only its hash is stored, and it works for one account.


def _cache_key(token: str) -> str:
	return f"growthx:google_signup:{sha256_hash(token)}"


def issue_signup_token(identity: dict) -> str:
	token = "gxg_" + frappe.generate_hash(length=43)
	frappe.cache.set_value(_cache_key(token), json.dumps(identity), expires_in_sec=SIGNUP_TTL)
	return token


def signup_identity(token: str) -> dict | None:
	"""The identity behind a sign-up token, or None if it's unknown, expired or used."""
	value = frappe.cache.get_value(_cache_key(token))
	return json.loads(value) if value else None


def use_signup_token(token: str):
	"""Once the account exists; kept until then so a form error (e.g. a taken subdomain) can be fixed."""
	frappe.cache.delete_value(_cache_key(token))
