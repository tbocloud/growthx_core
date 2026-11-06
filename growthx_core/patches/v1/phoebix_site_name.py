"""PhoebiX is spelt with a capital X, like PrintagX, WillpeX and BillaX. Fix the seeded site row, but
only if editors haven't renamed it themselves."""

import frappe


def execute():
	if not frappe.db.table_exists("GX Content Site") or not frappe.db.exists("GX Content Site", "phoebix"):
		return  # a fresh site: after_migrate seeds it with the right name
	row = frappe.db.get_value("GX Content Site", "phoebix", ["site_name", "title_template"], as_dict=True)
	if row.site_name == "Phoebix":
		frappe.db.set_value("GX Content Site", "phoebix", "site_name", "PhoebiX")
	if row.title_template == "%s · Phoebix":
		frappe.db.set_value("GX Content Site", "phoebix", "title_template", "%s · PhoebiX")
