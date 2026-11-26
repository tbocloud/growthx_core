"""Renewals: reminders, grace and suspension for GrowthX and ERPNext Direct subscriptions."""

import frappe

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
