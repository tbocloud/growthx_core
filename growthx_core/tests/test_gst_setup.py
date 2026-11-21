"""growthx_core.setup.gst: the seller, GST templates, the GX series and plan items (India Compliance)."""

import unittest
from unittest.mock import patch

import frappe
from frappe.model.naming import parse_naming_series
from frappe.tests import IntegrationTestCase
from frappe.utils import nowdate

from growthx_core.setup import gst
from growthx_core.tests.test_checkout_payu import COMPANY, ensure_erpnext_company

IC = "india_compliance" in frappe.get_installed_apps()
PRODUCT = "test-gst"


@unittest.skipUnless(IC, "needs India Compliance (CI installs it)")
class TestGstSetup(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		ensure_erpnext_company()

	def setUp(self):
		frappe.set_user("Administrator")
		self.conf = patch.dict(frappe.local.conf, {"growthx_company": COMPANY})
		self.conf.start()
		# Only this test's plan is active, so plan_items touches nothing else.
		frappe.db.set_value("GX Plan", {"is_active": 1}, "is_active", 0)
		frappe.get_doc(
			{
				"doctype": "GX Product",
				"slug": PRODUCT,
				"product_name": "GST Test",
				"availability": "available",
			}
		).insert()
		frappe.get_doc(
			{
				"doctype": "GX Plan",
				"plan_id": "test-gst-lifetime",
				"product": PRODUCT,
				"plan_name": "Lifetime",
				"billing_interval": "one_time",
				"price": 150000,
				"gst_included": 1,
				"gst_rate": 18,
				"is_active": 1,
			}
		).insert()

	def tearDown(self):
		self.conf.stop()
		frappe.db.rollback()
		frappe.clear_cache(doctype="Sales Invoice")

	def _templates(self, category):
		return frappe.get_all(
			"Sales Taxes and Charges Template",
			{"company": COMPANY, "tax_category": category, "disabled": 0},
			pluck="name",
		)

	def test_sets_up_the_seller_templates_series_and_plan_items(self):
		gst.setup()

		company = frappe.db.get_value("Company", COMPANY, ["gstin", "gst_category"], as_dict=True)
		self.assertEqual((company.gstin, company.gst_category), (gst.SELLER.gstin, "Registered Regular"))
		address = frappe.get_doc("Address", gst._company_address(COMPANY))
		self.assertEqual(
			(address.address_line1, address.city, address.state, address.pincode, address.gstin),
			(gst.SELLER.address_line1, "Calicut", "Kerala", "673014", gst.SELLER.gstin),
		)
		self.assertEqual((address.is_your_company_address, address.address_type), (1, "Billing"))

		for category, accounts in ((gst.IN_STATE, {"CGST", "SGST"}), (gst.OUT_STATE, {"IGST"})):
			doc = frappe.get_doc("Sales Taxes and Charges Template", gst.sales_template(COMPANY, category))
			self.assertEqual({r.account_head.split()[2] for r in doc.taxes}, accounts, category)
			self.assertTrue(all(r.included_in_print_rate for r in doc.taxes), category)

		self.assertEqual(
			[frappe.db.get_single_value("GST Settings", f) for f in gst.GST_SETTINGS_OFF], [0, 0, 0]
		)  # no e-way bills, India Compliance API or e-invoicing
		field = frappe.get_meta("Sales Invoice").get_field("naming_series")
		self.assertEqual((field.default, field.options.split("\n")[0]), (gst.SERIES, gst.SERIES))
		prefix = parse_naming_series(
			"GX/.GXFY./", doc=frappe._dict(doctype="Sales Invoice", company=COMPANY, posting_date=nowdate())
		)
		from erpnext.accounts.utils import get_fiscal_year

		start, end = get_fiscal_year(nowdate(), company=COMPANY)[1:3]
		self.assertEqual(prefix, f"GX/{start.year % 100:02d}-{end.year % 100:02d}/")
		self.assertLessEqual(len(prefix + "10000"), 16)  # GST's limit, even past 9,999 a year
		# The next financial year, once ERPNext has created it: GX/27-28/
		next_year = frappe.get_doc(
			{
				"doctype": "Fiscal Year",
				"year": f"Next GX {end.year}",  # any name: the dates decide
				"year_start_date": frappe.utils.add_days(end, 1),
				"year_end_date": frappe.utils.add_years(end, 1),
			}
		).insert()
		self.assertEqual(
			gst.naming_fiscal_year(frappe._dict(company=COMPANY, posting_date=next_year.year_start_date)),
			f"{end.year % 100:02d}-{(end.year + 1) % 100:02d}",
		)

		item = frappe.get_doc("Item", frappe.db.get_value("GX Plan", "test-gst-lifetime", "item"))
		self.assertEqual(item.gst_hsn_code, "998315")
		self.assertEqual([r.item_tax_template for r in item.taxes], [gst.item_tax_template(COMPANY, 18)])
		self.assertEqual(
			frappe.db.get_value("Item Tax Template", item.taxes[0].item_tax_template, "gst_rate"), 18
		)

	def test_the_india_address_template_prints_the_gstin(self):
		from india_compliance.gst_india.utils import get_data_file_path

		wizard = "{{ address_line1 }}<br>{{ city }}<br>{{ country }}<br>"  # ERPNext's, roughly
		if frappe.db.exists("Address Template", "India"):
			frappe.db.set_value("Address Template", "India", "template", wizard)
		else:
			frappe.get_doc(
				{"doctype": "Address Template", "country": "India", "is_default": 1, "template": wizard}
			).insert()
		gst.setup()
		self.assertEqual(
			frappe.db.get_value("Address Template", "India", "template"),
			frappe.read_file(get_data_file_path("address_template.html")),
		)
		from frappe.contacts.doctype.address.address import render_address

		html = render_address(gst._company_address(COMPANY), check_permissions=False)
		self.assertIn("GSTIN: 32AAVFT5109L1ZQ", html)
		self.assertIn("State Code: 32", html)

	def test_running_again_changes_nothing(self):
		gst.setup()
		counts = lambda: (  # noqa: E731
			frappe.db.count("Address"),
			frappe.db.count("Sales Taxes and Charges Template", {"company": COMPANY}),
			frappe.db.count("Item Tax Template", {"company": COMPANY}),
			frappe.db.count("Property Setter", {"doc_type": "Sales Invoice", "field_name": "naming_series"}),
		)
		modified = lambda: [  # noqa: E731
			frappe.db.get_value("Company", COMPANY, "modified"),
			frappe.db.get_value("Address", gst._company_address(COMPANY), "modified"),
			frappe.db.get_value("Address Template", "India", "modified"),
			frappe.db.get_value(
				"Item", frappe.db.get_value("GX Plan", "test-gst-lifetime", "item"), "modified"
			),
		]
		before = (counts(), modified())
		gst.setup()
		self.assertEqual((counts(), modified()), before)

	def test_a_company_from_before_india_compliance_gets_its_templates(self):
		# core-test and production: ERPNext's wizard ran without India Compliance, so no GST templates.
		for doctype in ("Sales Taxes and Charges Template", "Item Tax Template"):
			for name in frappe.get_all(doctype, {"company": COMPANY}, pluck="name"):
				frappe.delete_doc(doctype, name, force=True)
		with self.assertRaises(frappe.ValidationError):
			gst.sales_template(COMPANY, gst.IN_STATE)
		gst.setup()
		self.assertEqual(len(self._templates(gst.IN_STATE)), 1)
		self.assertEqual(len(self._templates(gst.OUT_STATE)), 1)
		self.assertTrue(gst.item_tax_template(COMPANY, 18))

	def test_an_existing_company_address_is_updated_not_duplicated(self):
		while existing := gst._company_address(COMPANY):  # this test starts from a placeholder only
			frappe.delete_doc("Address", existing, force=True)
		placeholder = frappe.get_doc(
			{
				"doctype": "Address",
				"address_title": "Placeholder",
				"address_type": "Billing",
				"address_line1": "Somewhere",
				"city": "Kochi",
				"state": "Kerala",
				"pincode": "682001",
				"country": "India",
				"is_your_company_address": 1,
				"links": [{"link_doctype": "Company", "link_name": COMPANY}],
			}
		).insert()
		gst.setup()
		self.assertEqual(gst._company_address(COMPANY), placeholder.name)
		self.assertEqual(
			frappe.db.get_value("Address", placeholder.name, ["address_line1", "pincode", "gstin"]),
			(gst.SELLER.address_line1, "673014", gst.SELLER.gstin),
		)

	def test_the_sac_code_comes_from_site_config(self):
		with patch.dict(frappe.local.conf, {"growthx_sac_code": "998316"}):
			gst.setup()
		item = frappe.db.get_value("GX Plan", "test-gst-lifetime", "item")
		self.assertEqual(frappe.db.get_value("Item", item, "gst_hsn_code"), "998316")
		self.assertTrue(frappe.db.exists("GST HSN Code", "998316"))
