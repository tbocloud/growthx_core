"""Online payment with PayU, through the frappe_payu app (repo tbocloud/payu_billing).

1. `billing.create_checkout`: Core makes a Sales Invoice for the plan and a PayU Payment Request for it
   (no email), starts a payment attempt and returns PayU's form. The website posts it to PayU.
2. PayU sends the customer back through frappe_payu's payment_response. The app checks the payment
   with PayU's verify API, marks the Payment Request paid (a Payment Entry against the invoice) and
   sends the customer to the website's billing page with `?payu_txnid=…` (PayU Settings: "After-payment
   Link" + "Return Customers Straight to the After-payment Link"). If the customer never comes back,
   the PayU webhook and the app's 15-minute check do the same.
3. `on_payment_entry_submit` (doc_events): the invoice of a GX Checkout is settled, so the customer's
   subscription becomes active, exactly as with admin.record_payment.
4. `billing.checkout_status`: the billing page asks for the result.

The amount always comes from the plan, on the server, and the invoice is a GST tax invoice for the
customer's billing details (growthx_core.billing.tax_invoice). site_config:
    "growthx_payu_gateway_account": the PayU Payment Gateway Account, e.g. "PayU-core-test - INR - TBO".
        Without it (or without frappe_payu) checkout answers 503 payment_unavailable: online payment off.
"""

import inspect
from urllib.parse import parse_qs, urlsplit

import frappe
from frappe.utils import add_to_date, flt, get_datetime, getdate, now_datetime, nowdate

from growthx_core.api.v1._core import ApiError, audit
from growthx_core.billing import payments, tax_invoice
from growthx_core.partners import commissions

GATEWAY = "payu"
PAYABLE_PR_STATUSES = ("Initiated", "Requested")
# While a PayU attempt may still succeed, its invoice and Payment Request must not be cancelled.
# frappe_payu records a payment against a cancelled Payment Request without a Payment Entry (and still
# mails the customer a receipt), so the money would be taken with nothing to show for it.
# Authorized: PayU has confirmed the money and frappe_payu retries completing the Payment Request
# hourly, so it blocks at any age. Queued: the customer may still be on PayU's page; after
# IN_FLIGHT_MINUTES it counts as abandoned.
AUTHORIZED_ATTEMPT_STATUS = "Authorized"
QUEUED_ATTEMPT_STATUS = "Queued"
IN_FLIGHT_MINUTES = 30


class Unavailable(Exception):
	"""Online payment is off or not set up; the reason goes to the Error Log, not to the customer."""


# ---------------------------------------------------------------- configuration


def gateway_account() -> str:
	name = frappe.conf.get("growthx_payu_gateway_account")
	if not name:
		raise Unavailable("growthx_payu_gateway_account is not set in site_config")
	if "frappe_payu" not in frappe.get_installed_apps():
		raise Unavailable("the frappe_payu app is not installed")
	if not frappe.db.exists("Payment Gateway Account", name):
		raise Unavailable(f"Payment Gateway Account {name} does not exist")
	return name


def company() -> str:
	name = frappe.conf.get("growthx_company") or frappe.defaults.get_global_default("company")
	if not name or not frappe.db.exists("Company", name):
		raise Unavailable("no default Company (set growthx_company in site_config)")
	return name


# ---------------------------------------------------------------- checkout


