"""ERPNext defaults GrowthX needs, which exist only after ERPNext's setup wizard has run.

A Core site created by Press (or CI) may not have run the wizard, so creating an ERPNext Customer
or Sales Partner would fail with "Could not find Territory: All Territories". These helpers find a
usable (non-group) value, creating the standard root and a GrowthX leaf under it if there is none.
"""

import frappe

# doctype: (Selling Settings field, name field, parent field, root group, leaf created if none exists)
TREES = {
	"Territory": ("territory", "territory_name", "parent_territory", "All Territories", "India"),
	"Customer Group": (
		"customer_group",
		"customer_group_name",
		"parent_customer_group",
		"All Customer Groups",
		"GrowthX Customers",
	),
}


def default_link(doctype: str) -> str:
	"""A non-group record (ERPNext won't accept a group on a Customer): Selling Settings' default,
	else any existing leaf, else a GrowthX leaf created under the root."""
	settings_field, name_field, parent_field, _, leaf = TREES[doctype]
	value = frappe.db.get_single_value("Selling Settings", settings_field)
	if value and frappe.db.get_value(doctype, value, "is_group") == 0:
		return value
	existing = frappe.db.get_value(doctype, {"is_group": 0}, "name", order_by="lft asc")
	if existing:
		return existing
	frappe.get_doc(
		{"doctype": doctype, name_field: leaf, parent_field: ensure_root(doctype), "is_group": 0}
	).insert(ignore_permissions=True)
	return leaf


def ensure_root(doctype: str) -> str:
	_, name_field, _, root, _ = TREES[doctype]
	if not frappe.db.exists(doctype, root):
		frappe.get_doc({"doctype": doctype, name_field: root, "is_group": 1}).insert(ignore_permissions=True)
	return root


def ensure_all():
	for doctype in TREES:
		default_link(doctype)
