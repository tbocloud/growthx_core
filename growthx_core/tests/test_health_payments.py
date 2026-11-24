"""health.payments: the endpoint DevOps' payment alert calls."""

import json
from datetime import timedelta

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import now_datetime

from growthx_core.api.v1 import health


def _call():
	frappe.local.form_dict = frappe._dict()
	frappe.local.request = None
	frappe.local.request_ip = "127.0.0.1"
	response = health.payments()
	return response.status_code, json.loads(response.get_data())


class TestPaymentsHealth(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		# Only this test's rows count (all rolled back in tearDown).
		frappe.db.delete("Error Log")
		frappe.db.delete("GX Checkout", {"status": ("in", ["needs_attention", "pending"])})
		frappe.db.delete("Integration Request", {"integration_request_service": "PayU"})

	def tearDown(self):
		frappe.db.rollback()

	def _error(self, title, minutes_ago=0):
		log = frappe.get_doc({"doctype": "Error Log", "method": title, "error": "test"}).insert(
			ignore_permissions=True
		)
		if minutes_ago:
			frappe.db.set_value(
				"Error Log",
				log.name,
				"creation",
				now_datetime() - timedelta(minutes=minutes_ago),
				update_modified=False,
			)

	def _attempt(self, status, minutes_ago):
		ir = frappe.get_doc(
			{
				"doctype": "Integration Request",
				"integration_request_service": "PayU",
				"request_description": "PayU payment attempt",
				"status": status,
			}
		).insert(ignore_permissions=True)
		frappe.db.set_value(
			"Integration Request",
			ir.name,
			"modified",
			now_datetime() - timedelta(minutes=minutes_ago),
			update_modified=False,
		)

	def test_ok_when_nothing_is_wrong(self):
		self._error("GrowthX: something unrelated")  # not a payment error
		self._error("PayU: an old failure", minutes_ago=90)  # outside the 60-minute window
		self._attempt("Authorized", minutes_ago=5)  # still within the 30 minutes frappe_payu gets
		status, body = _call()
		self.assertEqual(status, 200, body)
		self.assertEqual(body["status"], "ok")
		self.assertEqual(body["problems"], [])
		self.assertEqual(
			(body["paymentErrors"], body["checkoutsNeedAttention"], body["paidNotApplied"]), (0, 0, 0)
		)

	def test_a_payment_error_alerts(self):
		self._error("PayU: refund request PAYU-REF-00001 got no answer")
		self._error("GrowthX API error in growthx_core.api.v1.billing.create_checkout")
		status, body = _call()
		self.assertEqual(status, 503)
		self.assertEqual(body["problems"], ["payment_errors"])
		self.assertEqual(body["paymentErrors"], 2)

	def test_money_taken_but_not_applied_alerts(self):
		self._attempt("Authorized", minutes_ago=45)
		status, body = _call()
		self.assertEqual(status, 503)
		self.assertIn("paid_not_applied", body["problems"])
		self.assertEqual(body["paidNotApplied"], 1)

	def test_a_checkout_needing_attention_alerts(self):
		checkout = frappe.new_doc("GX Checkout")
		checkout.status = "needs_attention"
		checkout.flags.ignore_links = True
		checkout.flags.ignore_mandatory = True
		checkout.insert(ignore_permissions=True)
		status, body = _call()
		self.assertEqual(status, 503)
		self.assertEqual(body["problems"], ["checkouts_need_attention"])
		self.assertEqual(body["checkoutsNeedAttention"], 1)
