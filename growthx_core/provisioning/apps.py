"""Which Frappe apps go on a customer's site.

Admins manage it in the desk:
- GX Product → Apps: the default apps for every new site of that product (e.g. Solar:
  growthx_theme, solar). Add or remove rows to change what new sites get.
- GX Tenant → Apps: copied from the product when the site is requested. Change it for one
  customer before the site is created (or before a retry) to give that site different apps.

frappe and erpnext are always installed first and are not listed. growthx_theme can't be removed:
it carries the "Open my app" sign-in. Every app must also be in the product's Press release group.
"""

import re

import frappe
from frappe import _

BASE_APPS = ("frappe", "erpnext")
THEME_APP = "growthx_theme"
THEME_REPO = "https://github.com/tbocloud/growthx_theme.git"
APP_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


def validate_app_rows(doc, label: str):
	"""Clean and check `doc.apps`. Adds growthx_theme to an empty table; refuses a table without it."""
	if not doc.get("apps"):
		doc.append("apps", {"app_name": THEME_APP, "repository": THEME_REPO})

	seen = set()
	for row in doc.apps:
		row.app_name = (row.app_name or "").strip().lower()
		row.repository = (row.repository or "").strip() or None
		if not APP_NAME.match(row.app_name):
			frappe.throw(
				_(
					"Row {0}: {1} is not a valid app name. Use the app's name, e.g. solar, not its repository URL."
				).format(row.idx, frappe.bold(row.app_name or _("(empty)")))
			)
		if row.app_name in BASE_APPS:
			frappe.throw(
				_("Row {0}: {1} is always installed; remove it from the list.").format(row.idx, row.app_name)
			)
		if row.app_name in seen:
			frappe.throw(_("Row {0}: {1} is listed twice.").format(row.idx, row.app_name))
		seen.add(row.app_name)

	if THEME_APP not in seen:
		frappe.throw(
			_('{0} needs {1}: it carries the "Open my app" sign-in on every customer site.').format(
				label, frappe.bold(THEME_APP)
			)
		)


def copy_product_apps(tenant):
	"""Give a new tenant its product's apps (only if it has none yet)."""
	if tenant.get("apps") or not tenant.product:
		return
	for row in frappe.get_all(
		"GX Product App",
		filters={"parenttype": "GX Product", "parent": tenant.product},
		fields=["app_name", "repository"],
		order_by="idx",
	):
		tenant.append("apps", row)


def site_apps(tenant) -> list[str]:
	"""Every app to install on the tenant's site, in order: frappe, erpnext, then its Apps table
	(or its product's, for tenants created before the table existed)."""
	names = [row.app_name for row in tenant.get("apps") or []]
	if not names:
		names = frappe.get_all(
			"GX Product App",
			filters={"parenttype": "GX Product", "parent": tenant.product},
			pluck="app_name",
			order_by="idx",
		) or [THEME_APP]
	return [*BASE_APPS, *[name for name in names if name not in BASE_APPS]]


def all_listed_apps() -> set[str]:
	"""Every app any product or tenant lists (for the stand-in Press clients)."""
	return set(frappe.get_all("GX Product App", pluck="app_name", distinct=True))
