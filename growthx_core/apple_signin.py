"""Sign in with Apple (web, Services ID flow): the Next.js server receives Apple's form POST and sends the
authorization code to `auth.apple`; Core exchanges it with Apple for the user's verified email.

Apple wants a *client secret* that is a JWT signed (ES256) with the account's Sign in with Apple private
key (.p8). It is made here for every request and lives ten minutes; the key never leaves Core. PyJWT and
`cryptography` ship with Frappe, so nothing is added to the bench.

The ID token comes straight from Apple's token endpoint over TLS, so its claims are checked but its
signature isn't (same as Google). `nonce` (sent by the website at the start of the flow) is checked when
the caller gives one. Apple never grants admin; a private-relay address is just an email address that
matches an existing account only when it is identical.

site_config:
- `apple_oauth_client_id`: the Services ID (e.g. `com.tbogrowthx.web`).
- `apple_oauth_team_id`, `apple_oauth_key_id`: the Apple Developer Team ID and the Key ID.
- `apple_oauth_private_key`: the contents of the `.p8` file (newlines may be stored as the two characters
  `\\n`).
- `apple_oauth_redirect_uri`: optional; default `<growthx_web_url>/api/auth/apple/callback`. It must be the
  exact Return URL registered at Apple and the one the website used.
"""

import base64
import hmac
import json
import re
import time

import frappe
import jwt
import requests

from growthx_core.api.v1._core import ApiError, web_url

AUTH_URL = "https://appleid.apple.com"
TOKEN_URL = "https://appleid.apple.com/auth/token"
CALLBACK_PATH = "/api/auth/apple/callback"
CLIENT_SECRET_TTL = 10 * 60  # Apple allows up to six months; a request needs seconds
CLOCK_SKEW = 60
RELAY_SUFFIX = "@privaterelay.appleid.com"
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _failed():
	return ApiError(400, "apple_failed", "Apple sign-in failed. Please try again.")


def _unavailable():
	return ApiError(503, "apple_unavailable", "Apple sign-in isn't available right now.")


def redirect_uri() -> str:
	return frappe.conf.get("apple_oauth_redirect_uri") or web_url(CALLBACK_PATH)


def _private_key() -> str:
	return (frappe.conf.get("apple_oauth_private_key") or "").replace("\\n", "\n").strip()


def client_secret(now: int | None = None) -> str:
	"""The JWT Apple takes as `client_secret`: ES256, `kid` the Key ID, issued by the Team ID for the Services ID."""
	client_id = frappe.conf.get("apple_oauth_client_id")
	team_id = frappe.conf.get("apple_oauth_team_id")
	key_id = frappe.conf.get("apple_oauth_key_id")
	key = _private_key()
	if not (client_id and team_id and key_id and key):
		raise _unavailable()
	now = now or int(time.time())
	try:
		return jwt.encode(
			{"iss": team_id, "iat": now, "exp": now + CLIENT_SECRET_TTL, "aud": AUTH_URL, "sub": client_id},
			key,
			algorithm="ES256",
			headers={"kid": key_id},
		)
	except Exception:
		# A key that isn't a P-256 EC private key. Never log the key itself.
		frappe.log_error(title="Apple sign-in: the private key can't sign")
		raise _unavailable()


def _bool(value) -> bool:
	"""Apple sends `email_verified` and `is_private_email` as booleans or as the strings "true"/"false"."""
	return value is True or (isinstance(value, str) and value.lower() == "true")


def clean_name(value) -> str:
	"""A display name from the form Apple posts on the first authorization: single-spaced, no control characters."""
	if not isinstance(value, str):
		return ""
	return " ".join(_CONTROL.sub(" ", value).split())[:140]


def verified_identity(
	code: str, code_verifier: str | None = None, *, nonce: str | None = None, name: str | None = None
) -> dict:
	"""Exchange the code with Apple. Returns {provider, id, email, name, sub, emailVerified, privateEmail}.
	`code_verifier` is accepted for the common signature but unused (Apple's web flow has no PKCE here).
	`name` is what Apple posted next to the code (the ID token has none), only on the first authorization."""
	client_id = frappe.conf.get("apple_oauth_client_id")
	data = {
		"code": code,
		"client_id": client_id,
		"client_secret": client_secret(),
		"redirect_uri": redirect_uri(),
		"grant_type": "authorization_code",
	}
	try:
		response = requests.post(TOKEN_URL, data=data, timeout=10)
	except requests.RequestException:
		frappe.log_error(title="Apple sign-in: token endpoint unreachable")
		raise _unavailable()
	if response.status_code != 200:
		# e.g. invalid_grant (code used or expired), invalid_client (Services ID, key or redirect URI wrong).
		error = (response.json() if _is_json(response) else {}).get("error")
		if error != "invalid_grant":
			frappe.log_error(title=f"Apple sign-in: {error or response.status_code}")
		raise _failed()

	claims = _claims(response.json().get("id_token") if _is_json(response) else None)
	if (
		claims.get("aud") != client_id
		or claims.get("iss") != AUTH_URL
		or not isinstance(claims.get("exp"), int)
		or claims["exp"] < time.time() - CLOCK_SKEW
		or not isinstance(claims.get("sub"), str)
		or not claims["sub"]
	):
		raise _failed()
	if nonce and not (
		isinstance(claims.get("nonce"), str) and hmac.compare_digest(claims["nonce"].encode(), nonce.encode())
	):
		raise _failed()
	email = claims.get("email")
	if not isinstance(email, str) or not email.strip() or not _bool(claims.get("email_verified")):
		raise ApiError(
			403,
			"email_not_verified",
			"Apple didn't share a verified email for this account. Use another way to sign in.",
		)
	email = email.strip().lower()
	return {
		"provider": "apple",
		"id": claims["sub"],
		"email": email,
		"name": clean_name(name),
		"sub": claims["sub"],
		"emailVerified": True,
		"privateEmail": _bool(claims.get("is_private_email")) or email.endswith(RELAY_SUFFIX),
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
