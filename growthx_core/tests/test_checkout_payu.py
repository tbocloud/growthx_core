"""Online checkout with PayU: billing.create_checkout, billing.checkout_status and the Payment Entry hook.

frappe_payu isn't installed in CI, so the PayU parts (Payment Request link, payment attempt) are
mocked (the gateway account and the payment attempt); the Sales Invoice, the Payment Request and the
Payment Entry are real ERPNext documents.
"""

import json
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import getdate, nowdate

from growthx_core import accounts
from growthx_core.api.v1 import _core, billing
from growthx_core.billing import payu
from growthx_core.provisioning import runner
from growthx_core.setup import seed

COMPANY = "GX Checkout Test Company"
PRODUCT = "test-checkout"
PLAN = "test-checkout-lifetime"
ITEM = "GX-TEST-CHECKOUT-ITEM"
FORM = {"url": "https://test.payu.in/_payment", "params": {"key": "k", "txnid": "PAYUtest", "hash": "h"}}


def ensure_erpnext_company():
	"""A company to invoice from, on a site where ERPNext's setup wizard never ran (CI). Committed
	once; later runs find it."""
	from erpnext.setup.setup_wizard.operations import install_fixtures

	if not frappe.db.exists("Warehouse Type", "Transit"):
		install_fixtures.install("India")
	today = getdate(nowdate())
	year = today.year if today.month >= 4 else today.year - 1
	if not frappe.db.exists("Fiscal Year", {"year_start_date": f"{year}-04-01"}):
		frappe.get_doc(
			{
				"doctype": "Fiscal Year",
				"year": f"{year}-{year + 1} GX",
				"year_start_date": f"{year}-04-01",
				"year_end_date": f"{year + 1}-03-31",
			}
		).insert()
	if not frappe.db.exists("Company", COMPANY):
		frappe.get_doc(
			{
				"doctype": "Company",
				"company_name": COMPANY,
				"abbr": "GXCT",
				"default_currency": "INR",
				"country": "India",
				"chart_of_accounts": "Standard",
			}
		).insert()
	if not frappe.db.get_single_value("Selling Settings", "selling_price_list"):
		if not frappe.db.exists("Price List", "Standard Selling"):
			frappe.get_doc(
				{
					"doctype": "Price List",
					"price_list_name": "Standard Selling",
					"selling": 1,
					"currency": "INR",
				}
			).insert()
		frappe.db.set_single_value("Selling Settings", "selling_price_list", "Standard Selling")
	frappe.db.commit()


def _call(fn, data=None):
	frappe.local.form_dict = frappe._dict(data or {})
	frappe.local.request = None
	frappe.local.request_ip = "127.0.0.1"
	with (
		patch.object(_core, "_idempotency_key", return_value=None),
		patch.object(_core, "_check_rate_limit"),
		patch.object(_core, "_require_auth"),  # the caller is frappe.session.user
		patch.object(frappe.db, "rollback"),  # an error response would roll back this test's data
	):
		response = fn()
	return response.status_code, json.loads(response.get_data())


