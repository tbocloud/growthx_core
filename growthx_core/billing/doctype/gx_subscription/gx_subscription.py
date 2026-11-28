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


def account_status_from_subscriptions(account: str) -> str:
	"""The account's status as its subscriptions say: the best one's, churned when all are canceled."""
	statuses = set(frappe.get_all("GX Subscription", {"account": account}, pluck="status"))
	status = "churned" if statuses else "trial"
	for sub_status, account_status in ACCOUNT_STATUS:
		if sub_status in statuses:
			status = account_status
			break
	return status


def sync_account_status(account: str):
	status = account_status_from_subscriptions(account)
	frappe.db.set_value("GX Account", account, "status", status, update_modified=False)


def sync_account_status_after_cancel(account: str):
	"""After subscriptions were canceled outside a save (admin.terminate_tenant, the renewal job's archive):
	as sync_account_status, but an account an admin suspended stays suspended while a live subscription is
	left. An admin's Suspend (payments.set_customer_status) suspends the account and its sites and leaves the
	subscriptions as they are; only Reactivate, allowed only from `suspended`, brings those sites back."""
	status = account_status_from_subscriptions(account)
	if status != "churned" and frappe.db.get_value("GX Account", account, "status") == "suspended":
		return
	frappe.db.set_value("GX Account", account, "status", status, update_modified=False)
