"""GST tax invoices for payments an admin records (bank transfer): admin.record_payment and
admin.add_direct_site's "already paid offline".

The same tax invoice as an online checkout (tax_invoice: the plan price, GST included, for the customer's
billing details), then a Payment Entry for the amount received, into the company's bank account, with the
transfer reference (UTR) and date. The invoice is then Paid (or Partly Paid, if less arrived).

It is switched on by site_config `growthx_bank_account` (the ERPNext Account bank transfers arrive in, a
Bank or Cash ledger account of the selling company) on a site with India Compliance. Off (not set, or no
India Compliance), nothing changes: the payment is recorded as before and `make` returns (None, None).

On, but something missing: the payment is still recorded, and `make` says why there is no invoice (it goes
in the audit line), so the accountant can raise it by hand:
- `bank_account_invalid`: growthx_bank_account isn't a Bank or Cash ledger account of the selling company;
- `gst_not_set_up`: no company, company address or tax templates (growthx_core.setup.gst.run);
- `no_billing_details`: the customer hasn't saved a billing address (state) for the invoice;
- `plan_has_no_item`: the plan has no billing Item (growthx_core.setup.seed.plan_items);
- `invoice_failed`: anything else; the Error Log has it, and nothing of the invoice is kept.
"""

from datetime import date

import frappe

SAVEPOINT = "gx_manual_invoice"


def make(account, plan, reference: str, amount: float, paid_on: date) -> tuple[str | None, str | None]:
	"""(Sales Invoice, None); (None, why not); or (None, None) when switched off. `amount` is what
	arrived, in rupees. Never raises."""
	from growthx_core.billing import billing_details, payu, tax_invoice

	if not frappe.conf.get("growthx_bank_account") or not billing_details.available():
		return None, None
	if not plan.item:
		return None, "plan_has_no_item"
	try:
		seller = payu.company()
	except payu.Unavailable:
		return None, "gst_not_set_up"
	bank = _bank_account(seller)
	if not bank:
		return None, "bank_account_invalid"
	customer = payu.ensure_customer(account)
	if not billing_details.address_name(customer):
		return None, "no_billing_details"
	try:
		buyer = tax_invoice.buyer(account, seller)
	except payu.Unavailable:
		return None, "gst_not_set_up"

	frappe.db.savepoint(SAVEPOINT)
	try:
		product = frappe.db.get_value("GX Product", plan.product, "product_name")
		invoice = payu._make_invoice(
			account,
			plan,
			buyer,
			remarks=f"GrowthX {product} {plan.plan_name}, paid by bank transfer, reference {reference}",
		)
		_pay(invoice.name, bank, reference, amount, paid_on)
	except Exception:
		frappe.db.rollback(save_point=SAVEPOINT)
		frappe.log_error(title=f"GrowthX: invoice for a recorded payment failed ({account.name})")
		return None, "invoice_failed"
	return invoice.name, None


def _bank_account(company: str) -> str | None:
	"""site_config growthx_bank_account, if it is a ledger Bank or Cash account of `company`."""
	name = frappe.conf.get("growthx_bank_account")
	if not name:
		return None
	row = frappe.db.get_value("Account", name, ["company", "is_group", "account_type"], as_dict=True)
	if not row or row.company != company or row.is_group or row.account_type not in ("Bank", "Cash"):
		return None
	return name


def _pay(invoice: str, bank: str, reference: str, amount: float, paid_on: date):
	from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

	from growthx_core.billing.payu import _as_administrator

	with _as_administrator():
		pe = get_payment_entry("Sales Invoice", invoice, party_amount=amount)
		pe.paid_to = bank
		pe.paid_to_account_currency = frappe.db.get_value("Account", bank, "account_currency")
		pe.reference_no = reference
		pe.reference_date = paid_on
		pe.remarks = f"Bank transfer {reference} on {paid_on.isoformat()}, recorded in GrowthX"
		pe.insert(ignore_permissions=True)
		pe.submit()
	return pe.name
