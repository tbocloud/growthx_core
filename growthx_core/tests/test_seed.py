from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from growthx_core.setup import seed

REAL_PRESS = {"growthx_press": {"mode": "press"}}


class TestSeed(IntegrationTestCase):
	def setUp(self):
		# seed.run commits; keep the test's rollback working
		self.commit = patch.object(frappe.db, "commit")
		self.commit.start()

	def tearDown(self):
		self.commit.stop()
		frappe.db.rollback()

	def _group(self):
		return frappe.db.get_value("GX Product", "solar", "press_release_group")

	def test_real_press_needs_a_release_group(self):
		with patch.dict(frappe.local.conf, REAL_PRESS), self.assertRaises(frappe.ValidationError):
			seed.run()

	def test_real_press_sets_the_group_and_skips_demo_users(self):
		with (
			patch.dict(frappe.local.conf, REAL_PRESS),
			patch.object(seed, "_demo_users") as demo_users,
		):
			seed.run(release_group="bench-test-0001")
		demo_users.assert_not_called()
		self.assertEqual(self._group(), "bench-test-0001")
		self.assertTrue(frappe.db.exists("GX Plan", "solar-growth-month"))

	def test_rerun_keeps_a_group_set_by_an_admin(self):
		seed._products(release_group="bench-test-0001")
		seed._products()
		self.assertEqual(self._group(), "bench-test-0001")

	def test_paid_subscription_only_on_test_sites(self):
		from growthx_core import accounts

		seed._products()
		seed._plans()
		user, _ = accounts.create_customer_account(
			email="gx-seed-paid@example.com",
			full_name="Paid QA",
			company="Paid QA",
			password="Long-Enough-Pass-91",
		)
		account = frappe.db.get_value("GX Account", {"user": user})
		sub = frappe.get_doc(
			{
				"doctype": "GX Subscription",
				"account": account,
				"plan": "solar-growth-month",
				"status": "trialing",
			}
		).insert(ignore_permissions=True)
		with patch.dict(frappe.local.conf, REAL_PRESS), self.assertRaises(frappe.ValidationError):
			seed.paid_subscription("gx-seed-paid@example.com")
		self.assertEqual(frappe.db.get_value("GX Subscription", sub.name, "status"), "trialing")
		with patch.dict(frappe.local.conf, {"growthx_press": {"mode": "press", "test_sites": 1}}):
			seed.paid_subscription("gx-seed-paid@example.com")
		self.assertEqual(frappe.db.get_value("GX Subscription", sub.name, "status"), "active")
		self.assertEqual(frappe.db.get_value("GX Account", account, "status"), "active")