def create_checkout(account, plan):
	"""Find or make the open checkout for this account and plan, start a new PayU attempt and return
	(checkout, {"url", "params"}). Each step can resume: frappe_payu commits when the Payment Request
	gets its link, so a call that fails half-way leaves a checkout the next call picks up."""
	account_name = account.name
	payments.check_payable(account, plan)
	gateway = gateway_account()
	if not plan.item:
		raise Unavailable(f"GX Plan {plan.name} has no Item for its invoices")
	ensure_customer(account)
	buyer = tax_invoice.buyer(account, company())

	checkout = _open_checkout(account_name, plan, tax_invoice.buyer_snapshot(buyer))
	try:
		if not checkout.sales_invoice:
			invoice = _make_invoice(account, plan, buyer)
			checkout.db_set({"sales_invoice": invoice.name, "amount": invoice.grand_total})
		if not checkout.payment_request:
			checkout.db_set("payment_request", _payment_request(checkout.sales_invoice, account, gateway))
		elif authorized_attempt(checkout.payment_request):
			# PayU already has the money for this checkout; a new attempt would let them pay twice.
			# (A recent Queued attempt is fine: that's the customer retrying on PayU's page.)
			raise _payment_in_progress()
		return checkout, start_attempt(checkout.payment_request)
	except (frappe.ValidationError, frappe.PermissionError) as e:
		# ERPNext or PayU set-up problems (no price list, no fiscal year, a refused link...): not
		# something the customer can fix, and their messages name internal fields.
		raise Unavailable(f"checkout {checkout.name}: {e}\n{frappe.get_traceback()}")


def _open_checkout(account_name: str, plan, buyer: tuple):
	"""The pending checkout for this plan if it can still be paid; older pending checkouts for the
	product (another plan, an old price, an invoice for other billing details or from before GST
	invoicing) are cancelled. Locks the account first, so two checkouts started at the same time
	don't make two invoices."""
	frappe.db.get_value("GX Account", account_name, "name", for_update=True)
	found = None
	for row in frappe.get_all(
		"GX Checkout",
		{"account": account_name, "product": plan.product, "status": "pending"},
		["name", "plan", "sales_invoice", "payment_request"],
		order_by="creation desc",
	):
		if not found and row.plan == plan.name and _still_payable(row, plan, buyer):
			found = row.name
		else:
			_cancel(row)
	if found:
		return frappe.get_doc("GX Checkout", found)
	return frappe.get_doc(
		{
			"doctype": "GX Checkout",
			"account": account_name,
			"plan": plan.name,
			"subscription": frappe.db.get_value(
				"GX Subscription",
				{"account": account_name, "product": plan.product},
				"name",
				order_by="creation desc",
			),
			"status": "pending",
		}
	).insert(ignore_permissions=True)


def _still_payable(row, plan, buyer: tuple) -> bool:
	if not row.sales_invoice:
		return True
	si = frappe.db.get_value(
		"Sales Invoice", row.sales_invoice, ["docstatus", "outstanding_amount", "grand_total"], as_dict=True
	)
	if not si or si.docstatus != 1 or flt(si.outstanding_amount) != flt(si.grand_total):
		return False  # cancelled, or (part) paid: not something to take another payment for
	rate = frappe.db.get_value("Sales Invoice Item", {"parent": row.sales_invoice}, "rate")
	if flt(rate) != flt(plan.price):
		return False  # the plan's price changed since
	if tax_invoice.invoice_snapshot(row.sales_invoice) != buyer:
		return False  # pre-GST, or the billing name, address, state or GSTIN changed since
	if row.payment_request:
		status = frappe.db.get_value("Payment Request", row.payment_request, ["docstatus", "status"])
		if not status or status[0] != 1 or status[1] not in PAYABLE_PR_STATUSES:
			return False
	return True


def _cancel(row):
	"""Cancel an unpaid checkout's Payment Request and invoice. A checkout whose invoice already has a
	payment against it is never replaced (that could charge the customer twice): 409 instead."""
	si = None
	if row.sales_invoice:
		si = frappe.get_doc("Sales Invoice", row.sales_invoice)
		if si.docstatus == 1 and flt(si.outstanding_amount) != flt(si.grand_total):
			raise _payment_in_progress()
	if row.payment_request and attempt_in_flight(row.payment_request):
		raise _payment_in_progress()
	with _as_administrator():
		if row.payment_request:
			pr = frappe.get_doc("Payment Request", row.payment_request)
			if pr.docstatus == 1 and pr.status in PAYABLE_PR_STATUSES:
				pr.cancel()
		if si and si.docstatus == 1:
			si.cancel()
	frappe.db.set_value("GX Checkout", row.name, "status", "cancelled")


