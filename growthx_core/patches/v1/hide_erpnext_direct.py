"""ERPNext Direct isn't sold on the website: it is the customer's own ERPNext site, added by the
team in Core so GrowthX can bill it. Hide it from the public catalogue. The seed leaves
`availability` to admins after the first run, so an existing row still says `coming_soon`."""

import frappe

PRODUCT = "erpnext-direct"


def execute():
	if not frappe.db.table_exists("GX Product") or not frappe.db.exists("GX Product", PRODUCT):
		return
	if frappe.db.get_value("GX Product", PRODUCT, "availability") != "hidden":
		frappe.db.set_value("GX Product", PRODUCT, "availability", "hidden")
