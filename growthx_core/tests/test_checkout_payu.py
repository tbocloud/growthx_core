"""Online checkout with PayU: billing.create_checkout, billing.checkout_status and the Payment Entry hook.

frappe_payu isn't installed in CI, so the PayU parts (Payment Request link, payment attempt) are
mocked (the gateway account and the payment attempt); the Sales Invoice, the Payment Request and the
Payment Entry are real ERPNext documents.
"""

import json
import unittest
from contextlib import contextmanager
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import fmt_money, getdate, nowdate
from werkzeug.test import EnvironBuilder

from growthx_core import accounts
from growthx_core.api.v1 import _core, billing
from growthx_core.api.v1 import customer as customer_api
from growthx_core.billing import billing_details, payu, tax_invoice
from growthx_core.provisioning import runner
from growthx_core.setup import seed

COMPANY = "GX Checkout Test Company"
PRODUCT = "test-checkout"
PLAN = "test-checkout-lifetime"
ITEM = "GX-TEST-CHECKOUT-ITEM"
IC = "india_compliance" in frappe.get_installed_apps()
KERALA = {
	"billingName": None,
	"addressLine1": "1 Test Street",
	"addressLine2": None,
	"city": "Kochi",
	"state": "Kerala",
	"pincode": "682001",
	"gstin": None,
}
KERALA_GSTIN = "32AABCK1234M1ZT"  # a customer's; 32AAVFT5109L1ZQ is the seller's own
FORM = {"url": "https://test.payu.in/_payment", "params": {"key": "k", "txnid": "PAYUtest", "hash": "h"}}


def item_gst() -> dict:
	"""India Compliance requires an HSN/SAC code on sales items."""
	if not IC:
		return {}
	from growthx_core.setup import gst

	return {"gst_hsn_code": seed._ensure_sac(gst.DEFAULT_SAC)}


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


def ensure_gst_company():
	"""The company's GST set-up (growthx_core.setup.gst without the plan items), committed once."""
	from growthx_core.setup import gst

	with patch.dict(frappe.local.conf, {"growthx_company": COMPANY}):
		gst._seller(COMPANY)
		gst._tax_templates(COMPANY)
		gst._series()
		gst._gst_settings()
		gst._address_template()
		gst._print_format()
	frappe.db.commit()


def _last_error() -> str:
	"""The newest Error Log, for assert messages: a 500's traceback is only there (e.g. on CI)."""
	row = frappe.get_all("Error Log", fields=["method", "error"], order_by="creation desc", limit=1)
	return f"{row[0].method}\n{row[0].error}" if row else "no Error Log"


def _call(fn, data=None, request=None):
	frappe.local.form_dict = frappe._dict(data or {})
	frappe.local.request = request
	frappe.local.request_ip = "127.0.0.1"
	with (
		patch.object(_core, "_idempotency_key", return_value=None),
		patch.object(_core, "_check_rate_limit"),
		patch.object(_core, "_require_auth"),  # the caller is frappe.session.user
		patch.object(frappe.db, "rollback"),  # an error response would roll back this test's data
	):
		response = fn()
	if response.mimetype != "application/json":
		return response.status_code, response  # a file download
	return response.status_code, json.loads(response.get_data())


