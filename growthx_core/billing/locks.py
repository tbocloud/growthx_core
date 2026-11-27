"""The GX Account lock for requests: admin actions and payments that change an account's billing or sites."""

import sys

import frappe

AFTER_A_WRITE = "GrowthX: lock_account after a write"


def lock_account(name: str):
	"""Lock the GX Account `name` in a fresh transaction and return it (loaded after the lock).

	Why: MariaDB (REPEATABLE READ) keeps a transaction's snapshot from its first plain read. A request that
	waits for this lock (held by the renewal job through Press calls, or by a payment) would otherwise read
	the account's sites, subscriptions, checkouts and invoices as they were before it waited. So the
	transaction is committed first (nothing is pending: it only ends the old snapshot), and the lock is the
	new transaction's first read: every read after it, plain ones too, sees what others committed meanwhile.
	(Like the renewal job's checkpoints, this relies on after_commit callbacks not reading the database.)

	Rule: call it before the request's first write and before any other lock, since a commit releases every
	lock held. That is why PayU's `_activate` (it locks the GX Checkout first, inside the Payment Entry's
	on_submit) locks the account itself and must not use this.
	- After a write (`frappe.db.transaction_writes`) it doesn't commit: it logs the caller to the Error Log
	  and takes the lock in the current transaction, as before. Raising instead could break every admin
	  action and payment if the framework ever wrote before the handler.
	- In tests it never commits (that would keep the test's records)."""
	if not frappe.flags.in_test:
		if frappe.db.transaction_writes:
			caller = sys._getframe(1)
			frappe.log_error(
				title=AFTER_A_WRITE,
				message=(
					f"lock_account({name}) was called by {caller.f_globals.get('__name__')}."
					f"{caller.f_code.co_name} after {frappe.db.transaction_writes} write(s) in this"
					" transaction. It took the lock without committing, so reads after it may come from the"
					" request's older snapshot. Move the call before the first write."
				),
				reference_doctype="GX Account",
				reference_name=name,
			)
		else:
			frappe.db.commit()  # nothing to commit: this only ends the old snapshot
	frappe.db.get_value("GX Account", name, "name", for_update=True)
	return frappe.get_doc("GX Account", name)
