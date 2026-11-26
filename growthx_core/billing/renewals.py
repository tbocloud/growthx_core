"""Renewals: reminders, grace and suspension for GrowthX and ERPNext Direct subscriptions."""

import frappe
from frappe.utils import add_days, cint, now_datetime, nowdate

from growthx_core.api.v1._core import audit
from growthx_core.provisioning import runner


def resume_subscription_site(sub_name: str) -> None:
	"""Bring the subscription's site back after a payment. A GrowthX (provisioned) site is resumed on
	Press; an ERPNext Direct (imported) site is the customer's own and only changes status here. If
	Press refuses, the error is logged and the tenant stays suspended."""
	tenant_name = frappe.db.get_value("GX Subscription", sub_name, "tenant")
	if not tenant_name:
		return
	tenant = frappe.db.get_value(
		"GX Tenant", tenant_name, ["name", "status", "origin", "press_site_id"], as_dict=True
	)
	if not tenant or tenant.status != "suspended":
		return
	if tenant.origin != "imported" and tenant.press_site_id:
		try:
			runner.get_client().resume_site(tenant.press_site_id)
		except Exception:
			frappe.log_error(title=f"GrowthX: resume_site failed for {tenant.name}")
			return
	frappe.db.set_value("GX Tenant", tenant.name, "status", "ready")
	audit("renewal.resume", "GX Subscription", sub_name, f"site {tenant.name} resumed after payment")


def never_paid(account: str) -> bool:
	"""True if none of the account's subscriptions has ever recorded a payment."""
	paid = frappe.get_all("GX Subscription", {"account": account}, pluck="last_payment_id")
	return not any(paid)


def suspend_subscription(sub_name: str, by: str = "billing") -> bool:
	"""Suspend the subscription's site. A GrowthX (provisioned) site is suspended on Press first and
	Core changes only if that succeeds; an ERPNext Direct (imported) site is the customer's own, so
	only status changes here. A never-paid GrowthX trial also gets an archive date. Returns False if
	Press refused (logged, nothing changed)."""
	sub = frappe.db.get_value("GX Subscription", sub_name, ["name", "account", "tenant"], as_dict=True)
	if not sub or not sub.tenant:
		return False
	tenant = frappe.db.get_value("GX Tenant", sub.tenant, ["name", "origin", "press_site_id"], as_dict=True)
	if not tenant:
		return False
	imported = tenant.origin == "imported"
	if not imported and tenant.press_site_id:
		try:
			runner.get_client().suspend_site(tenant.press_site_id)
		except Exception:
			frappe.log_error(title=f"GrowthX: suspend_site failed for {tenant.name}")
			return False
	values = {"status": "suspended", "suspended_at": now_datetime(), "suspended_by": by}
	if not imported and never_paid(sub.account):
		days = cint(frappe.db.get_single_value("GX Billing Settings", "trial_archive_after_days"))
		values["archive_due_on"] = add_days(nowdate(), days)
	frappe.db.set_value("GX Tenant", tenant.name, "status", "suspended")
	frappe.db.set_value("GX Subscription", sub_name, values)
	audit("renewal.suspend", "GX Subscription", sub_name, f"site {tenant.name} suspended ({by})")
	return True


def archive_trial(sub_name: str) -> bool:
	"""Back up and archive a never-paid GrowthX trial's site and cancel the subscription. Refuses
	(False) for an ERPNext Direct tenant and for any account that has ever paid."""
	sub = frappe.db.get_value("GX Subscription", sub_name, ["name", "account", "tenant"], as_dict=True)
	if not sub or not sub.tenant:
		return False
	origin = frappe.db.get_value("GX Tenant", sub.tenant, "origin")
	if origin == "imported" or not never_paid(sub.account):
		return False
	runner.archive(sub.tenant)
	frappe.db.set_value("GX Subscription", sub_name, "status", "canceled")
	audit("renewal.archive", "GX Subscription", sub_name, f"site {sub.tenant} archived")
	return True
