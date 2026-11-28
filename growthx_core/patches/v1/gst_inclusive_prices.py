"""Every plan price includes GST: set gst_included on the GX Plan and GX Subscription rows still
priced "+ GST". Prices stay as they are. Plain updates (no saves, no hooks, `modified` kept); a second
run finds nothing to change."""

import frappe


def execute():
	for doctype in ("GX Plan", "GX Subscription"):
		frappe.db.set_value(doctype, {"gst_included": 0}, "gst_included", 1, update_modified=False)