def _payment_in_progress():
	return ApiError(
		409,
		"payment_in_progress",
		"A payment for this plan is already being processed. Please try again in a few minutes, or contact support.",
	)


def cancel_open_checkouts(account_name: str, product: str):
	"""Before a payment is recorded by hand (admin.record_payment): cancel the account's pending
	checkouts for the product, so the customer can't also pay online. Same rule as a new checkout:
	409 payment_in_progress if one is part-paid or has a PayU attempt that may still succeed."""
	frappe.db.get_value("GX Account", account_name, "name", for_update=True)
	for row in frappe.get_all(
		"GX Checkout",
		{"account": account_name, "product": product, "status": "pending"},
		["name", "plan", "sales_invoice", "payment_request"],
	):
		_cancel(row)


def _make_invoice(account, plan, buyer):
	"""The plan's GST tax invoice (submitted), for the buyer from tax_invoice.buyer()."""
	product_name = frappe.db.get_value("GX Product", plan.product, "product_name")
	customer = ensure_customer(account)
	seller = company()
	tax_invoice.sync_customer_name(customer, buyer)
	with _as_administrator():
		si = frappe.get_doc(
			{
				"doctype": "Sales Invoice",
				"customer": customer,
				"company": seller,
				"posting_date": nowdate(),
				"due_date": nowdate(),
				"items": [
					{
						"item_code": plan.item,
						"qty": 1,
						"rate": plan.price,
						"description": f"{product_name}, {plan.plan_name} plan",
					}
				],
				"remarks": f"GrowthX {product_name} {plan.plan_name}, paid online (PayU)",
			}
		)
		tax_invoice.apply(si, buyer, plan, seller)
		si.insert(ignore_permissions=True)
		tax_invoice.check(si, buyer, plan)
		si.submit()
	return si


def ensure_customer(account) -> str:
	"""The ERPNext Customer for the account. Accounts made before sign-up created one get it now."""
	if account.customer and frappe.db.exists("Customer", account.customer):
		return account.customer
	from growthx_core.setup.defaults import default_link

	customer = frappe.get_doc(
		{
			"doctype": "Customer",
			"customer_name": account.account_name,
			"customer_type": "Company",
			"customer_group": default_link("Customer Group"),
			"territory": default_link("Territory"),
			"email_id": account.contact_email,
			"mobile_no": account.phone,
		}
	).insert(ignore_permissions=True)
	frappe.db.set_value("GX Account", account.name, "customer", customer.name)
	account.customer = customer.name
	return customer.name


def _payment_request(invoice: str, account, gateway: str) -> str:
	existing = frappe.db.get_value(
		"Payment Request",
		{
			"reference_doctype": "Sales Invoice",
			"reference_name": invoice,
			"docstatus": 1,
			"status": ("in", PAYABLE_PR_STATUSES),
		},
	)
	if existing:
		return existing
	from erpnext.accounts.doctype.payment_request.payment_request import make_payment_request

	with _as_administrator():
		pr = make_payment_request(
			dt="Sales Invoice",
			dn=invoice,
			recipient_id=account.contact_email or frappe.db.get_value("GX Account", account.name, "user"),
			payment_gateway_account=gateway,
			mute_email=1,  # the customer pays from the dashboard, never from a mailed link
			submit_doc=1,
			return_doc=1,
		)
	return pr.name


def start_attempt(payment_request: str) -> dict:
	"""A new PayU attempt (txnid) for the Payment Request's link: {"url": PayU's page, "params": form}.
	Calls frappe_payu's start_payment without its per-IP limit: every customer reaches Core from the
	website server's address. billing.create_checkout has its own per-user limit."""
	from frappe_payu import gateway as payu_gateway

	url = frappe.db.get_value("Payment Request", payment_request, "payment_url") or ""
	token = (parse_qs(urlsplit(url).query).get("token") or [None])[0]
	if not token:
		raise Unavailable(f"Payment Request {payment_request} has no PayU payment link")
	return inspect.unwrap(payu_gateway.start_payment)(token)


# ---------------------------------------------------------------- status


