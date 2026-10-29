// Copyright (c) 2026, TBO GrowthX and contributors
// For license information, please see license.txt

frappe.ui.form.on("GX Tenant", {
	refresh(frm) {
		// A failed site (e.g. a test sign-up) keeps health.provisioning at 503 until it's archived.
		if (frm.doc.status !== "failed" || frm.is_new()) return;
		frm.add_custom_button(__("Archive site"), () => {
			frappe.confirm(
				__("Archive this failed site? Its Press site, if any, is backed up and deleted."),
				() =>
					frm
						.call({ method: "archive_failed_site", doc: frm.doc, freeze: true })
						.then(() => frm.reload_doc()),
			);
		});
	},
});
