"""Email verification at sign-up: `auth.send_signup_code` emails a 6-digit code, and `auth.signup`
checks it (not needed with a Google sign-up token: Google has verified the email).

A code lasts 10 minutes and allows 5 wrong tries; a new code replaces the old one. Only a hash of the
code is kept (in Redis). The code is used up when the account is created, not when it's checked, so
a form error (e.g. a taken subdomain) doesn't cost the user a new code.

`growthx_signup_email_code: 0` in site_config turns the check off, for the time between deploying
Core and the website that sends codes. Leave it unset otherwise.
"""

import json
import secrets
import time
from datetime import timedelta

import frappe
from frappe.utils import cint, now_datetime, sha256_hash

from growthx_core.api.v1._core import ApiError, validation_error

CODE_TTL = 10 * 60
RESEND_AFTER = 60
MAX_WRONG = 5


def required() -> bool:
	return bool(cint(frappe.conf.get("growthx_signup_email_code", 1)))


def _key(email: str) -> str:
	return f"growthx:signup_code:{sha256_hash(email)}"


def _hash(email: str, code: str) -> str:
	return sha256_hash(f"{email}:{code}")


def _get(email: str) -> dict | None:
	value = frappe.cache.get_value(_key(email))
	entry = json.loads(value) if value else None
	if entry and entry["expires"] > time.time():
		return entry
	return None


def _put(email: str, entry: dict):
	frappe.cache.set_value(
		_key(email), json.dumps(entry), expires_in_sec=max(int(entry["expires"] - time.time()), 1)
	)


def send(email: str) -> dict:
	"""Email a new code. Returns {expiresAt, resendAfter} as datetimes."""
	now = time.time()
	previous = _get(email)
	if previous and now < previous["sent"] + RESEND_AFTER:
		wait = int(previous["sent"] + RESEND_AFTER - now) + 1
		raise ApiError(
			429,
			"rate_limited",
			f"Please wait {wait} seconds before asking for a new code.",
			headers={"Retry-After": wait},
		)

	code = f"{secrets.randbelow(10**6):06d}"
	entry = {"hash": _hash(email, code), "sent": now, "expires": now + CODE_TTL, "wrong": 0}
	try:
		frappe.sendmail(
			recipients=[email],
			subject="Your GrowthX verification code",
			message=(
				f"<p>Your GrowthX verification code is:</p><p style='font-size:24px'><b>{code}</b></p>"
				"<p>It expires in 10 minutes. If you didn't ask for it, you can ignore this email.</p>"
			),
			now=True,
		)
	except Exception:
		frappe.log_error(title="GrowthX: could not send sign-up code")
		raise ApiError(503, "email_unavailable", "We couldn't send the email. Please try again shortly.")
	_put(email, entry)
	sent_at = now_datetime()  # in the site's timezone, like every date the API returns
	return {
		"expiresAt": sent_at + timedelta(seconds=CODE_TTL),
		"resendAfter": sent_at + timedelta(seconds=RESEND_AFTER),
	}


def check(email: str, code: str):
	"""Raise unless `code` is the email's current code. Wrong: 422 (fields.emailCode). Expired,
	never sent, or 5 wrong tries: 400 email_code_expired."""
	expired = ApiError(400, "email_code_expired", "This code has expired. Please ask for a new one.")
	entry = _get(email)
	if not entry or entry["wrong"] >= MAX_WRONG:
		raise expired
	if secrets.compare_digest(entry["hash"], _hash(email, code)):
		return
	entry["wrong"] += 1
	_put(email, entry)
	if entry["wrong"] >= MAX_WRONG:
		raise expired
	left = MAX_WRONG - entry["wrong"]
	raise validation_error(
		{"emailCode": f"That code isn't right. {left} {'try' if left == 1 else 'tries'} left."}
	)


def use(email: str):
	"""Once the account exists."""
	frappe.cache.delete_value(_key(email))