class TestCheckoutPayU(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		ensure_erpnext_company()

	def setUp(self):
		frappe.set_user("Administrator")
		frappe.get_doc(
			{
				"doctype": "GX Product",
				"slug": PRODUCT,
				"product_name": "Checkout Test",
				"availability": "available",
			}
		).insert()
		frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": ITEM,
				"item_name": "Checkout Test Lifetime",
				"item_group": "Services",
				"stock_uom": "Nos",
				"is_stock_item": 0,
			}
		).insert()
		self.plan = frappe.get_doc(
			{
				"doctype": "GX Plan",
				"plan_id": PLAN,
				"product": PRODUCT,
				"plan_name": "Lifetime",
				"billing_interval": "one_time",
				"price": 150000,
				"gst_included": 1,
				"gst_rate": 18,
				"is_active": 1,
				"item": ITEM,
			}
		).insert()
		self.user, self.account = self._customer("gx-checkout@example.com", "Checkout Solar")
		self.patches = [
			patch.dict(frappe.local.conf, {"growthx_company": COMPANY}),
			patch.object(payu, "gateway_account", return_value="PayU-test - INR - GXCT"),
			patch.object(payu, "start_attempt", return_value=FORM),
			patch.object(runner, "get_client"),
		]
		for p in self.patches:
			p.start()

	def tearDown(self):
		for p in reversed(self.patches):
			p.stop()
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def _customer(self, email, company):
		user, _ = accounts.create_customer_account(
			email=email, full_name="Checkout Test", company=company, password="Long-Enough-Pass-91"
		)
		account = frappe.db.get_value("GX Account", {"user": user})
		frappe.get_doc(
			{
				"doctype": "GX Subscription",
				"account": account,
				"plan": PLAN,
				"status": "trialing",
				"price": 150000,
			}
		).insert(ignore_permissions=True)
		return user, account

	def _checkout(self, user=None, **data):
		frappe.set_user(user or self.user)
		try:
			return _call(billing.create_checkout, {"planId": PLAN, **data})
		finally:
			frappe.set_user("Administrator")

	def _status(self, user=None, **data):
		frappe.set_user(user or self.user)
		try:
			return _call(billing.checkout_status, data)
		finally:
			frappe.set_user("Administrator")

	def _pay(self, invoice, amount=None):
		from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

		pe = get_payment_entry("Sales Invoice", invoice, party_amount=amount)
		pe.reference_no = "PAYU-TEST"
		pe.reference_date = nowdate()
		pe.insert()
		pe.submit()
		return pe

	def test_checkout_invoices_the_plan_and_returns_payus_form(self):
		status, body = self._checkout()
		self.assertEqual(status, 201, body)
		self.assertEqual(body["provider"], "payu")
		self.assertEqual(body["redirect"], {"url": FORM["url"], "method": "POST", "fields": FORM["params"]})
		self.assertEqual(body["amount"]["amount"], 15000000)  # paise
		self.assertEqual(body["invoice"]["total"]["amount"], 15000000)
		checkout = frappe.get_doc("GX Checkout", body["reference"])
		self.assertEqual((checkout.account, checkout.plan, checkout.status), (self.account, PLAN, "pending"))
		si = frappe.get_doc("Sales Invoice", checkout.sales_invoice)
		self.assertEqual((si.docstatus, si.grand_total, si.company), (1, 150000, COMPANY))
		self.assertEqual(si.customer, frappe.db.get_value("GX Account", self.account, "customer"))
		self.assertEqual((si.items[0].item_code, si.items[0].rate), (ITEM, 150000))
		pr = frappe.get_doc("Payment Request", checkout.payment_request)
		self.assertEqual((pr.reference_doctype, pr.reference_name), ("Sales Invoice", si.name))
		self.assertEqual(
			(pr.docstatus, pr.status, pr.mute_email, pr.grand_total), (1, "Requested", 1, 150000)
		)

	def test_a_second_checkout_reuses_the_open_invoice(self):
		_, first = self._checkout()
		status, second = self._checkout()
		self.assertEqual(status, 201, second)
		self.assertEqual(second["reference"], first["reference"])
		self.assertEqual(
			frappe.db.count("Sales Invoice", {"customer": self._erp_customer(), "docstatus": 1}), 1
		)

	def test_a_price_change_cancels_the_unpaid_checkout(self):
		_, first = self._checkout()
		old_invoice = frappe.db.get_value("GX Checkout", first["reference"], "sales_invoice")
		frappe.db.set_value("GX Plan", PLAN, "price", 120000)
		status, second = self._checkout()
		self.assertEqual(status, 201, second)
		self.assertNotEqual(second["reference"], first["reference"])
		self.assertEqual(frappe.db.get_value("GX Checkout", first["reference"], "status"), "cancelled")
		self.assertEqual(frappe.db.get_value("Sales Invoice", old_invoice, "docstatus"), 2)
		self.assertEqual(second["invoice"]["total"]["amount"], 12000000)

	def test_paying_the_invoice_activates_the_plan(self):
		_, body = self._checkout()
		invoice = frappe.db.get_value("GX Checkout", body["reference"], "sales_invoice")
		pe = self._pay(invoice)

		checkout = frappe.get_doc("GX Checkout", body["reference"])
		self.assertEqual((checkout.status, checkout.payment_entry), ("paid", pe.name))
		sub = frappe.get_doc("GX Subscription", {"account": self.account})
		self.assertEqual((sub.status, sub.billing_interval, sub.gateway), ("active", "one_time", "payu"))
		self.assertIsNone(sub.current_period_end)
		self.assertEqual(sub.last_payment_id, checkout.name)
		self.assertEqual(frappe.db.get_value("GX Account", self.account, "status"), "active")
		log = frappe.get_last_doc("GX Audit Log", {"reference_name": self.account})
		self.assertEqual(log.action, "customer.online_payment")
		self.assertIn(invoice, log.details)

		status, again = self._checkout()
		self.assertEqual((status, again["error"]["code"]), (409, "invalid_transition"))

	def test_a_part_paid_invoice_is_never_replaced(self):
		_, body = self._checkout()
		self._pay(frappe.db.get_value("GX Checkout", body["reference"], "sales_invoice"), amount=50000)
		self.assertEqual(frappe.db.get_value("GX Checkout", body["reference"], "status"), "pending")
		self.assertEqual(
			frappe.db.get_value("GX Subscription", {"account": self.account}, "status"), "trialing"
		)
		frappe.db.set_value("GX Plan", PLAN, "price", 120000)
		status, again = self._checkout()
		self.assertEqual((status, again["error"]["code"]), (409, "payment_in_progress"))

	def test_status_follows_the_payment_and_is_private(self):
		_, body = self._checkout()
		status, result = self._status(reference=body["reference"])
		self.assertEqual((status, result["status"], result["subscription"]), (200, "pending", None))

		self._pay(frappe.db.get_value("GX Checkout", body["reference"], "sales_invoice"))
		status, result = self._status(reference=body["reference"])
		self.assertEqual((status, result["status"]), (200, "paid"))
		self.assertEqual(result["subscription"]["status"], "active")
		self.assertEqual(result["invoice"]["status"], "paid")

		other, _ = self._customer("gx-checkout-other@example.com", "Other Solar")
		status, result = self._status(user=other, reference=body["reference"])
		self.assertEqual(status, 404, result)
		status, result = self._status()
		self.assertEqual(status, 422, result)

	def test_status_by_payu_txnid(self):
		_, body = self._checkout()
		pr = frappe.db.get_value("GX Checkout", body["reference"], "payment_request")
		self._payu_records(pr, "PAYUfailedattempt1", "Failed")
		status, result = self._status(txnid="PAYUfailedattempt1")
		self.assertEqual((status, result["reference"], result["status"]), (200, body["reference"], "failed"))
		status, result = self._status(txnid="PAYUunknown")
		self.assertEqual(status, 404, result)

	def test_a_payment_that_cant_be_applied_needs_attention(self):
		_, body = self._checkout()
		frappe.db.set_value("GX Account", self.account, "status", "churned")
		pe = self._pay(frappe.db.get_value("GX Checkout", body["reference"], "sales_invoice"))

		self.assertEqual(
			frappe.db.get_value("Payment Entry", pe.name, "docstatus"), 1
		)  # money still recorded
		checkout = frappe.get_doc("GX Checkout", body["reference"])
		self.assertEqual(checkout.status, "needs_attention")
		self.assertIn("churned", checkout.error)
		status, result = self._status(reference=body["reference"])
		self.assertEqual((status, result["status"]), (200, "attention"))

	def test_unavailable_without_a_payu_gateway_account(self):
		self.patches[1].stop()
		try:
			with patch.dict(frappe.local.conf, {"growthx_payu_gateway_account": None}):
				status, body = self._checkout()
		finally:
			self.patches[1].start()
		self.assertEqual((status, body["error"]["code"]), (503, "payment_unavailable"))

	def test_a_plan_priced_without_gst_needs_a_tax_template(self):
		frappe.db.set_value("GX Plan", PLAN, "gst_included", 0)
		status, body = self._checkout()
		self.assertEqual((status, body["error"]["code"]), (503, "payment_unavailable"))

	def test_unknown_or_mismatched_plan(self):
		status, body = self._checkout(planId="nope")
		self.assertEqual((status, body["error"]["code"]), (422, "invalid_plan"))
		status, body = self._checkout(billingInterval="month")
		self.assertEqual((status, body["error"]["code"]), (422, "invalid_plan"))

	def test_seed_gives_each_active_plan_a_service_item(self):
		frappe.db.set_value("GX Plan", PLAN, "item", None)
		with patch.object(frappe.db, "commit"):
			seed.plan_items()
		code = frappe.db.get_value("GX Plan", PLAN, "item")
		self.assertEqual(code, "GX-TEST-CHECKOUT-LIFETIME")
		item = frappe.db.get_value(
			"Item", code, ["is_stock_item", "is_sales_item", "item_name"], as_dict=True
		)
		self.assertEqual((item.is_stock_item, item.is_sales_item), (0, 1))
		self.assertEqual(item.item_name, "Checkout Test Lifetime")
		frappe.db.set_value("GX Plan", PLAN, "item", ITEM)
		with patch.object(frappe.db, "commit"):
			seed.plan_items()  # a plan's own Item is kept
		self.assertEqual(frappe.db.get_value("GX Plan", PLAN, "item"), ITEM)

	def _erp_customer(self):
		return frappe.db.get_value("GX Account", self.account, "customer")

	def _payu_records(self, payment_request, txnid, attempt_status):
		"""What frappe_payu stores: a link for the Payment Request and an attempt (txnid) under it."""
		data = {"reference_doctype": "Payment Request", "reference_docname": payment_request}
		link = frappe.get_doc(
			{
				"doctype": "Integration Request",
				"integration_request_service": "PayU",
				"request_description": "PayU payment link",
				"reference_doctype": "Payment Request",
				"reference_docname": payment_request,
				"data": json.dumps(data),
			}
		).insert(ignore_permissions=True)
		attempt = frappe.get_doc(
			{
				"doctype": "Integration Request",
				"integration_request_service": "PayU",
				"request_description": "PayU payment attempt",
				"request_id": link.name,
				"reference_doctype": "Payment Request",
				"reference_docname": payment_request,
				"data": json.dumps({**data, "payment_link": link.name}),
				"status": attempt_status,
			}
		)
		attempt.flags._name = txnid
		attempt.insert(ignore_permissions=True)
