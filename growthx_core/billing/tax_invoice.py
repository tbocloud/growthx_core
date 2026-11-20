"""GST tax invoices for plans paid online (India Compliance).

The plan price includes GST, so the invoice total is always the plan price:
₹1,50,000 = ₹1,27,118.64 + ₹22,881.36 GST.

- The seller is the company and its own address, as growthx_core.setup.gst.run sets them up.
- The buyer is the customer's billing details (growthx_core.billing.billing_details): the billing name
  as the invoice's customer name, plus the Billing Address and its GSTIN.
- GST is CGST + SGST (In-State template) when the customer's state is the company's, and IGST
  (Out-State) otherwise. The Item Tax Template for the plan's rate goes on the item row.
- The invoice is refused (Unavailable, so 503 for the customer) when anything is missing, and when the
  total isn't the plan price or the GST isn't the plan's rate. A customer must never be charged a
  different amount, or get an invoice without the GST.

`buyer_snapshot` is what an unpaid invoice must still match (the billing name, place of supply and
GSTIN); otherwise the checkout is replaced.
"""

import frappe
from frappe.utils import cint, flt

PRINT_FORMAT = "GST Tax Invoice"
# GST on each line is rounded to the paisa, so the computed tax may differ from the exact split by a
# paisa or two.
TAX_TOLERANCE = 0.05


def _unavailable(reason: str):
	from growthx_core.billing.payu import Unavailable

	return Unavailable(reason)


def buyer(account, company: str) -> frappe._dict:
	"""Who the invoice is for, and which GST applies, from the account's current billing details."""
	from growthx_core.billing import billing_details
	from growthx_core.setup import gst

	if not billing_details.available():
		raise _unavailable("india_compliance isn't installed: no GST invoices")
	customer_address = billing_details.address_name(account.customer)
	if not customer_address:
		raise _unavailable(f"account {account.name} has no billing address")
	company_address = gst._company_address(company)
	if not company_address:
		raise _unavailable(f"{company} has no company address; run growthx_core.setup.gst.run")

	address = frappe.db.get_value(
		"Address", customer_address, ["address_title", "state", "gstin"], as_dict=True
	)
	company_state = frappe.db.get_value("Address", company_address, "state")
	codes = billing_details.states()
	if address.state not in codes:
		raise _unavailable(f"billing address {customer_address} has an unknown state {address.state!r}")
	tax_category = gst.IN_STATE if address.state == company_state else gst.OUT_STATE
	try:
		template = gst.sales_template(company, tax_category)
	except frappe.ValidationError as e:
		raise _unavailable(str(e)) from e
	return frappe._dict(
		customer_name=address.address_title or account.account_name,
		customer_address=customer_address,
		company_address=company_address,
		gstin=address.gstin or None,
		place_of_supply=f"{codes[address.state]}-{address.state}",
		tax_category=tax_category,
		taxes_and_charges=template,
	)


def buyer_snapshot(details) -> tuple:
	"""What an invoice for this buyer() says about them: billing name, place of supply, GSTIN."""
	return (details.customer_name, details.place_of_supply, details.gstin or None)


def invoice_snapshot(si) -> tuple | None:
	"""The same for a Sales Invoice (a doc or a name); None if it was issued before GST invoicing."""
	if isinstance(si, str):
		si = frappe.db.get_value(
			"Sales Invoice",
			si,
			["customer_name", "place_of_supply", "billing_address_gstin", "taxes_and_charges"],
			as_dict=True,
		)
	if not si or not si.taxes_and_charges:
		return None
	return (si.customer_name, si.place_of_supply, si.billing_address_gstin or None)


def sync_customer_name(customer: str, details):
	"""ERPNext copies the Customer's customer_name onto the invoice; it must be the billing name
	(billing_details.save keeps them equal, this covers details saved before it did)."""
	if frappe.db.get_value("Customer", customer, "customer_name") != details.customer_name:
		frappe.db.set_value("Customer", customer, "customer_name", details.customer_name)


def apply(si, details, plan, company: str):
	"""Set the GST parts of a new (unsaved) Sales Invoice for the plan."""
	from erpnext.controllers.accounts_controller import get_taxes_and_charges

	from growthx_core.setup import gst

	if flt(plan.gst_rate) and not cint(plan.gst_included):
		raise _unavailable(f"plan {plan.name} is priced without GST; invoices need GST-inclusive prices")
	try:
		item_tax_template = gst.item_tax_template(company, flt(plan.gst_rate))
	except frappe.ValidationError as e:
		raise _unavailable(str(e)) from e

	si.update(
		{
			"customer_address": details.customer_address,
			"company_address": details.company_address,
			"tax_category": details.tax_category,
			"taxes_and_charges": details.taxes_and_charges,
		}
	)
	for row in si.items:
		row.item_tax_template = item_tax_template
	si.set("taxes", get_taxes_and_charges("Sales Taxes and Charges Template", details.taxes_and_charges))


def check(si, details, plan):
	"""After insert, before submit: the invoice must be exactly the plan price, with the plan's GST,
	for the buyer it was made for."""
	price = flt(plan.price, 2)
	expected_tax = flt(price - price / (1 + flt(plan.gst_rate) / 100), 2)
	total = flt(si.rounded_total or si.grand_total, 2)
	problems = []
	if flt(si.grand_total, 2) != price or total != price:
		problems.append(f"total {si.grand_total} (rounded {si.rounded_total}) is not the plan price {price}")
	if abs(flt(si.total_taxes_and_charges) - expected_tax) > TAX_TOLERANCE:
		problems.append(
			f"GST {si.total_taxes_and_charges} is not {plan.gst_rate}% of {price} ({expected_tax})"
		)
	if invoice_snapshot(si) != buyer_snapshot(details):
		problems.append(f"buyer {invoice_snapshot(si)} is not {buyer_snapshot(details)}")
	if problems:
		raise _unavailable(f"invoice for plan {plan.name}: " + "; ".join(problems))


def pdf(invoice: str) -> bytes:
	from growthx_core.billing.payu import _as_administrator

	with _as_administrator():
		return frappe.get_print("Sales Invoice", invoice, PRINT_FORMAT, as_pdf=True)


def filename(invoice: str) -> str:
	return invoice.replace("/", "-") + ".pdf"
