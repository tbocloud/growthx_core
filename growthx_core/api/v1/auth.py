import re
from datetime import timedelta
from urllib.parse import urlsplit

import frappe
from frappe.core.doctype.user.user import test_password_strength
from frappe.utils import cint, now_datetime, sha256_hash
from frappe.utils.password import check_password, update_password

from growthx_core import (
	accounts,
	apple_signin,
	github_signin,
	google_signin,
	microsoft_signin,
	signup_codes,
	social_signup,
	tokens,
)
from growthx_core.api.v1 import _serializers as s
from growthx_core.api.v1._core import (
	ApiError,
	RateLimit,
	audit,
	body,
	content_portal_login,
	current_user,
	endpoint,
	get_choice,
	get_email,
	get_role,
	get_str,
	iso,
	ok,
	query,
	validation_error,
	web_url,
)
from growthx_core.provisioning import countries, custom_domains, runner
from growthx_core.provisioning import site_login as site_login_tokens

GROWTHX_ROLES = {"GrowthX Customer", "GrowthX Partner", "GrowthX Admin"}
CONTENT_EDITOR_ROLE = "GrowthX Content Editor"
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


def _growthx_user(email: str, editors: bool = False) -> str | None:
	"""The enabled user with this email who has a GrowthX role, or None. With `editors`, content
	editors too while portal sign-in is on (password sign-in and reset only, not Google)."""
	user = frappe.db.get_value("User", {"email": email, "enabled": 1}, "name")
	return _with_growthx_role(user, editors)


def _with_growthx_role(user: str | None, editors: bool = False) -> str | None:
	if not user or not frappe.db.get_value("User", user, "enabled"):
		return None
	roles = set(frappe.get_roles(user))
	if GROWTHX_ROLES & roles:
		return user
	if editors and CONTENT_EDITOR_ROLE in roles and content_portal_login():
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

	user = _growthx_user(email, editors=True)
	try:
		if not user:
			raise frappe.AuthenticationError
		check_password(user, password)
	except frappe.AuthenticationError:
		raise ApiError(401, "invalid_credentials", "The email or password is incorrect.")

	pair = tokens.issue(user, _client(data))
	return s.auth_session(pair, user)


def _social_login(identity: dict, data):
	"""Sign in, or start a sign-up, with a verified social identity (Google, Microsoft customers,
	GitHub). The provider account is found by its stable id first; a verified email then matches an
	existing GrowthX user. An unverified email never matches (that would be an account takeover),
	and `never_admin` providers can't open an admin account."""
	# An identity from an older caller or test may lack the provider fields: it is Google's.
	identity = {"provider": "google", "id": identity.get("sub"), "emailVerified": True, **identity}
	provider = identity["provider"]
	sign_in_only = data.get("signInOnly") is True
	verified = identity.get("emailVerified") is True
	linked = accounts.user_by_social(provider, identity["id"])

	if linked:
		user = _with_growthx_role(linked)
	else:
		user = _growthx_user(identity["email"]) if verified else None
	if user:
		if provider != "google" and accounts.ADMIN_ROLE in frappe.get_roles(user):
			# Microsoft customers and GitHub never open the admin panel; staff use the staff path.
			raise ApiError(403, "account_unavailable", "This account can't sign in here.")
		if not linked:
			accounts.link_social(user, provider, identity["id"])
		return s.auth_session(tokens.issue(user, _client(data)), user)

	if linked or _email_taken(identity["email"]):
		if not verified and not linked and _growthx_user(identity["email"]):
			raise ApiError(
				409,
				"email_unverified",
				f"There's already an account with this email, but {provider.title()} hasn't verified it. "
				f"Sign in another way, then link {provider.title()} from Account settings.",
			)
		# A disabled account, or a staff account without a GrowthX role.
		raise ApiError(403, "account_unavailable", "This account can't sign in here.")
	if sign_in_only:
		raise ApiError(404, "no_account", f"There's no GrowthX account for this {provider.title()} account.")
	return 202, {
		"signupRequired": True,
		"signupToken": social_signup.issue(identity),
		"expiresAt": iso(now_datetime() + timedelta(seconds=social_signup.SIGNUP_TTL)),
		"email": identity["email"],
		"name": identity["name"],
		"provider": provider,
		"emailVerified": verified,
	}


def _oauth_input(data) -> tuple[str, str | None]:
	code = get_str(data, "code", required=True, max_length=2048, label="Code")
	return code, get_str(data, "codeVerifier", max_length=128, label="Code verifier")


def _apple_extras(data) -> dict:
	return {
		"nonce": get_str(data, "nonce", max_length=128, label="Nonce"),
		"name": get_str(data, "name", max_length=300, label="Name"),
	}


