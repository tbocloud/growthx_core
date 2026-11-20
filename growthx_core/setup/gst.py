"""GST set-up for Core's invoices (India Compliance). Safe to run more than once.

    bench --site <core site> execute growthx_core.setup.gst.run

1. The seller: the company's GSTIN and GST category, and its registered address (an Address marked as
   the company's own, with the GSTIN) from SELLER below.
2. The GST accounts and tax templates India Compliance's setup wizard makes. Core and core-test ran
   ERPNext's wizard before India Compliance was installed, so they are created here if missing.
3. The In-State (CGST + SGST) and Out-State (IGST) sales templates are marked "included in print
   rate": plan prices include GST, so ₹1,50,000 is ₹1,27,118.64 + ₹22,881.36 GST.
4. The invoice series GX/26-27/0001, made the Sales Invoice default. .GXFY. (naming_fiscal_year) is
   the financial year from its dates, so the series moves to GX/27-28 in April by itself (ERPNext
   creates the next Fiscal Year). ERPNext's own .TFY. shortens the Fiscal Year's *name* instead, which
   breaks if the year is named differently. GST allows at most 16 characters.
5. Every plan's Item gets the SAC code and the Item Tax Template for its rate (seed.plan_items).
6. GST Settings: e-way bills and India Compliance's API features off. Core sells services only and has
   no India Compliance account, and no e-invoicing (turnover under ₹5 crore).

site_config:
    "growthx_company": the selling Company (default: the global default company).
    "growthx_sac_code": the SAC code on plan items (default 998315, pending the accountant).
"""

import frappe

SELLER = frappe._dict(
	legal_name="TBO TEAM BACK OFFICE INTERNATIONAL LLP",
	gstin="32AAVFT5109L1ZQ",
	gst_category="Registered Regular",
	address_line1="2/1149/I186, 5th Floor, Tower 2, Hilite Business Park",
	address_line2="Thondayad Bypass, Olavanna",
	city="Calicut",
	state="Kerala",
	pincode="673014",
	country="India",
)
SERIES = "GX/.GXFY./.####"
DEFAULT_SAC = "998315"
IN_STATE, OUT_STATE = "In-State", "Out-State"


def run():
	company = setup()
	frappe.db.commit()
	print(f"GST set up for {company}: series {SERIES}, SAC {sac_code()}")


def setup(company: str | None = None) -> str:
	"""Everything above, without committing (tests roll it back)."""
	require_india_compliance()
	from growthx_core.billing import payu
	from growthx_core.setup import seed

	company = company or payu.company()
	_seller(company)
	_tax_templates(company)
	_series()
	_gst_settings()
	seed.plan_items(commit=False)
	return company


def naming_fiscal_year(doc, variable=None) -> str:
	"""naming_series_variables hook: "26-27" for the document's financial year (by posting date)."""
	from erpnext.accounts.utils import get_fiscal_year
	from frappe.utils import getdate

	date = getdate((doc and (doc.get("posting_date") or doc.get("transaction_date"))) or None)
	start, end = get_fiscal_year(date, company=doc and doc.get("company"))[1:3]
	return f"{start.year % 100:02d}-{end.year % 100:02d}"


def require_india_compliance():
	if "india_compliance" not in frappe.get_installed_apps():
		frappe.throw("India Compliance isn't installed on this site; install it first (version-16).")


def sac_code() -> str:
	return str(frappe.conf.get("growthx_sac_code") or DEFAULT_SAC)


def sales_template(company: str, tax_category: str) -> str:
	"""The company's active sales template for the tax category (In-State / Out-State)."""
	names = frappe.get_all(
		"Sales Taxes and Charges Template",
		{"company": company, "tax_category": tax_category, "disabled": 0},
		pluck="name",
	)
	if not names:  # ERPNext allows only one per tax category
		frappe.throw(
			f"{company} has no active {tax_category} Sales Taxes and Charges Template;"
			" run growthx_core.setup.gst.run"
		)
	return names[0]


