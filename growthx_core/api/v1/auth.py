from datetime import timedelta

import frappe
from frappe.core.doctype.user.user import test_password_strength
from frappe.utils import cint, now_datetime, sha256_hash
from frappe.utils.password import check_password, update_password

from growthx_core import accounts, tokens
from growthx_core.api.v1 import _serializers as s
from growthx_core.api.v1._core import (
	ApiError,
	RateLimit,
	body,
	current_user,
	endpoint,
	get_choice,
	get_email,
	get_str,
	ok,
	validation_error,
	web_url,
)

GROWTHX_ROLES = {"GrowthX Customer", "GrowthX Partner", "GrowthX Admin"}
MIN_PASSWORD_LENGTH = 8
RESET_KEY_TTL = timedelta(hours=24)


def _client(data) -> str:
	return get_choice(data, "client", ("web", "mobile"), default="web")


def _password(data, key="password") -> str:
	value = data.get(key)
	if not isinstance(value, str) or not value:
		raise validation_error({key: "Password is required."})
	if len(value) < MIN_PASSWORD_LENGTH:
		raise validation_error({key: f"Password must be at least {MIN_PASSWORD_LENGTH} characters."})
	if len(value) > 128:
		raise validation_error({key: "Password must be at most 128 characters."})
	return value


def _check_password_policy(password: str, key: str, user_inputs: tuple):
	"""Apply the site's password policy (System Settings), reporting it on the form field."""
	result = test_password_strength(password, user_data=user_inputs)
	feedback = (result or {}).get("feedback") or {}
	if result and not feedback.get("password_policy_validation_passed"):
		hint = feedback.get("warning") or " ".join(feedback.get("suggestions") or [])
		raise validation_error({key: (hint or "This password is too weak.").strip()})


def _growthx_user(email: str) -> str | None:
	"""The enabled user with this email who has a GrowthX role, or None."""
	user = frappe.db.get_value("User", {"email": email, "enabled": 1}, "name")
	if user and GROWTHX_ROLES & set(frappe.get_roles(user)):
		return user
	return None


@endpoint(
	methods=("POST",),
	auth=False,
	rate_limit=(RateLimit(20, 15 * 60), RateLimit(5, 15 * 60, by_field="email")),
)
def login():
	data = body()
	email = get_email(data)
	password = data.get("password")
	if not isinstance(password, str) or not password:
		raise validation_error({"password": "Password is required."})

	user = _growthx_user(email)
	try:
		if not user:
			raise frappe.AuthenticationError
		check_password(user, password)
	except frappe.AuthenticationError:
		raise ApiError(401, "invalid_credentials", "The email or password is incorrect.")

	pair = tokens.issue(user, _client(data))
	return s.auth_session(pair, user)


@endpoint(methods=("POST",), auth=False, rate_limit=RateLimit(10, 60 * 60), idempotent=True)
def signup():
	data = body()
	account_type = get_choice(data, "accountType", ("customer", "partner"), required=True)
	name = get_str(data, "name", required=True, label="Name")
	email = get_email(data)
	password = _password(data)
	phone = get_str(data, "phone", max_length=20, label="Phone")
	company = get_str(data, "company", required=account_type == "customer", label="Company")
	client = _client(data)
	_check_password_policy(password, "password", (name, email, company))

	if frappe.db.exists("User", {"email": email}) or frappe.db.exists("User", email):
		raise ApiError(
			409,
			"email_taken",
			"An account with this email already exists.",
			fields={"email": "An account with this email already exists."},
		)

	tenant = None
	if account_type == "customer":
		plan = _signup_plan(data)
		user, tenant = accounts.create_customer_account(
			email=email,
			full_name=name,
			company=company,
			password=password,
			phone=phone,
			plan=plan,
			referral_code=get_str(data, "referralCode", max_length=40),
		)
	else:
		user = accounts.create_partner_account(
			email=email, full_name=name, password=password, phone=phone, company=company
		)

	result = s.auth_session(tokens.issue(user, client), user)
	if tenant:
		result["site"] = {"id": tenant, "status": frappe.db.get_value("GX Tenant", tenant, "status")}
	return 201, result