@endpoint(methods=("POST",), auth=False, rate_limit=RateLimit(20, 15 * 60))
def google():
	"""Sign in with Google: an existing account is logged in (200); a new email gets a sign-up
	token for `auth.signup` (202), or `404 no_account` with `signInOnly` (signing in to a site)."""
	data = body()
	identity = google_signin.verified_identity(*_oauth_input(data))
	return _social_login(identity, data)


@endpoint(methods=("POST",), auth=False, rate_limit=RateLimit(20, 15 * 60))
def github():
	"""Sign in or sign up with GitHub, like Google, with the primary verified email. Never admin."""
	data = body()
	identity = github_signin.verified_identity(*_oauth_input(data))
	return _social_login(identity, data)


@endpoint(methods=("POST",), auth=False, rate_limit=RateLimit(20, 15 * 60))
def apple():
	"""Sign in or sign up with Apple, like Google. `nonce` is the one the website sent Apple, `name` the
	one Apple posts only on the first authorization. Never admin; a private-relay email is an ordinary
	email that matches an account only if identical."""
	data = body()
	identity = apple_signin.verified_identity(*_oauth_input(data), **_apple_extras(data))
	return _social_login(identity, data)


@endpoint(methods=("POST",), auth=False, rate_limit=RateLimit(20, 15 * 60))
def microsoft():
	"""Sign in with Microsoft (one multi-tenant app). A member of the staff directory is logged in as
	a GrowthX Admin (created on first sign-in); anyone else signs in or signs up as a customer."""
	data = body()
	identity = microsoft_signin.verified_identity(*_oauth_input(data))
	if identity["staff"]:
		return _microsoft_staff(identity, data)
	return _social_login(identity, data)


def _microsoft_staff(identity: dict, data):
	email = identity["email"]
	name = frappe.db.get_value("User", {"email": email}, "name")
	if name:
		if not frappe.db.get_value("User", name, "enabled"):
			# Disabled in GrowthX stays blocked, whatever the directory says.
			raise ApiError(403, "account_unavailable", "This account can't sign in here.")
		if accounts.ADMIN_ROLE not in frappe.get_roles(name):
			doc = frappe.get_doc("User", name)
			doc.append_roles(accounts.ADMIN_ROLE)
			doc.flags.no_welcome_mail = True
			doc.save(ignore_permissions=True)
			audit("staff.grant_admin", "User", name, details=email, actor=name)
	else:
		first, _, last = (identity["name"] or email.split("@")[0]).partition(" ")
		doc = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": first,
				"last_name": last or None,
				"user_type": "Website User",
				"send_welcome_email": 0,
				"new_password": frappe.generate_hash(length=32),  # nobody knows it
				"roles": [{"role": accounts.ADMIN_ROLE}],
			}
		)
		doc.flags.no_welcome_mail = True
		doc.insert(ignore_permissions=True)
		name = doc.name
		accounts.mark_no_password(name)
		audit("staff.auto_provision", "User", name, details=email, actor=name)
	accounts.link_social(name, accounts.MICROSOFT, identity["id"])
	return s.auth_session(tokens.issue(name, _client(data)), name)


@endpoint(
	methods=("POST",),
	auth=False,
	rate_limit=(RateLimit(10, 60 * 60), RateLimit(5, 60 * 60, by_field="email")),
)
def send_signup_code():
	"""Email a 6-digit code to verify the address before `auth.signup` (not needed with Google)."""
	data = body()
	email = get_email(data)
	_client(data)
	if _email_taken(email):
		raise _email_taken_error()
	sent = signup_codes.send(email)
	return 202, {"expiresAt": iso(sent["expiresAt"]), "resendAfter": iso(sent["resendAfter"])}


def _email_taken(email: str) -> bool:
	# An email a deleted account left behind (disabled user, only churned accounts) is free again.
	if accounts.reusable_user(email):
		return False
	return bool(frappe.db.exists("User", {"email": email}) or frappe.db.exists("User", email))


def _email_taken_error():
	return ApiError(
		409,
		"email_taken",
		"An account with this email already exists.",
		fields={"email": "An account with this email already exists."},
	)


