"""customer.billing_details / update_billing_details: the address and GSTIN on GST tax invoices."""

import unittest
import uuid

import frappe
from frappe.tests import IntegrationTestCase

from growthx_core import accounts
from growthx_core.api.v1 import customer
from growthx_core.billing import billing_details
from growthx_core.tests.test_checkout_payu import IC, _call

KERALA_GSTIN = "32AAVFT5109L1ZQ"
KARNATAKA_GSTIN = "29AABCG1234M1ZK"


def _details(**over):
	data = {
		"billingName": None,
		"addressLine1": "2nd Floor, Lulu Cyber Tower",
		"addressLine2": "Infopark",
		"city": "Kochi",
		"state": "Kerala",
		"pincode": "682042",
		"gstin": None,
	}
	data.update(over)
	return data


@unittest.skipUnless(IC, "needs India Compliance (CI installs it)")
class TestBillingDetails(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.user, _ = accounts.create_customer_account(
			email=f"gx-billing-{uuid.uuid4().hex[:8]}@example.com",
			full_name="Billing Test",
			company="Noor Solar",
			password="Long-Enough-Pass-91",
		)
		self.account = frappe.db.get_value("GX Account", {"user": self.user})

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def _get(self):
		frappe.set_user(self.user)
		try:
			return _call(customer.billing_details)
		finally:
			frappe.set_user("Administrator")

	def _save(self, **over):
		frappe.set_user(self.user)
		try:
			return _call(customer.update_billing_details, _details(**over))
		finally:
			frappe.set_user("Administrator")

	def _customer(self):
		return frappe.db.get_value("GX Account", self.account, "customer")

	def _audit(self):
		return frappe.get_all(
			"GX Audit Log",
			{"reference_name": self.account, "action": "customer.billing_details_updated"},
			pluck="details",
			order_by="creation asc",
		)

	def test_empty_until_saved(self):
		self.assertEqual(
			self._get(),
			(
				200,
				{"complete": False, "billingName": None, "address": None, "gstin": None, "gstCategory": None},
			),
		)

	def test_save_with_a_gstin_registers_the_customer(self):
		status, body = self._save(gstin=KERALA_GSTIN, billingName="Noor Solar Private Limited")
		self.assertEqual(status, 200, body)
		self.assertEqual(
			body,
			{
				"complete": True,
				"billingName": "Noor Solar Private Limited",
				"address": {
					"addressLine1": "2nd Floor, Lulu Cyber Tower",
					"addressLine2": "Infopark",
					"city": "Kochi",
					"state": "Kerala",
					"pincode": "682042",
					"country": "India",
				},
				"gstin": KERALA_GSTIN,
				"gstCategory": "registered",
			},
		)
		self.assertEqual(self._get(), (200, body))
		address = frappe.get_doc("Address", billing_details.address_name(self._customer()))
		self.assertEqual(
			(address.address_type, address.address_title, address.gstin, address.gst_category),
			("Billing", "Noor Solar Private Limited", KERALA_GSTIN, "Registered Regular"),
		)
		self.assertEqual(
			[(lnk.link_doctype, lnk.link_name) for lnk in address.links], [("Customer", self._customer())]
		)
		cust = frappe.db.get_value(
			"Customer", self._customer(), ["gstin", "gst_category", "customer_primary_address"], as_dict=True
		)
		self.assertEqual(
			(cust.gstin, cust.gst_category, cust.customer_primary_address),
			(KERALA_GSTIN, "Registered Regular", address.name),
		)

	def test_saving_again_updates_the_same_address(self):
		self._save(gstin=KERALA_GSTIN)
		first = billing_details.address_name(self._customer())
		status, body = self._save(state="Karnataka", city="Bengaluru", pincode="560001", addressLine2=None)
		self.assertEqual(status, 200, body)
		self.assertEqual(billing_details.address_name(self._customer()), first)
		self.assertEqual(frappe.db.count("Address", {"address_title": "Noor Solar"}), 1)
		self.assertEqual(
			(body["billingName"], body["address"]["state"], body["address"]["addressLine2"]),
			(None, "Karnataka", None),
		)
		self.assertEqual((body["gstin"], body["gstCategory"]), (None, "unregistered"))
		self.assertEqual(
			frappe.db.get_value("Customer", self._customer(), ["gstin", "gst_category"]),
			(None, "Unregistered"),
		)
		# null billingName is the company name on the invoice
		self.assertEqual(frappe.db.get_value("Address", first, "address_title"), "Noor Solar")

	def test_changes_are_audited_once(self):
		self._save()
		self._save()  # nothing changed
		self._save(gstin=KERALA_GSTIN)
		self.assertEqual(
			self._audit(),
			[
				"company name, Kochi, Kerala 682042, GSTIN none",
				"company name, Kochi, Kerala 682042, GSTIN 32AAVFT5109L1ZQ"
				" (was: company name, Kochi, Kerala 682042, GSTIN none)",
			],
		)

	def test_every_problem_is_reported_at_once(self):
		status, body = self._save(
			billingName="<b>Noor</b>",
			addressLine1=" ",
			city=None,
			state="Kerala ",
			pincode="68204",
			gstin="32aavft5109l1zq",
		)
		self.assertEqual((status, body["error"]["code"]), (422, "validation_failed"))
		self.assertEqual(
			set(body["error"]["fields"]), {"billingName", "addressLine1", "city", "state", "pincode", "gstin"}
		)
		self.assertFalse(billing_details.address_name(self._customer()))

	def test_pin_and_gstin_must_belong_to_the_state(self):
		cases = [
			({"pincode": "560001"}, "pincode", "PIN code 560001 isn't in Kerala."),
			({"gstin": KARNATAKA_GSTIN}, "gstin", "This GSTIN is registered in Karnataka, not Kerala."),
			(
				{"gstin": "32AAVFT5109L1ZA"},
				"gstin",
				"This isn't a valid GSTIN. Check it for typing mistakes.",
			),
			(
				{"gstin": "32 AAVFT5109L1ZQ"},
				"gstin",
				"Enter the 15-character GSTIN in capitals, without spaces.",
			),
			({"state": "Other Territory"}, "state", "Choose a state or union territory from the list."),
		]
		for over, field, message in cases:
			status, body = self._save(**over)
			self.assertEqual((status, body["error"]["fields"]), (422, {field: message}), over)
		status, body = self._save(
			state="Karnataka", city="Bengaluru", pincode="560001", gstin=KARNATAKA_GSTIN
		)
		self.assertEqual((status, body["gstCategory"]), (200, "registered"), body)

	def test_customers_only(self):
		admin = f"gx-billing-admin-{uuid.uuid4().hex[:8]}@example.com"
		frappe.get_doc(
			{"doctype": "User", "email": admin, "first_name": "Ad", "send_welcome_email": 0}
		).insert(ignore_permissions=True).add_roles("GrowthX Admin")
		frappe.set_user(admin)
		for fn in (customer.billing_details, customer.update_billing_details):
			self.assertEqual(_call(fn, _details())[0], 403, fn.__name__)
