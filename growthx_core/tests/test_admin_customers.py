"""Admin portal customers: admin.customer, admin.update_customer_status, admin.record_payment,
admin.audit_log."""

import json
from datetime import timedelta
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import get_datetime, now_datetime

from growthx_core import accounts
from growthx_core.api.v1 import _core, admin
from growthx_core.provisioning import runner

PRODUCT = "test-admin-customers"
PLAN = "test-admin-customers-month"
YEAR_PLAN = "test-admin-customers-year"
OTHER_PRODUCT = "test-admin-customers-other"


def _call(fn, data=None):
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


class TestAdminCustomers(IntegrationTestCase):
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
				"price": 1000,
				"is_active": 1,
			}
		).insert()
		frappe.get_doc(
			{
				"doctype": "GX Plan",
				"plan_id": YEAR_PLAN,
				"product": PRODUCT,
				"plan_name": "Test Growth Yearly",
				"billing_interval": "year",
				"price": 10000,
				"is_active": 0,  # retired from the catalogue; can still be recorded
			}
		).insert()
		user, _ = accounts.create_customer_account(
			email="gx-admin-cust@example.com",
			full_name="Noor Admin",
			company="Noor Admin Solar",
			password="Long-Enough-Pass-91",
		)
		self.account = frappe.db.get_value("GX Account", {"user": user})
		self.trial_end = now_datetime() + timedelta(days=5)
		self.sub = frappe.get_doc(
			{
				"doctype": "GX Subscription",
				"account": self.account,
				"plan": PLAN,
				"status": "trialing",
				"price": 1000,
				"trial_ends_at": self.trial_end,
			}
		).insert(ignore_permissions=True)
		self.site = frappe.get_doc(
			{
				"doctype": "GX Tenant",
				"account": self.account,
				"product": PRODUCT,
				"subdomain": "noor-admin-x",
				"site_url": "https://noor-admin-x.example.com",
				"press_site_id": "noor-admin-x.example.com",
				"status": "ready",
				"country": "India",
			}
		).insert(ignore_permissions=True)
		self.press = MagicMock()
		self.client = patch.object(runner, "get_client", return_value=self.press)
		self.client.start()

	def tearDown(self):
		self.client.stop()
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def _action(self, action, **extra):
		return _call(admin.update_customer_status, {"id": self.account, "action": action, **extra})

	def test_detail(self):
		status, body = _call(admin.customer, {"id": self.account})
		self.assertEqual(status, 200, body)
		self.assertEqual(
			(body["company"], body["contactName"], body["status"]),
			("Noor Admin Solar", "Noor Admin", "trial"),
		)
		self.assertEqual(body["country"], "India")
		self.assertEqual(get_datetime(body["trialEndsAt"].rstrip("Z")).date() is not None, True)
		self.assertEqual(
			body["sites"],
			[
				{
					"id": self.site.name,
					"productSlug": PRODUCT,
					"siteUrl": self.site.site_url,
					"status": "ready",
				}
			],
		)
		self.assertEqual(
			[(s["id"], s["plan"], s["status"]) for s in body["subscriptions"]],
			[(self.sub.name, "Test Growth", "trialing")],
		)
		self.assertEqual(body["history"], [])
		self.assertEqual(_call(admin.customer, {"id": "nope"})[0], 404)

	def test_extend_trial_from_the_later_of_now_and_trial_end(self):
		status, body = self._action("extend_trial", days=10)
		self.assertEqual(status, 200, body)
		new_end = frappe.db.get_value("GX Subscription", self.sub.name, "trial_ends_at")
		self.assertAlmostEqual((new_end - self.trial_end).total_seconds(), 10 * 86400, delta=2)
		self.assertEqual(body["history"][0]["action"], "extend_trial")

		# An already-ended trial extends from now.
		frappe.db.set_value(
			"GX Subscription", self.sub.name, "trial_ends_at", now_datetime() - timedelta(days=3)
		)
		self._action("extend_trial", days=2)
		new_end = frappe.db.get_value("GX Subscription", self.sub.name, "trial_ends_at")
		self.assertAlmostEqual((new_end - now_datetime()).total_seconds(), 2 * 86400, delta=60)

	def test_extend_trial_validation(self):
		for days in (None, 0, 31, "5", True, 2.5):
			status, body = self._action("extend_trial", days=days)
			self.assertEqual((status, list(body["error"]["fields"])), (422, ["days"]), days)
		frappe.db.set_value("GX Account", self.account, "status", "active")
		status, body = self._action("extend_trial", days=5)
		self.assertEqual((status, body["error"]["code"]), (409, "invalid_transition"))

	def test_suspend_and_reactivate_take_the_sites_down_and_up(self):
		status, body = self._action("suspend")
		self.assertEqual((status, list(body["error"]["fields"])), (422, ["reason"]))

		status, body = self._action("suspend", reason="Chargeback")
		self.assertEqual(
			(status, body["status"], body["sites"][0]["status"]), (200, "suspended", "suspended")
		)
		self.press.suspend_site.assert_called_once_with("noor-admin-x.example.com")
		self.assertEqual(body["history"][-1]["details"], "trial → suspended: Chargeback")
		self.assertEqual(self._action("suspend", reason="again")[0], 409)

		status, body = self._action("reactivate")
		self.assertEqual((status, body["status"], body["sites"][0]["status"]), (200, "trial", "ready"))
		self.press.resume_site.assert_called_once_with("noor-admin-x.example.com")

	def test_reactivate_after_the_trial_ended_is_active(self):
		self._action("suspend", reason="Test")
		frappe.db.set_value(
			"GX Subscription", self.sub.name, "trial_ends_at", now_datetime() - timedelta(days=1)
		)
		status, body = self._action("reactivate")
		self.assertEqual((status, body["status"]), (200, "active"))

	def test_press_failure_keeps_that_site_and_says_so(self):
		self.press.suspend_site.side_effect = Exception("Press down")
		status, body = self._action("suspend", reason="Test")
		self.assertEqual((status, body["status"], body["sites"][0]["status"]), (200, "suspended", "ready"))
		self.assertIn(f"sites not changed, Press error: {self.site.name}", body["history"][-1]["details"])

	def _pay(self, **extra):
		data = {"id": self.account, "plan": YEAR_PLAN, "amount": 1000000, "reference": "UTR123", **extra}
		return _call(admin.record_payment, {"paidOn": "2026-11-01", **data})

	def test_record_payment_makes_the_customer_active_on_that_plan(self):
		status, body = self._pay()
		self.assertEqual((status, body["status"], body["plan"]), (200, "active", "Test Growth Yearly"), body)
		self.assertIsNone(body["trialEndsAt"])
		self.assertEqual(body["subscriptions"][-1]["status"], "active")
		sub = frappe.get_doc("GX Subscription", self.sub.name)
		self.assertEqual((sub.plan, sub.billing_interval, sub.price), (YEAR_PLAN, "year", 10000))
		self.assertEqual(
			(get_datetime(sub.current_period_end).date().isoformat(), sub.gateway, sub.last_payment_id),
			("2027-11-01", "manual", "UTR123"),
		)
		self.assertEqual(
			body["history"][-1]["details"],
			"trial → active: ₹ 10,000.00 for Test Growth Yearly paid on 2026-11-01, reference UTR123",
		)
		self.assertEqual(body["history"][-1]["action"], "record_payment")
		status, body = self._pay(reference="UTR124")
		self.assertEqual((status, body["error"]["code"]), (409, "invalid_transition"))

	def test_record_payment_brings_a_suspended_customers_sites_back(self):
		self._action("suspend", reason="Trial over")
		status, body = self._pay(plan=PLAN, amount=100000)
		self.assertEqual((status, body["status"], body["sites"][0]["status"]), (200, "active", "ready"))
		self.press.resume_site.assert_called_once_with("noor-admin-x.example.com")
		self.assertEqual(
			get_datetime(frappe.db.get_value("GX Subscription", self.sub.name, "current_period_end")).date(),
			get_datetime("2026-12-01").date(),
		)

	def test_record_payment_validation(self):
		for extra, field in (
			({"amount": 0}, "amount"),
			({"amount": 99.5}, "amount"),
			({"amount": True}, "amount"),
			({"paidOn": "01-11-2026"}, "paidOn"),
			({"paidOn": "2026-11-1"}, "paidOn"),
			({"paidOn": "2999-01-01"}, "paidOn"),
			({"reference": ""}, "reference"),
			({"plan": "nope"}, "plan"),
		):
			status, body = self._pay(**extra)
			self.assertEqual((status, list(body["error"]["fields"])), (422, [field]), extra)
		frappe.get_doc(
			{
				"doctype": "GX Product",
				"slug": OTHER_PRODUCT,
				"product_name": "Other",
				"availability": "available",
			}
		).insert()
		frappe.get_doc(
			{
				"doctype": "GX Plan",
				"plan_id": f"{OTHER_PRODUCT}-month",
				"product": OTHER_PRODUCT,
				"plan_name": "Other",
				"billing_interval": "month",
				"price": 500,
			}
		).insert()
		status, body = self._pay(plan=f"{OTHER_PRODUCT}-month")
		self.assertEqual((status, list(body["error"]["fields"])), (422, ["plan"]))
		self.assertEqual(self._pay(id="nope")[0], 404)
		self.assertEqual(frappe.db.get_value("GX Account", self.account, "status"), "trial")

	def test_audit_log(self):
		self._action("suspend", reason="Audit me")
		status, body = _call(admin.audit_log, {"entityType": "customer", "entityId": self.account})
		self.assertEqual(status, 200, body)
		self.assertEqual(body["total"], 1)
		item = body["items"][0]
		self.assertEqual(
			(item["action"], item["entityType"], item["entityId"], item["entityLabel"], item["actor"]),
			("suspend", "customer", self.account, "Noor Admin Solar", "Administrator"),
		)
		today = now_datetime().date().isoformat()
		self.assertEqual(
			_call(admin.audit_log, {"entityId": self.account, "from": today, "to": today})[1]["total"], 1
		)
		self.assertEqual(
			_call(admin.audit_log, {"entityId": self.account, "to": "2000-01-01"})[1]["total"], 0
		)
		self.assertEqual(_call(admin.audit_log, {"entityType": "nope"})[0], 422)
		self.assertEqual(_call(admin.audit_log, {"from": "2026-12-01", "to": "2026-01-01"})[0], 422)
