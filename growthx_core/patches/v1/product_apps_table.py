"""GX Product: move the old single `vertical_app` field into the new Apps table, and give
existing tenants their product's apps."""

import frappe

from growthx_core.provisioning.apps import THEME_APP, THEME_REPO

# The old field held Solar's repository name; Press needs the app's name (solar_operations installs as "solar").
REPO_TO_APP = {"solar_operations": ("solar", "https://github.com/tbocloud/solar_operations.git")}


def execute():
	has_old = frappe.db.has_column("GX Product", "vertical_app")
	for name in frappe.get_all("GX Product", pluck="name"):
		if frappe.db.exists("GX Product App", {"parenttype": "GX Product", "parent": name}):
			continue
		doc = frappe.get_doc("GX Product", name)
		doc.append("apps", {"app_name": THEME_APP, "repository": THEME_REPO})
		old = frappe.db.get_value("GX Product", name, "vertical_app") if has_old else None
		if old:
			app, repo = REPO_TO_APP.get(old, (old, None))
			doc.append("apps", {"app_name": app, "repository": repo})
		doc.save(ignore_permissions=True)

	# Tenants created before the table existed: copy their product's apps.
	for name in frappe.get_all("GX Tenant", pluck="name"):
		if frappe.db.exists("GX Product App", {"parenttype": "GX Tenant", "parent": name}):
			continue
		tenant = frappe.get_doc("GX Tenant", name)
		for row in frappe.get_all(
			"GX Product App",
			filters={"parenttype": "GX Product", "parent": tenant.product},
			fields=["app_name", "repository"],
			order_by="idx",
		):
			tenant.append("apps", row)
		if tenant.get("apps"):
			tenant.flags.ignore_validate = True
			tenant.save(ignore_permissions=True)
