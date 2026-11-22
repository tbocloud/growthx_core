"""Sign in with GitHub (web): the Next.js server sends GitHub's authorization code to `auth.github`,
and Core exchanges it with GitHub for the user's id and primary verified email.

Only the primary email GitHub has verified is used (`GET /user/emails`, scope `read:user user:email`);
the numeric GitHub id is the stable id. GitHub never grants admin.

site_config:
- `github_oauth_client_id`, `github_oauth_client_secret`: the OAuth app (set by DevOps).
- `github_oauth_redirect_uri`: optional; default `<growthx_web_url>/api/auth/github/callback`. It
  must be the exact URI the website sent the user to GitHub with.
"""

import frappe
import requests

from growthx_core.api.v1._core import ApiError, web_url

TOKEN_URL = "https://github.com/login/oauth/access_token"
USER_URL = "https://api.github.com/user"
EMAILS_URL = "https://api.github.com/user/emails"
CALLBACK_PATH = "/api/auth/github/callback"
NOREPLY_SUFFIX = "@users.noreply.github.com"


def _failed():
	return ApiError(400, "github_failed", "GitHub sign-in failed. Please try again.")


def _unavailable():
	return ApiError(503, "github_unavailable", "GitHub sign-in isn't available right now.")


def redirect_uri() -> str:
	return frappe.conf.get("github_oauth_redirect_uri") or web_url(CALLBACK_PATH)


def _json(response):
	try:
		value = response.json()
	except ValueError:
		return None
	return value


def verified_identity(code: str, code_verifier: str | None = None) -> dict:
	"""Exchange the code with GitHub. Returns {provider, id, email, name, sub, emailVerified} for the
	primary verified email."""
	client_id = frappe.conf.get("github_oauth_client_id")
	client_secret = frappe.conf.get("github_oauth_client_secret")
	if not client_id or not client_secret:
		raise _unavailable()

	data = {
		"code": code,
		"client_id": client_id,
		"client_secret": client_secret,
		"redirect_uri": redirect_uri(),
	}
	if code_verifier:
		data["code_verifier"] = code_verifier
	try:
		response = requests.post(TOKEN_URL, data=data, headers={"Accept": "application/json"}, timeout=10)
	except requests.RequestException:
		frappe.log_error(title="GitHub sign-in: token endpoint unreachable")
		raise _unavailable()
	body = _json(response) if response.status_code == 200 else None
	# GitHub answers 200 with {"error": "bad_verification_code"} for a used or expired code.
	if not isinstance(body, dict) or body.get("error") or not isinstance(body.get("access_token"), str):
		error = body.get("error") if isinstance(body, dict) else response.status_code
		if error != "bad_verification_code":
			frappe.log_error(title=f"GitHub sign-in: {error}")
		raise _failed()

	headers = {
		"Authorization": f"Bearer {body['access_token']}",
		"Accept": "application/vnd.github+json",
		"X-GitHub-Api-Version": "2022-11-28",
	}
	profile = _get(USER_URL, headers)
	emails = _get(EMAILS_URL, headers)
	if not isinstance(profile, dict) or not profile.get("id") or not isinstance(emails, list):
		raise _failed()
	email = next(
		(
			e["email"]
			for e in emails
			if isinstance(e, dict)
			and e.get("primary") is True
			and e.get("verified") is True
			and isinstance(e.get("email"), str)
		),
		None,
	)
	if not email or email.lower().endswith(NOREPLY_SUFFIX):
		raise ApiError(
			403,
			"email_not_verified",
			"Your GitHub account has no verified primary email. Verify one on GitHub and try again.",
		)
	github_id = str(profile["id"])
	return {
		"provider": "github",
		"id": github_id,
		"email": email.strip().lower(),
		"name": (profile.get("name") or profile.get("login") or "").strip(),
		"sub": github_id,
		"emailVerified": True,
	}


def _get(url: str, headers: dict):
	try:
		response = requests.get(url, headers=headers, timeout=10)
	except requests.RequestException:
		frappe.log_error(title="GitHub sign-in: API unreachable")
		raise _unavailable()
	if response.status_code != 200:
		raise _failed()
	return _json(response)
