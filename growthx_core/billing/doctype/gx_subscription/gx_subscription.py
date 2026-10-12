# Copyright (c) 2026, TBO GrowthX and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

# Best status first: an account with any active subscription is "active".
ACCOUNT_STATUS = [
	("active", "active"),
	("past_due", "active"),
	("trialing", "trial"),
	("suspended", "suspended"),
]


class GXSubscription(Document):
	def on_update(self):
		sync_account_status(self.account)


def sync_account_status(account: str):
	statuses = set(frappe.get_all("GX Subscription", {"account": account}, pluck="status"))
	status = "churned" if statuses else "trial"
	for sub_status, account_status in ACCOUNT_STATUS:
		if sub_status in statuses:
			status = account_status
			break
	frappe.db.set_value("GX Account", account, "status", status, update_modified=False)
