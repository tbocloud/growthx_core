"""Move tenants created before single sign-on onto it.

    bench --site <core-site> execute growthx_core.provisioning.backfill.enable_sso
    bench --site <core-site> execute growthx_core.provisioning.backfill.enable_sso --kwargs "{'tenant': 'GXT-00004'}"

For each ready tenant: install growthx_theme, write a new tenant key and the Core URL into the
tenant's site_config, replace the owner's (shared) password with a random one nobody knows,
and enable "Login with Email Link". Then remove `growthx_tenant_login_path` from this site's
config, so "Open my app" uses growthx_theme's sso.login again. Safe to run more than once.
"""

import frappe
from frappe.installer import update_site_config

from growthx_core.provisioning import runner
from growthx_core.provisioning.press import PressError, get_client


def enable_sso(tenant: str | None = None):
	client = get_client()
	filters = {"status": "ready", "press_site_id": ("is", "set")}
	if tenant:
		filters["name"] = tenant
	done, failed = [], []

	for name in frappe.get_all("GX Tenant", filters=filters, pluck="name", order_by="creation asc"):
		doc = frappe.get_doc("GX Tenant", name)
		try:
			client.install_app(doc.press_site_id, "growthx_theme")
			client.configure_site(doc.press_site_id, runner.tenant_settings(doc, reset_owner_password=True))
			frappe.db.commit()
			done.append(f"{name} ({doc.press_site_id})")
		except (PressError, NotImplementedError) as e:
			frappe.db.rollback()
			failed.append(f"{name} ({doc.press_site_id}): {e}")

	if frappe.conf.get("growthx_tenant_login_path"):
		update_site_config("growthx_tenant_login_path", "None")
		print("Removed growthx_tenant_login_path: Open my app now uses growthx_theme.sso.login.")

	print(f"Single sign-on enabled on {len(done)} tenant(s).")
	for line in done:
		print(f"  ok      {line}")
	for line in failed:
		print(f"  FAILED  {line}")
	return {"done": done, "failed": failed}