@endpoint(methods=("POST",), auth=False, rate_limit=RateLimit(10, 60 * 60), idempotent=True)
def signup():
	data = body()
	account_type = get_choice(data, "accountType", ("customer", "partner"), required=True)
	# `socialToken` (any provider); `googleSignupToken` is the same token under its old name.
	social_token = get_str(data, "socialToken", max_length=100) or get_str(
		data, "googleSignupToken", max_length=100
	)
	social = _social_signup(social_token) if social_token else None
	name = get_str(data, "name", required=not social, label="Name") or social["name"]
	if not name:
		raise validation_error({"name": "Name is required."})
	email = social["email"] if social else get_email(data)
	# With a social sign-up there's no password; the user can set one later with "Forgot password".
	password = _password(data) if not social or data.get("password") else frappe.generate_hash(length=32)
	phone = get_str(data, "phone", max_length=20, label="Phone")
	company = get_str(data, "company", required=account_type == "customer", label="Company")
	if company and any(c in company for c in "<>"):
		# it's the name on the customer's invoices until they set a billing name, which refuses these too
		raise validation_error({"company": "Company can't contain < or >."})
	client = _client(data)
	site = _signup_site(data) if account_type == "customer" else {}
	if not social or data.get("password"):
		_check_password_policy(password, "password", (name, email, company))

	if _email_taken(email):
		raise _email_taken_error()
	# The provider has verified a social sign-up's email (a Microsoft work account may not have: then
	# the emailed code proves it); everyone else proves it with the emailed code.
	check_code = (not social or not social["emailVerified"]) and signup_codes.required()
	if check_code:
		code = get_str(data, "emailCode", required=True, max_length=6, label="Code")
		if not (len(code) == 6 and code.isdigit()):
			raise validation_error({"emailCode": "Enter the 6-digit code from the email."})
		signup_codes.check(email, code)

	tenant = None
	if account_type == "customer":
		plan = _signup_plan(data)
		# Last, because it calls Press (10 name checks a minute for the whole team).
		if plan and site["subdomain"] and runner.press_name_taken(site["subdomain"]):
			raise _subdomain_taken()
	referral_code = get_str(data, "referralCode", max_length=40)
	try:
		if account_type == "customer":
			user, tenant = accounts.create_customer_account(
				email=email,
				full_name=name,
				company=company,
				password=password,
				phone=phone,
				plan=plan,
				referral_code=referral_code,
				**site,
			)
		else:
			user = accounts.create_partner_account(
				email=email, full_name=name, password=password, phone=phone, company=company
			)
	except frappe.ValidationError as e:
		if isinstance(e, frappe.UniqueValidationError) and site.get("subdomain"):
			raise _subdomain_taken()  # someone else signed up with it a moment ago
		# Everything the visitor can fix was checked above; this is Core's setup (e.g. ERPNext
		# defaults). A 500 with a reference, logged, instead of ERPNext's text as a 422.
		raise AccountSetupError(f"Creating the {account_type} account failed") from e

	if social_token:
		accounts.link_social(user, social["provider"], social["id"])
		if not data.get("password"):
			accounts.mark_no_password(user)
		social_signup.use(social_token)
	if check_code:
		signup_codes.use(email)
	result = s.auth_session(tokens.issue(user, client), user)
	if tenant:
		result["site"] = {"id": tenant, "status": frappe.db.get_value("GX Tenant", tenant, "status")}
	return 201, result


class AccountSetupError(Exception):
	"""Creating an account failed for a reason the visitor can't fix."""


def _social_signup(token: str) -> dict:
	identity = social_signup.identity(token)
	if not identity:
		raise ApiError(
			400,
			"google_signup_expired",  # the code predates Microsoft and GitHub; the web client matches it
			"Your sign-in has expired. Please continue with your account again.",
		)
	return identity


SUBDOMAIN_MESSAGES = {
	"invalid": "Use {min}-{max} lowercase letters, numbers and hyphens, starting and ending with a letter or number.",
	"reserved": "This address is reserved. Please choose another.",
	"taken": "This address is already taken.",
}


def subdomain_message(problem: str) -> str:
	return SUBDOMAIN_MESSAGES[problem].format(min=5, max=runner.longest_subdomain())


def _subdomain_taken():
	message = subdomain_message("taken")
	return ApiError(409, "subdomain_taken", message, fields={"subdomain": message})


def _signup_site(data) -> dict:
	"""A customer's choices for their site (docs 09 §2). All optional: without them the site is
	set up for India, with a subdomain made from the company name."""
	code = get_choice(data, "country", tuple(countries.COUNTRIES), default=countries.DEFAULT)
	country = countries.COUNTRIES[code]
	chart = get_str(data, "chartOfAccounts", label="Chart of accounts")
	if chart and chart not in countries.charts(code):
		raise validation_error({"chartOfAccounts": "This chart of accounts isn't available for the country."})

	subdomain = get_str(data, "subdomain", max_length=64, label="Subdomain")
	if subdomain:
		subdomain = subdomain.lower()
		problem = runner.subdomain_problem(subdomain)
		if problem == "taken":
			raise _subdomain_taken()
		if problem:
			raise validation_error({"subdomain": subdomain_message(problem)})
	return {
		"subdomain": subdomain,
		"country": country.country,
		"chart_of_accounts": chart or country.default_chart,
	}


