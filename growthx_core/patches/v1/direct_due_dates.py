"""ERPNext Direct customers added before due dates were stored: a never-paid `past_due` subscription with
no due date and no period end gets the day it was created as its due date, so the renewal job and Admin →
Renewals see when its first payment was due (as the job already assumed)."""

import frappe
from frappe.utils import getdate

from growthx_core.api.v1._core import audit


def execute():
	sub, tenant = frappe.qb.DocType("GX Subscription"), frappe.qb.DocType("GX Tenant")
	rows = (
		frappe.qb.from_(sub)
		.join(tenant)
		.on(tenant.name == sub.tenant)
		.select(sub.name, sub.creation)
		.where(tenant.origin == "imported")
		.where(sub.status == "past_due")
		.where(sub.due_date.isnull())
		.where(sub.current_period_end.isnull())
		.run(as_dict=True)
	)
	for row in rows:
		due = getdate(row.creation)
		frappe.db.set_value("GX Subscription", row.name, "due_date", due, update_modified=False)
		audit(
			"renewal.due_date", "GX Subscription", row.name, f"due date set to {due} (the day it was added)"
		)