def item_tax_template(company: str, gst_rate) -> str:
	"""The company's Taxable Item Tax Template for the rate (India Compliance's "GST 18% - <abbr>")."""
	name = frappe.db.get_value(
		"Item Tax Template",
		{"company": company, "gst_rate": gst_rate, "gst_treatment": "Taxable", "disabled": 0},
		"name",
		order_by="creation asc",
	)
	if not name:
		frappe.throw(
			f"{company} has no Taxable Item Tax Template for GST {gst_rate}%; run growthx_core.setup.gst.run"
		)
	return name


# ---------------------------------------------------------------- steps


def _seller(company: str):
	doc = frappe.get_doc("Company", company)
	if (doc.get("gstin"), doc.get("gst_category")) != (SELLER.gstin, SELLER.gst_category):
		doc.gstin = SELLER.gstin
		doc.gst_category = SELLER.gst_category
		doc.save(ignore_permissions=True)

	address = _company_address(company)
	values = {
		"address_title": SELLER.legal_name,
		"address_type": "Billing",
		"address_line1": SELLER.address_line1,
		"address_line2": SELLER.address_line2,
		"city": SELLER.city,
		"state": SELLER.state,
		"pincode": SELLER.pincode,
		"country": SELLER.country,
		"gstin": SELLER.gstin,
		"gst_category": SELLER.gst_category,
		"is_your_company_address": 1,
		"is_primary_address": 1,
	}
	if address:
		doc = frappe.get_doc("Address", address)
		if any(doc.get(k) != v for k, v in values.items()):
			doc.update(values)
			doc.save(ignore_permissions=True)
	else:
		frappe.get_doc(
			{
				"doctype": "Address",
				**values,
				"links": [{"link_doctype": "Company", "link_name": company}],
			}
		).insert(ignore_permissions=True)


def _company_address(company: str) -> str | None:
	"""The company's own billing address: the one with our GSTIN, else its primary/first one."""
	rows = frappe.get_all(
		"Address",
		filters=[
			["Dynamic Link", "link_doctype", "=", "Company"],
			["Dynamic Link", "link_name", "=", company],
			["Address", "is_your_company_address", "=", 1],
		],
		fields=["name", "gstin", "is_primary_address"],
		order_by="creation asc",
	)
	for key in (lambda r: r.gstin == SELLER.gstin, lambda r: r.is_primary_address, lambda r: True):
		for r in rows:
			if key(r):
				return r.name
	return None


def _tax_templates(company: str):
	missing = [
		c
		for c in (IN_STATE, OUT_STATE)
		if not frappe.db.exists(
			"Sales Taxes and Charges Template", {"company": company, "tax_category": c, "disabled": 0}
		)
	]
	if missing or not frappe.db.exists("Item Tax Template", {"company": company, "gst_rate": 18}):
		from india_compliance.gst_india.overrides.company import create_company_fixtures

		create_company_fixtures(company)  # GST accounts + IC's default templates; skips existing ones

	for category in (IN_STATE, OUT_STATE):
		doc = frappe.get_doc("Sales Taxes and Charges Template", sales_template(company, category))
		if any(not row.included_in_print_rate for row in doc.taxes):
			for row in doc.taxes:
				row.included_in_print_rate = 1
			doc.save(ignore_permissions=True)


GST_SETTINGS_OFF = ("enable_e_waybill", "enable_api", "enable_e_invoice")


def _gst_settings():
	settings = frappe.get_single("GST Settings")
	if any(settings.get(f) for f in GST_SETTINGS_OFF):
		for f in GST_SETTINGS_OFF:
			settings.set(f, 0)
		settings.save(ignore_permissions=True)


def _series():
	"""GX/.TFY./.#### first (the default) in Sales Invoice's naming series; the others stay."""
	from frappe.custom.doctype.property_setter.property_setter import make_property_setter

	field = frappe.get_meta("Sales Invoice").get_field("naming_series")
	others = [o for o in (field.options or "").split("\n") if o.strip() and o.strip() != SERIES]
	value = "\n".join([SERIES, *others])
	if field.options != value or field.default != SERIES:
		make_property_setter("Sales Invoice", "naming_series", "default", "", "Text")
		make_property_setter("Sales Invoice", "naming_series", "options", value, "Text")
		make_property_setter("Sales Invoice", "naming_series", "default", SERIES, "Text")
		frappe.clear_cache(doctype="Sales Invoice")
