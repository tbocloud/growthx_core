"""Creating customer and partner accounts (used by `auth.signup` and the seed script)."""

import frappe
from frappe.utils import cint, now_datetime

from growthx_core.partners import referrals
from growthx_core.provisioning import runner
from growthx_core.setup.defaults import default_link

CUSTOMER_ROLE = "GrowthX Customer"
PARTNER_ROLE = "GrowthX Partner"


def create_user(email: str, full_name: str, password: str, role: str, phone: str | None = None) -> str:
	first, _, last = full_name.partition(" ")
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


def create_customer_account(
	*,
	email: str,
	full_name: str,
	company: str,
	password: str,
	phone: str | None = None,
	plan=None,
	referral_code: str | None = None,
) -> tuple[str, str | None]:
	"""Returns (user, tenant_name or None). The caller commits; provisioning starts after commit."""
	user = create_user(email, full_name, password, CUSTOMER_ROLE, phone)
	partner = referrals.resolve(referral_code, email)

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

	if not plan:
		return user, None

	# OI-2 is open (trial from sign-up, or from when the site is ready). Until the Lead decides,
	# the trial starts at sign-up.
	subscription = frappe.get_doc(
		{
			"doctype": "GX Subscription",
			"account": account.name,
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
			"account": account.name,
			"product": plan.product,
			"subscription": subscription.name,
			"display_name": company,
			"subdomain": runner.make_subdomain(company),
			"status": "requested",
			"status_message": "Preparing your site.",
		}
	)
	runner.init_steps(tenant)
	tenant.insert(ignore_permissions=True)
	subscription.db_set("tenant", tenant.name, update_modified=False)

	runner.enqueue(tenant.name)
	return user, tenant.name


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