def _signup_plan(data):
	"""The plan to start a trial on, or None if the customer didn't pick a product."""
	product_slug = get_str(data, "productSlug", max_length=40)
	plan_id = get_str(data, "planId", max_length=140)
	interval = get_choice(data, "billingInterval", ("month", "year"))
	if not product_slug and not plan_id:
		return None

	if plan_id:
		plan = frappe.db.get_value("GX Plan", {"name": plan_id, "is_active": 1}, "*", as_dict=True)
		if not plan:
			raise validation_error({"planId": "Unknown plan."})
		if product_slug and plan.product != product_slug:
			raise validation_error({"planId": "This plan is not for the selected product."})
		if interval and plan.billing_interval != interval:
			raise validation_error({"billingInterval": "Doesn't match the plan's billing interval."})
		product_slug = plan.product
	else:
		plan = None

	availability = frappe.db.get_value("GX Product", product_slug, "availability")
	if not availability:
		raise validation_error({"productSlug": "Unknown product."})
	if availability != "available":
		raise validation_error({"productSlug": "This product is not available yet."})

	if not plan:
		filters = {"product": product_slug, "is_active": 1}
		if interval:
			filters["billing_interval"] = interval
		plan = frappe.db.get_value(
			"GX Plan", filters, "*", as_dict=True, order_by="highlighted desc, sort_order asc, price asc"
		)
		if not plan:
			raise validation_error({"productSlug": "This product has no plans yet."})
	return frappe._dict(plan)


@endpoint(methods=("POST",), auth=False, rate_limit=RateLimit(60, 15 * 60))
def refresh():
	data = body()
	refresh_token = data.get("refreshToken")
	pair = tokens.rotate(refresh_token if isinstance(refresh_token, str) else "")
	if not pair:
		raise ApiError(401, "invalid_token", "Your session has expired. Please log in again.")
	return s.auth_session(pair, pair["_user"])


@endpoint(methods=("POST",))
def logout():
	tokens.revoke(frappe.local.gx_token)
	return ok()


@endpoint(methods=("GET",))
def me():
	return s.user(current_user())


@endpoint(
	methods=("POST",),
	auth=False,
	rate_limit=(RateLimit(10, 15 * 60), RateLimit(3, 15 * 60, by_field="email")),
)
def forgot_password():
	email = get_email(body())
	user = _growthx_user(email)
	if user:
		key = frappe.generate_hash()
		frappe.db.set_value(
			"User",
			user,
			{"reset_password_key": sha256_hash(key), "last_reset_password_key_generated_on": now_datetime()},
		)
		link = web_url(f"/reset-password?key={key}")
		frappe.sendmail(
			recipients=[email],
			subject="Reset your GrowthX password",
			message=(
				"<p>We received a request to reset your GrowthX password.</p>"
				f'<p><a href="{link}">Choose a new password</a>. The link works once and expires in 24 hours.</p>'
				"<p>If you didn't ask for this, you can ignore this email.</p>"
			),
		)
	# Same response whether or not the account exists.
	return ok(202)


@endpoint(methods=("POST",), auth=False, rate_limit=RateLimit(10, 15 * 60))
def reset_password():
	data = body()
	key = get_str(data, "key", required=True, max_length=200, label="Reset key")
	new_password = _password(data, "newPassword")

	row = frappe.db.get_value(
		"User",
		{"reset_password_key": sha256_hash(key), "enabled": 1},
		["name", "last_reset_password_key_generated_on"],
		as_dict=True,
	)
	expiry = (
		timedelta(seconds=cint(frappe.get_system_settings("reset_password_link_expiry_duration")))
		or RESET_KEY_TTL
	)
	if (
		not row
		or not row.last_reset_password_key_generated_on
		or now_datetime() > row.last_reset_password_key_generated_on + expiry
	):
		raise ApiError(400, "invalid_reset_key", "This reset link is invalid or has expired.")

	user_inputs = frappe.db.get_value("User", row.name, ["first_name", "last_name", "email"])
	_check_password_policy(new_password, "newPassword", tuple(user_inputs or ()))
	update_password(row.name, new_password)
	frappe.db.set_value("User", row.name, "reset_password_key", "")
	tokens.revoke_all(row.name)
	return ok()
