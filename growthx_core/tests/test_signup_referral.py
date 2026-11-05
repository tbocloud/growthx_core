"""A customer signing up with a partner's link: the partner is notified, and a failing
notification never fails the customer's sign-up (review feedback from Web, 16 Oct 2026)."""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from growthx_core import accounts
from growthx_core.partners import notifications


class TestSignupReferral(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.partner = frappe.get_doc(
			{
				"doctype": "GX Partner Profile",
				"partner_name": "Referrer Tester",
				"user": "Administrator",
				"referral_code": f"REF-{frappe.generate_hash(length=6).upper()}",
				"status": "pending_approval",
			}
		).insert(ignore_permissions=True)
		# Active, without creating a Sales Partner (not what this test is about).
		frappe.db.set_value("GX Partner Profile", self.partner.name, "status", "active")

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def _signup(self, key):
		user, _ = accounts.create_customer_account(
			email=f"gx-signup-ref-{key}@example.com",
			full_name=f"Referred {key}",
			company=f"Referred {key} Solar",
			password="Long-Enough-Pass-91",
			referral_code=self.partner.referral_code,
		)
		return frappe.db.get_value(
			"GX Account", {"user": user}, ["name", "referred_by", "customer"], as_dict=True
		)

	def test_referred_signup_notifies_the_partner(self):
		account = self._signup("ok")
		self.assertEqual(account.referred_by, self.partner.name)
		self.assertTrue(
			frappe.db.exists("GX Notification", {"user": "Administrator", "deep_link_id": account.name})
		)

	def test_signup_succeeds_even_if_the_partner_notification_fails(self):
		with patch.object(notifications, "on_customer_referred", side_effect=RuntimeError("push broken")):
			account = self._signup("fail")
		self.assertEqual(account.referred_by, self.partner.name)  # sign-up and attribution are intact
		self.assertTrue(frappe.db.exists("Customer", account.customer))
		self.assertFalse(frappe.db.exists("GX Notification", {"deep_link_id": account.name}))
		self.assertTrue(
			frappe.db.exists(
				"Error Log", {"method": ("like", f"%could not notify the partner of {account.name}%")}
			)
		)