@unittest.skipUnless(IC, "GST invoices need India Compliance (CI installs it)")
class TestCheckoutPayU(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		ensure_erpnext_company()
		ensure_gst_company()

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
				**item_gst(),
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
		doc = frappe.get_doc("GX Account", account)
		payu.ensure_customer(doc)
		billing_details.save(doc, KERALA)
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

	def test_a_plan_priced_without_gst_is_refused(self):
		frappe.db.set_value("GX Plan", PLAN, "gst_included", 0)
		status, body = self._checkout()
		self.assertEqual((status, body["error"]["code"]), (503, "payment_unavailable"))
		self.assertFalse(frappe.db.exists("Sales Invoice", {"customer": self._erp_customer()}))

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

	def _payu_records(self, payment_request, txnid, attempt_status, link=None):
		"""What frappe_payu stores: a link for the Payment Request and an attempt (txnid) under it.
		Pass `link` (the returned name) to add another attempt under the same link."""
		data = {"reference_doctype": "Payment Request", "reference_docname": payment_request}
		link = link and frappe.get_doc("Integration Request", link)
		link = link or frappe.get_doc(
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
		return link.name

	def test_checkout_needs_billing_details(self):
		address = billing_details.address_name(frappe.db.get_value("GX Account", self.account, "customer"))
		frappe.db.set_value("Address", address, "disabled", 1)
		status, body = self._checkout()
		self.assertEqual((status, body["error"]["code"]), (422, "billing_details_required"), body)
		self.assertFalse(frappe.db.exists("GX Checkout", {"account": self.account}))

	# ------------------------------------------------------------ payment safety (follow-up to #54)
	# record_payment closes open checkouts, a PayU attempt in flight is never cancelled under the
	# customer, a cancelled Payment Entry is flagged, and activation uses the payment's date.

	def _checkout_docs(self, reference):
		return frappe.db.get_value(
			"GX Checkout", reference, ["status", "sales_invoice", "payment_request"], as_dict=True
		)

	def _record_payment(self, plan=PLAN, reference="NEFT-1"):
		from growthx_core.api.v1 import admin

		return _call(
			admin.record_payment,
			{
				"id": self.account,
				"plan": plan,
				"reference": reference,
				"amount": 15000000,
				"paidOn": nowdate(),
			},
		)

	def _with_payment_request(self, body):
		"""create_checkout makes the Payment Request through ERPNext; give the checkout one we control."""
		row = self._checkout_docs(body["reference"])
		if row.payment_request:
			return row.payment_request
		self.skipTest("this ERPNext set-up made no Payment Request")

	def test_record_payment_cancels_the_open_checkout(self):
		_, body = self._checkout()
		row = self._checkout_docs(body["reference"])
		status, res = self._record_payment()
		self.assertEqual(status, 200, res)
		self.assertEqual(frappe.db.get_value("GX Checkout", body["reference"], "status"), "cancelled")
		self.assertEqual(frappe.db.get_value("Sales Invoice", row.sales_invoice, "docstatus"), 2)
		if row.payment_request:
			self.assertEqual(frappe.db.get_value("Payment Request", row.payment_request, "docstatus"), 2)
		self.assertEqual(
			frappe.db.get_value("GX Subscription", {"account": self.account}, ["status", "gateway"]),
			("active", "manual"),
		)

	def test_record_payment_refuses_a_part_paid_checkout(self):
		_, body = self._checkout()
		self._pay(self._checkout_docs(body["reference"]).sales_invoice, amount=50000)
		status, res = self._record_payment()
		self.assertEqual((status, res["error"]["code"]), (409, "payment_in_progress"))
		self.assertEqual(
			frappe.db.get_value("GX Subscription", {"account": self.account}, "status"), "trialing"
		)

	def test_a_payu_attempt_in_flight_is_never_cancelled(self):
		_, body = self._checkout()
		pr = self._with_payment_request(body)
		self._payu_records(pr, "PAYUinflight1", "Queued")
		# record_payment waits
		status, res = self._record_payment()
		self.assertEqual((status, res["error"]["code"]), (409, "payment_in_progress"))
		# so does replacing the checkout (here: the plan's price changed)
		frappe.db.set_value("GX Plan", PLAN, "price", 120000)
		status, res = self._checkout()
		self.assertEqual((status, res["error"]["code"]), (409, "payment_in_progress"))
		self.assertEqual(self._checkout_docs(body["reference"]).status, "pending")
		self.assertEqual(
			frappe.db.get_value("GX Subscription", {"account": self.account}, "status"), "trialing"
		)

	def _age_attempt(self, name, minutes):
		frappe.db.set_value(
			"Integration Request",
			name,
			"creation",
			frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=-minutes),
			update_modified=False,
		)

	def test_an_old_queued_or_finished_attempt_doesnt_block(self):
		_, body = self._checkout()
		pr = self._with_payment_request(body)
		self._payu_records(pr, "PAYUold1", "Queued")
		name = "PAYUold1"
		self._age_attempt(name, payu.IN_FLIGHT_MINUTES + 1)
		self.assertFalse(payu.attempt_in_flight(pr))
		frappe.db.set_value(
			"Integration Request", name, {"status": "Failed", "creation": frappe.utils.now_datetime()}
		)
		self.assertFalse(payu.attempt_in_flight(pr))
		status, res = self._record_payment()
		self.assertEqual(status, 200, res)

	def test_an_authorized_attempt_blocks_at_any_age(self):
		# PayU has the money; frappe_payu retries completing the Payment Request hourly.
		_, body = self._checkout()
		pr = self._with_payment_request(body)
		self._payu_records(pr, "PAYUauth1", "Authorized")
		self._age_attempt("PAYUauth1", payu.IN_FLIGHT_MINUTES + 1)
		self.assertTrue(payu.attempt_in_flight(pr))
		status, res = self._record_payment()
		self.assertEqual((status, res["error"]["code"]), (409, "payment_in_progress"))
		self.assertEqual(self._checkout_docs(body["reference"]).status, "pending")
		self.assertEqual(frappe.db.get_value("Payment Request", pr, "docstatus"), 1)
		self._age_attempt("PAYUauth1", 60 * 24 * 3)
		self.assertTrue(payu.attempt_in_flight(pr))

	def test_create_checkout_never_starts_a_second_attempt_after_an_authorized_one(self):
		_, body = self._checkout()
		pr = self._with_payment_request(body)
		start_attempt = payu.start_attempt  # the mock from setUp
		# a recent Queued attempt: the customer retrying on PayU's page gets a new attempt
		link = self._payu_records(pr, "PAYUretry1", "Queued")
		status, res = self._checkout()
		self.assertEqual((status, res["reference"]), (201, body["reference"]), res)
		calls = start_attempt.call_count
		# PayU has the money for this checkout: no new attempt, so no second payment
		self._payu_records(pr, "PAYUauth2", "Authorized", link=link)
		status, res = self._checkout()
		self.assertEqual((status, res["error"]["code"]), (409, "payment_in_progress"), res)
		self.assertEqual(start_attempt.call_count, calls)
		self.assertEqual(self._checkout_docs(body["reference"]).status, "pending")

	def test_an_authorized_attempt_counts_even_after_a_newer_failed_one(self):
		_, body = self._checkout()
		pr = self._with_payment_request(body)
		link = self._payu_records(pr, "PAYUauth3", "Authorized")
		self._payu_records(pr, "PAYUfail3", "Failed", link=link)
		self._age_attempt("PAYUauth3", 10)  # the Failed one is the newest
		self.assertEqual(payu.last_attempt_status(pr), "Failed")
		self.assertTrue(payu.attempt_in_flight(pr))
		status, res = self._record_payment()
		self.assertEqual((status, res["error"]["code"]), (409, "payment_in_progress"), res)
		self.assertEqual(self._checkout_docs(body["reference"]).status, "pending")
		self.assertEqual(frappe.db.get_value("Payment Request", pr, "docstatus"), 1)
		# the customer is not told the payment failed (and to try again)
		self.assertEqual(self._status(reference=body["reference"])[1]["status"], "pending")

	def test_checkout_status_is_reversed_after_the_payment_entry_is_cancelled(self):
		_, body = self._checkout()
		pe = self._pay(self._checkout_docs(body["reference"]).sales_invoice)
		self.assertEqual(self._status(reference=body["reference"])[1]["status"], "paid")
		pe.cancel()
		status, res = self._status(reference=body["reference"])
		self.assertEqual((status, res["status"], res["subscription"]), (200, "reversed", None))
		# also when the hook could not flag the checkout: the Payment Entry decides
		frappe.db.set_value("GX Checkout", body["reference"], "status", "paid")
		self.assertEqual(self._status(reference=body["reference"])[1]["status"], "reversed")

	def test_a_cancelled_payment_entry_flags_the_checkout(self):
		_, body = self._checkout()
		pe = self._pay(self._checkout_docs(body["reference"]).sales_invoice)
		self.assertEqual(self._checkout_docs(body["reference"]).status, "paid")
		pe.cancel()
		checkout = frappe.get_doc("GX Checkout", body["reference"])
		self.assertEqual(checkout.status, "needs_attention")
		self.assertIn(pe.name, checkout.error)
		log = frappe.get_last_doc("GX Audit Log", {"reference_name": self.account})
		self.assertEqual(log.action, "customer.online_payment_cancelled")
		self.assertIn(pe.name, log.details)
		# no automatic suspension
		self.assertEqual(
			frappe.db.get_value("GX Subscription", {"account": self.account}, "status"), "active"
		)
		self.assertEqual(frappe.db.get_value("GX Account", self.account, "status"), "active")

	def test_activation_uses_the_payment_date(self):
		monthly = frappe.get_doc(
			{
				"doctype": "GX Plan",
				"plan_id": "test-checkout-monthly",
				"product": PRODUCT,
				"plan_name": "Monthly",
				"billing_interval": "month",
				"price": 150000,
				"gst_included": 1,
				"gst_rate": 18,
				"is_active": 1,
				"item": ITEM,
			}
		).insert()
		frappe.set_user(self.user)
		try:
			_, body = _call(billing.create_checkout, {"planId": monthly.name})
		finally:
			frappe.set_user("Administrator")
		from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

		paid_on = frappe.utils.add_days(nowdate(), -3)
		pe = get_payment_entry("Sales Invoice", self._checkout_docs(body["reference"]).sales_invoice)
		pe.reference_no = "PAYU-TEST"
		pe.reference_date = paid_on
		pe.posting_date = paid_on
		pe.insert()
		pe.submit()
		period_end = frappe.db.get_value("GX Subscription", {"account": self.account}, "current_period_end")
		self.assertEqual(getdate(period_end), getdate(frappe.utils.add_months(paid_on, 1)))

	# ------------------------------------------------------------ GST tax invoices

	def _invoice(self, body):
		return frappe.get_doc("Sales Invoice", self._checkout_docs(body["reference"]).sales_invoice)

	def _billing(self, **over):
		doc = frappe.get_doc("GX Account", self.account)
		billing_details.save(doc, {**KERALA, **over})

	def _taxes(self, si):
		return [(t.account_head.rsplit(" - ", 1)[0], t.tax_amount) for t in si.taxes]

	def test_a_kerala_customer_gets_cgst_and_sgst_inside_the_plan_price(self):
		status, body = self._checkout()
		self.assertEqual(status, 201, body)
		si = self._invoice(body)
		self.assertRegex(si.name, r"^GX/\d{2}-\d{2}/\d{4}$")
		self.assertEqual(
			(si.net_total, si.total_taxes_and_charges, si.grand_total), (127118.64, 22881.36, 150000)
		)
		self.assertEqual(
			sorted(self._taxes(si)), [("Output Tax CGST", 11440.68), ("Output Tax SGST", 11440.68)]
		)
		self.assertEqual(
			(si.customer_name, si.place_of_supply, si.billing_address_gstin, si.gst_category),
			("Checkout Solar", "32-Kerala", None, "Unregistered"),
		)
		self.assertEqual(si.customer_address, billing_details.address_name(self._erp_customer()))
		self.assertEqual(si.company_gstin, "32AAVFT5109L1ZQ")
		self.assertEqual(si.items[0].item_tax_template, "GST 18% - GXCT")
		self.assertEqual(
			(
				body["invoice"]["number"],
				body["invoice"]["subtotal"]["amount"],
				body["invoice"]["tax"]["amount"],
			),
			(si.name, 12711864, 2288136),
		)
		self.assertEqual(body["amount"]["amount"], 15000000)

	def test_another_state_with_a_gstin_gets_igst_under_the_billing_name(self):
		self._billing(
			billingName="Checkout Solar Private Limited",
			city="Bengaluru",
			state="Karnataka",
			pincode="560001",
			gstin="29AABCG1234M1ZK",
		)
		status, body = self._checkout()
		self.assertEqual(status, 201, body)
		si = self._invoice(body)
		self.assertEqual(self._taxes(si), [("Output Tax IGST", 22881.36)])
		self.assertEqual(si.grand_total, 150000)
		self.assertEqual(
			(si.customer_name, si.place_of_supply, si.billing_address_gstin, si.gst_category),
			("Checkout Solar Private Limited", "29-Karnataka", "29AABCG1234M1ZK", "Registered Regular"),
		)
		self.assertIn("Bengaluru", si.address_display)

	def test_changed_billing_details_replace_the_unpaid_checkout(self):
		_, first = self._checkout()
		self._billing()  # saved again unchanged: same invoice
		_, again = self._checkout()
		self.assertEqual(again["reference"], first["reference"])

		changes = (
			{"addressLine1": "2 Test Street"},
			{"city": "Thrissur", "pincode": "680001"},
			{"billingName": "Checkout Solar LLP"},
			{"gstin": KERALA_GSTIN},
			{"state": "Tamil Nadu", "pincode": "600001", "city": "Chennai"},
		)
		for over in changes:
			old = again["reference"]
			old_invoice = self._checkout_docs(old).sales_invoice
			self._billing(**over)
			status, again = self._checkout()
			self.assertEqual(status, 201, again)
			self.assertNotEqual(again["reference"], old, over)
			self.assertEqual(frappe.db.get_value("GX Checkout", old, "status"), "cancelled", over)
			self.assertEqual(frappe.db.get_value("Sales Invoice", old_invoice, "docstatus"), 2, over)
		si = self._invoice(again)
		self.assertEqual(
			(si.place_of_supply, self._taxes(si)), ("33-Tamil Nadu", [("Output Tax IGST", 22881.36)])
		)
		self.assertIn("Chennai", si.address_display)

	def test_a_billing_change_waits_for_an_authorized_payment(self):
		_, body = self._checkout()
		pr = self._with_payment_request(body)
		self._payu_records(pr, "PAYUauthgst", "Authorized")
		self._billing(state="Karnataka", city="Bengaluru", pincode="560001")
		status, res = self._checkout()
		self.assertEqual((status, res["error"]["code"]), (409, "payment_in_progress"), res)
		self.assertEqual(self._checkout_docs(body["reference"]).status, "pending")

	def test_a_checkout_from_before_gst_invoicing_is_replaced(self):
		_, first = self._checkout()
		old = self._checkout_docs(first["reference"])
		frappe.db.set_value("Sales Invoice", old.sales_invoice, "taxes_and_charges", None)
		status, second = self._checkout()
		self.assertEqual(status, 201, second)
		self.assertNotEqual(second["reference"], first["reference"])
		self.assertEqual(frappe.db.get_value("GX Checkout", first["reference"], "status"), "cancelled")

	def test_an_invoice_that_isnt_the_plan_price_is_never_issued(self):
		from growthx_core.setup import gst

		wrong = gst.item_tax_template(COMPANY, 5)
		with patch.object(gst, "item_tax_template", return_value=wrong):
			status, body = self._checkout()
		self.assertEqual((status, body["error"]["code"]), (503, "payment_unavailable"), body)
		self.assertFalse(
			frappe.db.exists("Sales Invoice", {"customer": self._erp_customer(), "docstatus": 1})
		)

	@contextmanager
	def _print_view(self):
		"""What Frappe's print view needs in a test. CI's bench has no built assets (bench init
		--skip-assets), so include_style('print.bundle.css') would find no assets.json; and the website
		router reads the request's environ. Yields the request."""
		with patch("frappe.utils.get_assets_json", return_value={}):
			request = EnvironBuilder(
				path="/api/method/growthx_core.api.v1.customer.invoice_pdf"
			).get_request()
			previous = getattr(frappe.local, "request", None)
			frappe.local.request = request
			try:
				yield request
			finally:
				frappe.local.request = previous

	def _pdf(self, invoice, user=None):
		frappe.set_user(user or self.user)
		try:
			with (
				patch("frappe.utils.pdf.get_pdf", side_effect=lambda html, *a, **k: html.encode()) as get_pdf,
				self._print_view() as request,
			):
				status, res = _call(customer_api.invoice_pdf, {"id": invoice}, request=request)
			return status, res, get_pdf
		finally:
			frappe.set_user("Administrator")

	def _print_html(self, invoice, print_format):
		with payu._as_administrator(), self._print_view():
			return frappe.get_print("Sales Invoice", invoice, print_format)

	def test_invoice_pdf_is_the_gst_tax_invoice(self):
		self._billing(gstin=KERALA_GSTIN)
		# as on core-test: a default letterhead (ERPNext's setup wizard makes one), no company logo, and
		# headers repeated on every PDF page
		frappe.get_doc(
			{
				"doctype": "Letter Head",
				"letter_head_name": "GX Test Letterhead",
				"source": "HTML",
				"content": "<p>GX-LETTERHEAD-MARK</p>",
				"is_default": 1,
			}
		).insert()
		frappe.db.set_value("Company", COMPANY, "company_logo", None)
		frappe.db.set_single_value("Print Settings", "repeat_header_footer", 1)
		self.addCleanup(frappe.clear_document_cache, "Print Settings", "Print Settings")  # rolled back
		_, body = self._checkout()
		si = self._invoice(body)
		status, res, _ = self._pdf(si.name)
		self.assertEqual(status, 200, _last_error())
		self.assertEqual(res.mimetype, "application/pdf")
		self.assertEqual(
			res.headers["Content-Disposition"], f'attachment; filename="{si.name.replace("/", "-")}.pdf"'
		)
		html = res.get_data(as_text=True)

		# the supplier: our name, address, state with its code, and GSTIN, at the top
		seller = html[html.index('class="gx-seller"') : html.index('class="gx-title"')]
		for text in (
			COMPANY,
			"Hilite Business Park",
			"Calicut",
			"Kerala, State Code: 32",
			"PIN Code: 673014",
			"GSTIN: 32AAVFT5109L1ZQ",
		):
			self.assertIn(text, seller)
		# the recipient, and the GST against its amount
		row = '{}</td> <td class="gx-num">{}'.format
		inr = lambda amount: fmt_money(amount, currency="INR")  # noqa: E731
		for text in (
			si.name,
			"Checkout Solar",
			f"GSTIN: {KERALA_GSTIN}",
			"32-Kerala",
			"998315",
			row("Taxable value", inr(127118.64)),
			row("CGST @ 9%", inr(11440.68)),
			row("SGST @ 9%", inr(11440.68)),
			row("Total", inr(150000)),
			"Amount due",
		):
			self.assertIn(text, " ".join(html.split()))
		# CGST before SGST, the usual order (India Compliance's In-State template lists SGST first)
		self.assertLess(html.index("CGST @ 9%"), html.index("SGST @ 9%"))
		self.assertEqual(
			[t.gst_tax_type for t in si.taxes], ["sgst", "cgst"], "the template's order changed; drop this"
		)
		# one heading, in the page: no letterhead, logo, or page header that wkhtmltopdf would repeat.
		# (Counted in the format's own HTML: Frappe v16.35's print view also names the format,
		# "GrowthX Tax Invoice", in its "Get PDF" link, which isn't printed.)
		invoice = html[html.index('<div class="gx-invoice">') :]
		self.assertEqual(invoice.count("Tax Invoice"), 1, invoice)
		for text in (
			"GX-LETTERHEAD-MARK",
			'class="letter-head"',
			'class="print-heading"',
			"<img",
			'id="header-html"',
			'id="footer-html"',
		):
			self.assertNotIn(text, html)
		# (India Compliance's format, by contrast, puts its heading in the repeated page header)
		self.assertIn('id="header-html"', self._print_html(si.name, "GST Tax Invoice"))

		self._pay(si.name)
		status, res, _ = self._pdf(si.name)
		self.assertEqual(status, 200)
		self.assertIn("Paid", res.get_data(as_text=True))
		self.assertNotIn("Amount due", res.get_data(as_text=True))

	def test_invoice_pdf_escapes_the_customer_name(self):
		# sign-up and billing details refuse < and >; an account from before they did
		_, body = self._checkout()
		si = self._invoice(body)
		frappe.db.set_value("Sales Invoice", si.name, "customer_name", "<i>Solar</i> & Co")
		status, res, _ = self._pdf(si.name)
		self.assertEqual(status, 200, _last_error())
		html = res.get_data(as_text=True)
		self.assertIn("&lt;i&gt;Solar&lt;/i&gt; &amp; Co", html)
		self.assertNotIn("<i>Solar", html)

	def test_invoice_pdf_only_for_the_customers_issued_invoices(self):
		_, body = self._checkout()
		si = self._invoice(body)
		other, _ = self._customer("gx-checkout-pdf@example.com", "Other Solar")
		status, res, get_pdf = self._pdf(si.name, user=other)
		self.assertEqual((status, res["error"]["code"]), (404, "not_found"))
		get_pdf.assert_not_called()
		self.assertEqual(self._pdf("GX/00-00/9999")[0], 404)
		self.assertEqual(self._pdf("")[0], 422)

		with payu._as_administrator():
			frappe.get_doc("Payment Request", self._checkout_docs(body["reference"]).payment_request).cancel()
			si.reload()
			si.cancel()
		self.assertEqual(self._pdf(si.name)[0], 404)  # void
