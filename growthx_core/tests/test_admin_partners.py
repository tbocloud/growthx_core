"""Admin portal partners: admin.partners, admin.partner, admin.update_partner_status."""

import json
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from growthx_core import accounts
from growthx_core.api.v1 import _core, admin

PRODUCT = "test-admin-partners"
PLAN = "test-admin-partners-month"
PASSWORD = "Long-Enough-Pass-91"


def _call(fn, data=None, method="GET"):
	frappe.local.form_dict = frappe._dict(data or {})
	frappe.local.request = None
	frappe.local.request_ip = "127.0.0.1"
	with (
		patch.object(_core, "_idempotency_key", return_value=None),
		patch.object(_core, "_require_auth"),  # the caller is an admin
		patch.object(frappe.db, "rollback"),  # an error response would roll back this test's data
	):
		response = fn()
	return response.status_code, json.loads(response.get_data())


class TestAdminPartners(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		frappe.get_doc(
			{"doctype": "GX Product", "slug": PRODUCT, "product_name": "Test", "availability": "available"}
		).insert()
		frappe.get_doc(
			{
				"doctype": "GX Plan",
				"plan_id": PLAN,
				"product": PRODUCT,
				"plan_name": "Test Growth",
				"billing_interval": "month",
				"price": 1180,
				"gst_included": 1,
				"gst_rate": 18,
				"is_active": 1,
			}
		).insert()
		self.pending = self._partner("pending", "Zed Pending Partner")
		self.active = self._partner("active", "Asha Active Partner")
		self._set_status(self.active, "active")
		self.paying = self._customer("paying", self.active, paying=True)
		self.trial = self._customer("trial", self.active, paying=False)
		self.mail = patch("frappe.sendmail")
		self.sendmail = self.mail.start()

	def tearDown(self):
		self.mail.stop()
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def _partner(self, key, name):
		user = accounts.create_partner_account(
			email=f"gx-admin-partner-{key}@example.com",
			full_name=name,
			password=PASSWORD,
			company=f"{name} Co",
		)
		return frappe.db.get_value("GX Partner Profile", {"user": user})

	def _set_status(self, partner, status):
		doc = frappe.get_doc("GX Partner Profile", partner)
		doc.status = status
		doc.save()

	def _customer(self, key, partner, paying):
		code = frappe.db.get_value("GX Partner Profile", partner, "referral_code")
		user, _ = accounts.create_customer_account(
			email=f"gx-admin-partner-cust-{key}@example.com",
			full_name="Cust Omer",
			company=f"Customer {key}",
			password=PASSWORD,
			referral_code=code,
		)
		account = frappe.db.get_value("GX Account", {"user": user})
		frappe.get_doc(
			{
				"doctype": "GX Subscription",
				"account": account,
				"plan": PLAN,
				"status": "active" if paying else "trialing",
				"price": 1180,
				"gst_included": 1,
				"gst_rate": 18,
			}
		).insert(ignore_permissions=True)
		frappe.db.set_value("GX Account", account, "status", "active" if paying else "trial")
		return account

	def _row(self, body, partner):
		return next(i for i in body["items"] if i["id"] == partner)

	def test_list_has_every_partner_with_numbers(self):
		status, body = _call(admin.partners, {"q": "gx-admin-partner-", "pageSize": 50})
		self.assertEqual(status, 200, body)
		self.assertEqual(body["total"], 2)
		row = self._row(body, self.active)
		self.assertEqual(
			(row["name"], row["email"], row["status"], row["referredCustomers"], row["payingCustomers"]),
			("Asha Active Partner", "gx-admin-partner-active@example.com", "active", 2, 1),
		)
		expected = admin.money(
			admin._mrr(frappe._dict(price=1180, gst_included=1, gst_rate=18, billing_interval="month"))
		)
		self.assertEqual(row["mrr"], expected)
		self.assertTrue(row["referralCode"] and row["joinedAt"])
		pending = self._row(body, self.pending)
		self.assertEqual(
			(pending["status"], pending["referredCustomers"], pending["mrr"]["amount"]),
			("pending_approval", 0, 0),
		)

	def test_filter_and_search(self):
		_, body = _call(admin.partners, {"status": "pending_approval", "q": "Zed Pending"})
		self.assertEqual([i["id"] for i in body["items"]], [self.pending])
		code = frappe.db.get_value("GX Partner Profile", self.active, "referral_code")
		_, body = _call(admin.partners, {"q": code})
		self.assertEqual([i["id"] for i in body["items"]], [self.active])
		status, body = _call(admin.partners, {"status": "nope"})
		self.assertEqual(status, 422)

	def test_page_with_no_partners(self):
		# A customer with no partner must not break a page with no partners (production 500, ref 285292204)
		accounts.create_customer_account(
			email="gx-admin-partner-cust-direct@example.com",
			full_name="Cust Omer",
			company="Customer direct",
			password=PASSWORD,
		)
		for data in ({"q": "no partner has this name"}, {"status": "suspended", "q": "gx-admin-partner-"}):
			status, body = _call(admin.partners, data)
			self.assertEqual((status, body["items"], body["total"]), (200, [], 0), body)

	def test_detail_has_referred_customers(self):
		status, body = _call(admin.partner, {"id": self.active})
		self.assertEqual(status, 200, body)
		by_id = {c["id"]: c for c in body["customers"]}
		self.assertEqual(set(by_id), {self.paying, self.trial})
		self.assertEqual(
			(by_id[self.paying]["status"], by_id[self.paying]["plan"]), ("active", "Test Growth")
		)
		self.assertEqual(by_id[self.trial]["mrr"]["amount"], 0)
		self.assertEqual(_call(admin.partner, {"id": "nope"})[0], 404)

	def test_approve_creates_sales_partner_emails_and_logs(self):
		status, body = _call(admin.update_partner_status, {"id": self.pending, "action": "approve"}, "POST")
		self.assertEqual((status, body["status"]), (200, "active"))
		self.assertTrue(frappe.db.get_value("GX Partner Profile", self.pending, "sales_partner"))
		self.assertEqual(
			self.sendmail.call_args.kwargs["recipients"], ["gx-admin-partner-pending@example.com"]
		)
		self.assertEqual(
			[(h["action"], h["details"]) for h in body["history"]], [("approve", "pending_approval → active")]
		)

	def test_suspend_needs_a_reason_and_stops_the_referral_code(self):
		status, body = _call(admin.update_partner_status, {"id": self.active, "action": "suspend"}, "POST")
		self.assertEqual((status, list(body["error"]["fields"])), (422, ["reason"]))

		status, body = _call(
			admin.update_partner_status,
			{"id": self.active, "action": "suspend", "reason": "Spam referrals"},
			"POST",
		)
		self.assertEqual((status, body["status"]), (200, "suspended"))
		self.assertEqual(body["history"][-1]["details"], "active → suspended: Spam referrals")
		from growthx_core.partners import referrals

		code = frappe.db.get_value("GX Partner Profile", self.active, "referral_code")
		self.assertIsNone(referrals.resolve(code, "someone@example.com"))

		status, body = _call(admin.update_partner_status, {"id": self.active, "action": "reactivate"}, "POST")
		self.assertEqual((status, body["status"]), (200, "active"))

	def test_wrong_transition(self):
		status, body = _call(admin.update_partner_status, {"id": self.active, "action": "approve"}, "POST")
		self.assertEqual((status, body["error"]["code"]), (409, "not_changeable"))
		self.assertEqual(body["error"]["message"], "A partner who is active can't be approved.")
		status, _ = _call(admin.update_partner_status, {"id": self.pending, "action": "reactivate"}, "POST")
		self.assertEqual(status, 409)
