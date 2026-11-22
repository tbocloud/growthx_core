"""Creating customer and partner accounts (used by `auth.signup` and the seed script)."""

import frappe
from frappe.utils import cint, now_datetime

from growthx_core.partners import referrals
from growthx_core.provisioning import runner
from growthx_core.setup.defaults import default_link

CUSTOMER_ROLE = "GrowthX Customer"
PARTNER_ROLE = "GrowthX Partner"


def reusable_user(email: str) -> str | None:
	"""The user a deleted account left behind: disabled, and every account it has is `churned`.
	Deleting an account blocks signing in, but does not keep the email for good, so the same
	address may sign up again."""
	user = frappe.db.get_value("User", {"email": email}, ["name", "enabled"], as_dict=True)
	if not user or user.enabled:
		return None
	statuses = frappe.get_all("GX Account", filters={"user": user.name}, pluck="status")
	if not statuses or any(status != "churned" for status in statuses):
		return None
	return user.name


def live_site(account: str, product: str) -> str | None:
	"""The account's site for `product` that isn't archived, or None."""
	return frappe.db.get_value(
		"GX Tenant", {"account": account, "product": product, "status": ("!=", "archived")}, "name"
	)


def create_user(email: str, full_name: str, password: str, role: str, phone: str | None = None) -> str:
	first, _, last = full_name.partition(" ")
	existing = reusable_user(email)
	if existing:
		user = frappe.get_doc("User", existing)
		user.update(
			{
				"first_name": first,
				"last_name": last or None,
				"mobile_no": phone,
				"user_type": "Website User",
				"enabled": 1,
			}
		)
		user.new_password = password
		user.append_roles(role)
		user.flags.no_welcome_mail = True
		user.save(ignore_permissions=True)
		return user.name
	user = frappe.get_doc(
		{
			"doctype": "User",
			"email": email,
			"first_name": first,
			"last_name": last or None,
			"mobile_no": phone,
			"user_type": "Website User",
			"send_welcome_email": 0,
			"new_password": password,
			"roles": [{"role": role}],
		}
	)
	user.insert(ignore_permissions=True)
	return user.name


GOOGLE = "google"
MICROSOFT = "microsoft"
GITHUB = "github"
SOCIAL_PROVIDERS = (GOOGLE, MICROSOFT, GITHUB)
ADMIN_ROLE = "GrowthX Admin"


def link_social(user: str, provider: str, userid: str):
	"""Record that the user signs in with a provider (Frappe's User Social Login), for the welcome email."""
	if frappe.db.exists("User Social Login", {"parent": user, "provider": provider}):
		return
	frappe.get_doc(
		{
			"doctype": "User Social Login",
			"parent": user,
			"parenttype": "User",
			"parentfield": "social_logins",
			"provider": provider,
			"userid": userid,
		}
	).insert(ignore_permissions=True)


def link_google(user: str, google_id: str):
	"""Record that the user signs in with Google."""
	link_social(user, GOOGLE, google_id)


def link_microsoft(user: str, microsoft_id: str):
	"""Record that the user signs in with Microsoft (the back office's Entra ID)."""
	link_social(user, MICROSOFT, microsoft_id)


def link_github(user: str, github_id: str):
	"""Record that the user signs in with GitHub."""
	link_social(user, GITHUB, github_id)


def user_by_social(provider: str, userid: str) -> str | None:
	"""The user who signs in with this provider account (its stable id), or None."""
	if not userid:
		return None
	parent = frappe.db.get_value(
		"User Social Login", {"provider": provider, "userid": userid, "parenttype": "User"}, "parent"
	)
	return parent if parent and frappe.db.exists("User", parent) else None


def social_providers(user: str) -> list[str]:
	"""The providers (google, microsoft, github) the user can sign in with, in a fixed order."""
	linked = set(frappe.get_all("User Social Login", filters={"parent": user}, pluck="provider"))
	return [p for p in SOCIAL_PROVIDERS if p in linked]


def unlink_social(user: str, provider: str):
	frappe.db.delete("User Social Login", {"parent": user, "provider": provider, "parenttype": "User"})


# A Google sign-up without a password gets a random one nobody knows. Kept as a user default
# (no DocType change) until the user sets a password ("Forgot password" or change_password).
NO_PASSWORD = "growthx_no_password"


def mark_no_password(user: str):
	frappe.db.set_default(NO_PASSWORD, "1", parent=user)


def mark_has_password(user: str):
	frappe.db.delete("DefaultValue", {"parent": user, "defkey": NO_PASSWORD})


def has_password(user: str) -> bool:
	return not frappe.db.get_value("DefaultValue", {"parent": user, "defkey": NO_PASSWORD})


def signs_in_with_google(user: str | None) -> bool:
	return bool(user and frappe.db.exists("User Social Login", {"parent": user, "provider": GOOGLE}))