def _signup_plan(data):
	"""The plan to start a trial on, or None if the customer didn't pick a product."""
	product_slug = get_str(data, "productSlug", max_length=40)
	plan_id = get_str(data, "planId", max_length=140)
	interval = get_choice(data, "billingInterval", ("month", "year", "one_time"))
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
	if get_role(pair["_user"]) == "content_editor" and not content_portal_login():
		tokens.revoke_all(pair["_user"])
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
	user = _growthx_user(email, editors=True)
	if user:
		key = frappe.generate_hash()
		frappe.db.set_value(
			"User",
			user,
			{"reset_password_key": sha256_hash(key), "last_reset_password_key_generated_on": now_datetime()},
		)
		link = web_url(f"/reset-password?key={key}")
		try:
			frappe.sendmail(
				recipients=[email],
				subject="Reset your GrowthX password",
				now=True,  # at once, not via the queue: the user is waiting for it
				message=(
					"<p>We received a request to reset your GrowthX password.</p>"
					f'<p><a href="{link}">Choose a new password</a>. The link works once and expires in 24 hours.</p>'
					"<p>If you didn't ask for this, you can ignore this email.</p>"
				),
			)
		except Exception:
			# e.g. no outgoing email account. The client still gets 202: failing here would reveal
			# that the account exists. Admins see the failure in the Error Log.
			frappe.log_error(title="GrowthX: could not send password-reset email")
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
	accounts.mark_has_password(row.name)
	tokens.revoke_all(row.name)
	return ok()


# ---------------------------------------------------------------- account settings

NEW_PASSWORD_MIN = 10


def _new_password(data, key="newPassword") -> str:
	"""Account settings rule (the website's sign-up rule): 10+ characters with a letter and a number."""
	value = data.get(key)
	if not isinstance(value, str) or not value:
		raise validation_error({key: "Password is required."})
	if len(value) < NEW_PASSWORD_MIN or len(value) > 128:
		raise validation_error({key: f"Use {NEW_PASSWORD_MIN} to 128 characters."})
	if not (any(c.isalpha() for c in value) and any(c.isdigit() for c in value)):
		raise validation_error({key: "Use at least one letter and one number."})
	return value


@endpoint(methods=("POST",), rate_limit=RateLimit(5, 15 * 60, by_user=True))
def change_password():
	"""New password for the logged-in user. Every other session is logged out; this device gets
	new tokens."""
	data = body()
	user = current_user()
	current = data.get("currentPassword")
	if not accounts.has_password(user):
		raise ApiError(
			409,
			"no_password",
			"Your account signs in with Google and has no password yet. Use Forgot password to set one.",
		)
	if not isinstance(current, str) or not current:
		raise validation_error({"currentPassword": "Enter your current password."})
	new_password = _new_password(data)
	try:
		check_password(user, current)
	except frappe.AuthenticationError:
		raise ApiError(
			422,
			"wrong_password",
			"Your current password is incorrect.",
			fields={"currentPassword": "Your current password is incorrect."},
		)
	if new_password == current:
		raise validation_error({"newPassword": "Choose a password you haven't been using."})
	user_inputs = frappe.db.get_value("User", user, ["first_name", "last_name", "email"])
	_check_password_policy(new_password, "newPassword", tuple(user_inputs or ()))

	client = frappe.db.get_value("GX Auth Token", frappe.local.gx_token, "client") or "web"
	update_password(user, new_password)
	accounts.mark_has_password(user)
	tokens.revoke_all(user)
	return s.auth_session(tokens.issue(user, client), user)


@endpoint(methods=("POST",))
def logout_others():
	"""Log out every other device: revoke all the user's tokens except the one making this call."""
	revoked = tokens.revoke_all(current_user(), keep=frappe.local.gx_token)
	return {"ok": True, "revoked": revoked}


# Linked sign-in providers (Account settings). `user.socialLogins` lists them.

VERIFIERS = {
	accounts.GOOGLE: google_signin,
	accounts.MICROSOFT: microsoft_signin,
	accounts.GITHUB: github_signin,
	accounts.APPLE: apple_signin,
}