def checkout_for_txnid(txnid: str) -> str | None:
	"""The GX Checkout a PayU attempt (txnid) belongs to, if any."""
	if not txnid or len(txnid) > 40:
		return None
	row = frappe.db.get_value(
		"Integration Request",
		{"name": txnid, "integration_request_service": "PayU"},
		["reference_doctype", "reference_docname", "data"],
		as_dict=True,
	)
	if not row:
		return None
	pr = row.reference_docname if row.reference_doctype == "Payment Request" else None
	if not pr:
		data = frappe.parse_json(row.data or "{}") or {}
		pr = data.get("reference_docname") if data.get("reference_doctype") == "Payment Request" else None
	return frappe.db.get_value("GX Checkout", {"payment_request": pr}) if pr else None


def last_attempt_status(payment_request: str) -> str | None:
	"""Status of the newest PayU attempt for the Payment Request: Queued, Authorized, Completed,
	Failed or Cancelled (frappe_payu's Integration Request statuses)."""
	attempt = _last_attempt(payment_request)
	return attempt.status if attempt else None


def attempt_in_flight(payment_request: str) -> bool:
	"""A PayU payment for the Payment Request may still go through: any attempt is Authorized (PayU has
	the money; frappe_payu keeps retrying the Payment Request, whatever came after it), or the newest is
	Queued and under IN_FLIGHT_MINUTES old (the customer may still be on PayU's page)."""
	if authorized_attempt(payment_request):
		return True
	attempt = _last_attempt(payment_request)
	return bool(
		attempt
		and attempt.status == QUEUED_ATTEMPT_STATUS
		and get_datetime(attempt.creation) > add_to_date(now_datetime(), minutes=-IN_FLIGHT_MINUTES)
	)


def authorized_attempt(payment_request: str) -> bool:
	"""Any attempt on the Payment Request's PayU link is Authorized, not only the newest."""
	links = _links(payment_request)
	return bool(
		links
		and frappe.db.exists(
			"Integration Request",
			{
				"integration_request_service": "PayU",
				"request_id": ("in", links),
				"request_description": "PayU payment attempt",
				"status": AUTHORIZED_ATTEMPT_STATUS,
			},
		)
	)


def _links(payment_request: str) -> list[str]:
	return frappe.get_all(
		"Integration Request",
		{
			"integration_request_service": "PayU",
			"reference_doctype": "Payment Request",
			"reference_docname": payment_request,
			"request_description": "PayU payment link",
		},
		pluck="name",
		order_by="creation desc",
	)


def _last_attempt(payment_request: str):
	links = _links(payment_request)
	if not links:
		return None
	return frappe.db.get_value(
		"Integration Request",
		{
			"integration_request_service": "PayU",
			"request_id": links[0],
			"request_description": "PayU payment attempt",
		},
		["status", "creation"],
		as_dict=True,
		order_by="creation desc",
	)


# ---------------------------------------------------------------- activation


def on_payment_entry_submit(doc, method=None):
	"""doc_events: a payment settled the invoice of a pending checkout, so its plan becomes active.
	Runs for PayU payments and for a Payment Entry an accountant makes by hand. Never raises: the
	money is received either way; if the subscription can't be activated the checkout says why
	(needs_attention) and an admin uses Record payment."""
	invoices = {r.reference_name for r in doc.references if r.reference_doctype == "Sales Invoice"}
	for invoice in invoices:
		name = frappe.db.get_value(
			"GX Checkout", {"sales_invoice": invoice, "status": ("in", ("pending", "cancelled"))}
		)
		if name:
			_activate(name, doc.name, getdate(doc.posting_date))