def create_customer_account(
	*,
	email: str,
	full_name: str,
	company: str,
	password: str,
	phone: str | None = None,
	plan=None,
	referral_code: str | None = None,
	subdomain: str | None = None,
	country: str | None = None,
	chart_of_accounts: str | None = None,
) -> tuple[str, str | None]:
	"""Returns (user, tenant_name or None). The caller commits; provisioning starts after commit.

	`subdomain` is the one the customer chose (already checked); without it, one is made from
	the company name. `country` is a Country name; site set-up follows it (see countries.py)."""
	user = create_user(email, full_name, password, CUSTOMER_ROLE, phone)
	partner = referrals.resolve(referral_code, email)

	# A deleted account signing up again: `user` keeps its one GX Account (the link is unique), so
	# reset it to a fresh trial on the same Customer instead of inserting a second one.
	reviving = frappe.db.get_value("GX Account", {"user": user}, ["name", "customer"], as_dict=True)
	if reviving:
		customer = reviving.customer
		if customer and frappe.db.exists("Customer", customer):
			frappe.db.set_value("Customer", customer, {"email_id": email, "mobile_no": phone})
		account = frappe.get_doc("GX Account", reviving.name)
		account.update(
			{
				"account_name": company,
				"contact_email": email,
				"phone": phone,
				"referred_by": partner.name if partner else None,
				"referral_code": referral_code.strip().upper() if partner and referral_code else None,
				"referred_at": now_datetime() if partner else None,
				"status": "trial",
			}
		)
		account.save(ignore_permissions=True)
	else:
		customer = frappe.get_doc(
			{
				"doctype": "Customer",
				"customer_name": company,
				"customer_type": "Company",
				"customer_group": default_link("Customer Group"),
				"territory": default_link("Territory"),
				"email_id": email,
				"mobile_no": phone,
			}
		)
		if partner and partner.sales_partner:
			# Sales Invoices for this customer then carry the partner, so ERPNext computes commission.
			customer.default_sales_partner = partner.sales_partner
			customer.default_commission_rate = frappe.db.get_value(
				"Sales Partner", partner.sales_partner, "commission_rate"
			)
		customer.insert(ignore_permissions=True)

		account = frappe.get_doc(
			{
				"doctype": "GX Account",
				"account_name": company,
				"user": user,
				"contact_email": email,
				"phone": phone,
				"customer": customer.name,
				"referred_by": partner.name if partner else None,
				"referral_code": referral_code.strip().upper() if partner and referral_code else None,
				"referred_at": now_datetime() if partner else None,
				"status": "trial",
			}
		).insert(ignore_permissions=True)

	if account.referred_by:
		_notify_partner(account)

	if not plan:
		return user, None

	# Signing up again: the account already has this app's site, so bring that one back instead of
	# creating a second site the customer never asked for.
	if reviving and (existing := live_site(account.name, plan.product)):
		return user, existing

	tenant = start_site(
		account=account.name,
		plan=plan,
		display_name=company,
		subdomain=subdomain,
		country=country,
		chart_of_accounts=chart_of_accounts,
	)
	return user, tenant


def start_site(
	*,
	account: str,
	plan,
	display_name: str | None = None,
	subdomain: str | None = None,
	country: str | None = None,
	chart_of_accounts: str | None = None,
) -> str:
	"""Start a site for `account` on `plan`: its trial subscription, the tenant and the provisioning
	job. Returns the tenant name; the caller commits (`customer.add_site` for an app an existing
	customer adds to their account)."""
	existing = live_site(account, plan.product)
	if existing:
		frappe.throw(frappe._("This account already has a {0} site ({1}).").format(plan.product, existing))
	name = display_name or frappe.db.get_value("GX Account", account, "account_name")
	# OI-2 is open (trial from sign-up, or from when the site is ready). Until the Lead decides,
	# the trial starts at sign-up.
	subscription = frappe.get_doc(
		{
			"doctype": "GX Subscription",
			"account": account,
			"plan": plan.name,
			"status": "trialing",
			"price": plan.price,
			"gst_included": plan.gst_included,
			"gst_rate": plan.gst_rate,
			"trial_ends_at": frappe.utils.add_days(now_datetime(), cint(plan.trial_days)),
		}
	).insert(ignore_permissions=True)

	tenant = frappe.get_doc(
		{
			"doctype": "GX Tenant",
			"account": account,
			"product": plan.product,
			"subscription": subscription.name,
			"display_name": name,
			"subdomain": runner.site_subdomain(subdomain) if subdomain else runner.make_subdomain(name),
			"country": country,
			"chart_of_accounts": chart_of_accounts,
			"status": "requested",
			"status_message": "Preparing your site.",
		}
	)
	runner.init_steps(tenant)
	tenant.insert(ignore_permissions=True)
	subscription.db_set("tenant", tenant.name, update_modified=False)

	runner.enqueue(tenant.name)
	return tenant.name


def _notify_partner(account):
	"""Tell the referring partner. Never fails the customer's sign-up: on error, undo only the
	notification and log it."""
	from growthx_core.partners.notifications import on_customer_referred

	frappe.db.savepoint("gx_notify_partner")
	try:
		on_customer_referred(account)
	except Exception:
		frappe.db.rollback(save_point="gx_notify_partner")
		frappe.log_error(title=f"GrowthX: could not notify the partner of {account.name}")


def create_partner_account(
	*, email: str, full_name: str, password: str, phone: str | None = None, company: str | None = None
) -> str:
	user = create_user(email, full_name, password, PARTNER_ROLE, phone)
	frappe.get_doc(
		{
			"doctype": "GX Partner Profile",
			"partner_name": full_name,
			"user": user,
			"phone": phone,
			"company": company,
			"referral_code": referrals.generate_code(full_name),
			"status": "pending_approval",
		}
	).insert(ignore_permissions=True)
	return user
