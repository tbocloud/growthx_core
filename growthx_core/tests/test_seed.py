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