@endpoint(methods=("POST",), rate_limit=RateLimit(10, 15 * 60, by_user=True))
def link_provider():
	"""Link Google, Microsoft, GitHub or Apple to the logged-in account: the same `code` flow as signing in,
	sent with the user's own token. The provider account must not belong to another user."""
	data = body()
	provider = get_choice(data, "provider", tuple(VERIFIERS), required=True)
	user = current_user()
	extras = _apple_extras(data) if provider == accounts.APPLE else {}
	identity = VERIFIERS[provider].verified_identity(*_oauth_input(data), **extras)
	if provider == accounts.MICROSOFT and identity["staff"]:
		raise ApiError(409, "not_linkable", "Staff Microsoft accounts sign in with the staff login.")
	owner = accounts.user_by_social(provider, identity["id"])
	if owner and owner != user:
		raise ApiError(409, "already_linked", "This account is already linked to another GrowthX user.")
	if provider in accounts.social_providers(user) and not owner:
		raise ApiError(409, "provider_linked", f"A different {provider.title()} account is already linked.")
	accounts.link_social(user, provider, identity["id"])
	audit("auth.link_provider", "User", user, details=provider)
	return s.user(user)


@endpoint(methods=("POST",), rate_limit=RateLimit(10, 15 * 60, by_user=True))
def unlink_provider():
	"""Unlink a provider. The last way to sign in can't be removed: without a password, at least one
	provider must stay."""
	provider = get_choice(body(), "provider", tuple(VERIFIERS), required=True)
	user = current_user()
	linked = accounts.social_providers(user)
	if provider not in linked:
		raise ApiError(404, "not_linked", f"{provider.title()} isn't linked to your account.")
	if len(linked) == 1 and not accounts.has_password(user):
		raise ApiError(
			409,
			"last_sign_in_method",
			f"{provider.title()} is the only way you sign in. Set a password first (Forgot password).",
		)
	accounts.unlink_social(user, provider)
	audit("auth.unlink_provider", "User", user, details=provider)
	return s.user(user)


# ---------------------------------------------------------------- "Sign in with GrowthX" on a site
# The button on a site's login page goes to <website>/login?site=<host>. The website shows the
# site's name (site_info), the person signs in to GrowthX, presses "Continue", and site_login
# returns the same one-time link as "Open my app". Core only ever links to the site address in
# its own records, never to one from the request.

SIGN_IN_STATUSES = ("ready", "suspended")
HOST = re.compile(r"^[a-z0-9.-]{1,253}$")


def _site_by_host(host: str | None):
	"""The tenant whose address (or active custom domain) is `host`, or 404 site_not_found."""
	host = (host or "").strip().lower().rstrip(".")
	host = urlsplit(host if "//" in host else f"//{host}").hostname or ""
	missing = ApiError(404, "site_not_found", "We couldn't find this site.")
	if not HOST.match(host):
		raise missing
	rows = frappe.get_all(
		"GX Tenant",
		filters={"status": ("in", SIGN_IN_STATUSES)},
		or_filters={"site_url": ("like", f"%//{host}%"), "custom_domain": host},
		fields=["name", "site_url", "custom_domain", "custom_domain_status"],
	)
	for row in rows:
		if urlsplit(row.site_url or "").hostname == host or (
			row.custom_domain == host and row.custom_domain_status == "active"
		):
			return frappe.get_doc("GX Tenant", row.name)
	raise missing


def _site_info(tenant) -> dict:
	url = custom_domains.public_url(tenant)
	name = tenant.display_name or frappe.db.get_value("GX Account", tenant.account, "account_name")
	return {"siteName": name or urlsplit(url).hostname, "siteUrl": url, "loginUrl": f"{url}/login"}


@endpoint(methods=("GET",), auth=False, rate_limit=RateLimit(60, 60))
def site_info():
	return _site_info(_site_by_host(get_str(query(), "site", required=True, max_length=300)))


@endpoint(methods=("POST",), rate_limit=RateLimit(20, 60, by_user=True))
def site_login():
	tenant = _site_by_host(get_str(body(), "site", required=True, max_length=300))
	info = _site_info(tenant)
	if frappe.db.get_value("GX Account", tenant.account, "user") != current_user():
		raise ApiError(
			403,
			"not_linked",
			f"Your GrowthX account isn't linked to {info['siteName']}. If you work there, sign in with "
			"the site's own login.",
			extra={"loginUrl": info["loginUrl"]},
		)
	if tenant.status != "ready":
		raise ApiError(409, "site_unavailable", f"{info['siteName']} isn't available right now.")
	token = site_login_tokens.issue(tenant.name, current_user())
	audit("site_login", "GX Tenant", tenant.name)  # never log the token
	return {"redirectUrl": site_login_tokens.login_url(info["siteUrl"], token)}