def on_payment_entry_cancel(doc, method=None):
	"""doc_events: a Payment Entry that activated (or tried to activate) a checkout was cancelled, e.g.
	a reversal or refund. The plan stays as it is (no automatic suspension); the checkout becomes
	needs_attention and the audit log says so, for an admin to decide. Never raises."""
	for name in frappe.get_all(
		"GX Checkout",
		{"payment_entry": doc.name, "status": ("in", ("paid", "needs_attention"))},
		pluck="name",
	):
		try:
			checkout = frappe.get_doc("GX Checkout", name, for_update=True)
			_void_commission(checkout, doc.name)
			plan_state = (
				"The plan it activated is still active"
				if checkout.status == "paid"
				else "The plan had not been activated"
			)
			checkout.db_set(
				{
					"status": "needs_attention",
					"error": f"Payment Entry {doc.name} was cancelled. {plan_state}: check the payment, then"
					" suspend the customer or record a new payment if needed.",
				}
			)
			audit(
				"customer.online_payment_cancelled",
				"GX Account",
				checkout.account,
				f"Payment Entry {doc.name} cancelled for checkout {checkout.name}, invoice"
				f" {checkout.sales_invoice}. {plan_state}; needs a decision",
			)
		except Exception:
			frappe.log_error(title=f"GrowthX: could not flag checkout {name} after Payment Entry cancel")


def _void_commission(checkout, payment_entry: str):
	"""The money came back, so the partner's commission on it is void. One already paid out can't be
	voided here: the audit log asks an admin to take it off the partner's next payout."""
	reason = f"Payment Entry {payment_entry} was cancelled (refund or reversal)."
	voided = commissions.void_for_payment(checkout.account, checkout.name, reason)
	key = commissions.payment_key(checkout.account, checkout.name)
	paid = frappe.db.get_value("GX Commission", {"payment_key": key, "status": "paid"})
	if voided or paid:
		audit(
			"commission.void_on_refund",
			"GX Commission",
			(voided or [paid])[0],
			f"{reason} "
			+ ("Commission voided." if voided else "Commission was already paid out: claw it back."),
		)


def _activate(checkout_name: str, payment_entry: str, paid_on):
	checkout = frappe.get_doc("GX Checkout", checkout_name, for_update=True)
	if flt(frappe.db.get_value("Sales Invoice", checkout.sales_invoice, "outstanding_amount")) > 0:
		return  # part payment: wait for the rest
	frappe.db.savepoint("gx_checkout_activate")
	try:
		account = frappe.get_doc("GX Account", checkout.account, for_update=True)
		plan = frappe.get_doc("GX Plan", checkout.plan)
		_sub, details, period_end = payments.apply_payment(
			account,
			plan,
			checkout.name,
			paid_on,
			GATEWAY,
			amount=checkout.amount,
			sales_invoice=checkout.sales_invoice,
		)
		audit(
			"customer.online_payment",
			"GX Account",
			account.name,
			f"{details}: {frappe.utils.fmt_money(checkout.amount, currency='INR')} for {plan.plan_name}"
			f" paid online (PayU), invoice {checkout.sales_invoice}, checkout {checkout.name},"
			f" active {f'until {period_end.isoformat()}' if period_end else 'for life'}",
			actor="Administrator",
		)
		checkout.db_set(
			{"status": "paid", "paid_at": now_datetime(), "payment_entry": payment_entry, "error": None}
		)
	except Exception as e:
		frappe.db.rollback(save_point="gx_checkout_activate")
		message = e.message if isinstance(e, ApiError) else str(e) or type(e).__name__
		frappe.log_error(title=f"GrowthX: checkout {checkout_name} paid but not activated")
		checkout.db_set(
			{
				"status": "needs_attention",
				"paid_at": now_datetime(),
				"payment_entry": payment_entry,
				"error": f"Paid, but the plan was not activated: {message}",
			}
		)


# ---------------------------------------------------------------- helpers


class _as_administrator:
	"""ERPNext checks the session user's permissions on invoices and payment requests; Core's
	callers are customers, who have none in ERPNext. frappe.set_user also clears the session data and
	the request's form_dict, so both are put back afterwards."""

	def __enter__(self):
		self.session = frappe._dict(frappe.local.session)
		self.form_dict = frappe.local.form_dict
		frappe.set_user("Administrator")

	def __exit__(self, *exc):
		frappe.set_user(self.session.user)
		frappe.local.session.update(self.session)
		frappe.local.form_dict = self.form_dict
