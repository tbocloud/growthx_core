"""lock_account (growthx_core.billing.locks): the GX Account lock, taken in a fresh transaction so that every
read after it, plain ones too, sees what others committed while the request waited for it (MariaDB keeps a
transaction's snapshot from its first plain read, REPEATABLE READ)."""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

AFTER_A_WRITE = "GrowthX: lock_account after a write"


class TestLockAccount(IntegrationTestCase):
	def setUp(self):
		from growthx_core.billing import locks

		frappe.set_user("Administrator")
		self.locks = locks
		email = f"lock-{frappe.generate_hash(length=6)}@example.com"
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": "Lock",
				"user_type": "Website User",
				"send_welcome_email": 0,
			}
		).insert()
		self.account = frappe.get_doc(
			{
				"doctype": "GX Account",
				"account_name": "Lock Co",
				"user": user.name,
				"contact_email": email,
				"status": "active",
			}
		).insert()
		self.log = []  # "commit", and each SQL statement, in order
		self.commit_patch = patch.object(
			frappe.db, "commit", side_effect=lambda *args, **kwargs: self.log.append("commit")
		)
		self.commit = self.commit_patch.start()

	def tearDown(self):
		self.commit_patch.stop()
		frappe.db.rollback()

	def _lock(self, in_test: bool, writes: int):
		real_sql = frappe.db.sql

		def logged(query, *args, **kwargs):
			self.log.append(str(query).strip().lower())
			return real_sql(query, *args, **kwargs)

		with (
			patch.dict(frappe.local.flags, {"in_test": in_test}),
			patch.object(frappe.db, "transaction_writes", writes),
			patch.object(frappe.db, "sql", side_effect=logged),
		):
			return self.locks.lock_account(self.account.name)

	@staticmethod
	def _is_lock(query: str) -> bool:
		return "for update" in query and "`tabgx account`" in query

	def _error_logs(self):
		return frappe.get_all(
			"Error Log", {"method": AFTER_A_WRITE, "reference_name": self.account.name}, pluck="error"
		)

	def test_it_commits_then_takes_the_lock_as_the_transactions_first_read(self):
		account = self._lock(in_test=False, writes=0)
		self.assertEqual((account.doctype, account.name), ("GX Account", self.account.name))
		self.commit.assert_called_once()
		after = self.log[self.log.index("commit") + 1 :]
		self.assertTrue(after and self._is_lock(after[0]), after[:2])
		self.assertEqual(self._error_logs(), [])

	def test_after_a_write_it_logs_skips_the_commit_and_still_locks(self):
		account = self._lock(in_test=False, writes=2)
		self.assertEqual(account.name, self.account.name)
		self.commit.assert_not_called()
		self.assertTrue(any(self._is_lock(q) for q in self.log), self.log)
		logs = self._error_logs()
		self.assertEqual(len(logs), 1, logs)
		self.assertIn(self.account.name, logs[0])
		self.assertIn("test_locks", logs[0])  # the caller

	def test_in_tests_it_never_commits(self):
		account = self._lock(in_test=True, writes=2)
		self.assertEqual(account.name, self.account.name)
		self.commit.assert_not_called()
		self.assertTrue(any(self._is_lock(q) for q in self.log), self.log)
		self.assertEqual(self._error_logs(), [])
