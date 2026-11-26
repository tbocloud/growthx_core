"""ERPNext Direct: an ERPNext site that already runs on our Press, added by the team in the admin panel
(admin.add_direct_site) so the customer can pay for it through GrowthX.

Nothing is installed on or pulled from such a site. Its GX Tenant has `origin = imported` and no
`press_site_id` on purpose: every Press action Core takes on a tenant (archive, suspend, resume, domains)
goes through `press_site_id`, so without it Core can't touch the customer's site.
"""

import re
from urllib.parse import urlsplit

import frappe
from frappe.utils import sha256_hash

from growthx_core import accounts
from growthx_core.api.v1._core import ApiError, validation_error, web_url
from growthx_core.provisioning import press, runner

PRODUCT_PREFIX = "erpnext-direct"
HOST_RE = re.compile(r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


def is_direct_product(product: str | None) -> bool:
	"""`erpnext-direct`, or a product named `erpnext-direct-...` for another kind of direct deal."""
	return bool(product) and (product == PRODUCT_PREFIX or product.startswith(PRODUCT_PREFIX + "-"))


def normalize_site(value) -> str:
	"""A typed site address as a bare lowercase host name: `https://Acme.Example.com/app#x` -> acme.example.com."""
	text = (value or "").strip().lower() if isinstance(value, str) else ""
	if "://" not in text:
		text = "//" + text
	try:
		host = urlsplit(text).hostname or ""
	except ValueError:
		host = ""
	host = host.rstrip(".")
	if not HOST_RE.match(host) or host.endswith((".local", ".localhost", ".internal")):
		raise validation_error({"site": "Enter the site's domain, like acme.example.com."})
	own = {frappe.local.site, urlsplit(web_url()).hostname, urlsplit(frappe.utils.get_url()).hostname}
	if host in own:
		raise validation_error({"site": "That is a GrowthX address, not a customer's site."})
	return host


def site_taken(host: str) -> str | None:
	"""The live GX Tenant that already has this address (site address, Press site or custom domain)."""
	url = f"https://{host}"
	for filters in ({"site_url": url}, {"press_site_id": host}, {"custom_domain": host}):
		name = frappe.db.get_value("GX Tenant", {**filters, "status": ("!=", "archived")}, "name")
		if name:
			return name
	return None


def check_on_press(host: str) -> str:
	"""One exact-name lookup (never a listing): `confirmed`, `not_found`, `unreachable` (Press didn't answer)
	or `skipped` (this Core has no real Press to ask)."""
	client = runner.get_client()
	try:
		return "confirmed" if client.site_exists(host) else "not_found"
	except NotImplementedError:
		return "skipped"
	except press.PressError:
		frappe.log_error(title=f"GrowthX: Press couldn't confirm {host}", defer_insert=True)
		return "unreachable"


def require_assigned_plan(account_name: str, plan):
	"""A direct customer pays the plan the team assigned, never another one of the product (a cheaper
	plan, a test plan) and not after their billing was terminated. Plan changes are admin.assign_plan."""
	if not is_direct_product(plan.product):
		return
	sub = frappe.db.get_value(
		"GX Subscription",
		{"account": account_name, "product": plan.product},
		["plan", "status"],
		as_dict=True,
		order_by="creation desc",
	)
	if not sub:
		raise validation_error({"planId": "You have no subscription for this plan."})
	if sub.status == "canceled":
		raise ApiError(409, "invalid_transition", "Your billing has ended. Please contact GrowthX.")
	if sub.plan != plan.name:
		raise ApiError(
			422,
			"invalid_plan",
			"Your plan is set by GrowthX. Please contact us to change it.",
			fields={"planId": "Not your plan."},
		)


# ---------------------------------------------------------------- emails


def issue_password_link(user: str) -> str:
	"""A set-password link, through the same single-use key `auth.forgot_password` makes (stored hashed;
	`auth.reset_password` consumes it, 24 hours)."""
	key = frappe.generate_hash()
	frappe.db.set_value(
		"User",
		user,
		{
			"reset_password_key": sha256_hash(key),
			"last_reset_password_key_generated_on": frappe.utils.now_datetime(),
		},
	)
	return web_url(f"/reset-password?key={key}")


def send_email(email: str, subject: str, html: str) -> bool:
	"""Send a billing email now. False if it could not be sent (the Error Log has the cause)."""
	try:
		frappe.sendmail(recipients=[email], subject=subject, message=html, now=True)
		return True
	except Exception:
		# e.g. no outgoing email account: the admin sees `emailSent: false`, the renewal job retries next run.
		frappe.log_error(title="GrowthX: could not send a billing email")
		return False


_send = send_email  # the earlier name


def send_invite(account, site: str, plan, user: str) -> bool:
	esc = frappe.utils.escape_html
	link = issue_password_link(user)
	return send_email(
		account.contact_email,
		"Pay for your ERPNext site on GrowthX",
		f"<p>Hello {esc(account.account_name)},</p>"
		f"<p>Your ERPNext site <b>{esc(site)}</b> is now billed through GrowthX"
		f" ({esc(plan.plan_name)}).</p>"
		f'<p><a href="{esc(link)}">Set your password</a> to sign in to your GrowthX dashboard and pay securely'
		" with PayU. The link works once and expires in 24 hours; after that, use"
		f' <a href="{esc(web_url("/forgot-password"))}">Forgot password</a>.</p>'
		f"<p>Sign in at {esc(web_url('/login'))} with {esc(account.contact_email)}.</p>",
	)


def send_payment_link(account, site: str, plan, user: str, with_password: bool) -> bool:
	esc = frappe.utils.escape_html
	billing = web_url("/customer/billing")
	html = (
		f"<p>Hello {esc(account.account_name)},</p>"
		f"<p>Your payment for <b>{esc(site)}</b> ({esc(plan.plan_name)}) is due.</p>"
		f'<p><a href="{esc(billing)}">Open your billing page</a>, sign in and press <b>Pay now</b>.'
		" You pay securely on PayU with a card, UPI or net banking.</p>"
	)
	if with_password:
		html += (
			f'<p>You have not set a password yet: <a href="{esc(issue_password_link(user))}">set it here</a>'
			" (the link works once and expires in 24 hours).</p>"
		)
	return send_email(account.contact_email, "Your GrowthX payment link", html)


def random_password() -> str:
	return frappe.generate_hash(length=40) + "aA1!"


def new_customer(*, email: str, contact_name: str, company: str, phone: str | None) -> tuple[str, str]:
	"""A customer who has no password yet: (user, GX Account). They set one from the invite email."""
	user, _ = accounts.create_customer_account(
		email=email, full_name=contact_name, company=company, password=random_password(), phone=phone
	)
	accounts.mark_no_password(user)
	return user, frappe.db.get_value("GX Account", {"user": user}, "name")


def existing_customer(email: str) -> str | None:
	"""The GX Account of an existing customer user with this email, or None if there is no such user.
	Anything else with that email (a partner, an admin, a disabled or closed account) is refused: a
	direct customer must never be attached to someone else's login."""
	user = frappe.db.get_value("User", {"email": email}, ["name", "enabled"], as_dict=True)
	if not user or accounts.reusable_user(email):
		return None  # new, or a closed account's email: new_customer makes or revives it
	account = frappe.db.get_value("GX Account", {"user": user.name}, ["name", "status"], as_dict=True)
	if (
		not user.enabled
		or not account
		or account.status == "churned"
		or accounts.CUSTOMER_ROLE not in frappe.get_roles(user.name)
	):
		raise ApiError(
			409,
			"email_in_use",
			"That email already belongs to another kind of GrowthX user or a closed account.",
			fields={"email": "Use a different email."},
		)
	return account.name
