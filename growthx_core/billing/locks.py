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
	- `transaction_writes` is reset to 0 by `rollback to savepoint`, even with earlier writes still pending:
	  never call this after a savepoint rollback that follows a write, or it would commit those writes.
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


def account_rows(doctype: str, scope: dict, fields: list[str], *, fresh: bool) -> list:
	"""The rows of `doctype` in `scope` (one account's or one tenant's), with `name` and `fields`, as they are
	now. The caller holds the account lock.
	- `fresh`: it took the lock with lock_account, so one plain read is current.
	- Otherwise (PayU's `_activate`, which locks its checkout first): its snapshot can predate what others
	  committed while it waited for the lock, so each row is read again with a lock, by name. A locking read
	  returns the latest committed row. By name (the primary key) it locks only those rows: `account` and
	  `tenant` have no index, and a locking read filtered on them would lock the whole table. Which rows are in
	  `scope` comes from the plain read: rows never move between accounts or sites."""
	rows = frappe.get_all(doctype, scope, ["name", *fields], order_by="creation asc")
	if fresh:
		return rows
	latest = (
		frappe.db.get_value(doctype, row.name, ["name", *fields], as_dict=True, for_update=True)
		for row in rows
	)
	return [row for row in latest if row]
