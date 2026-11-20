"""A customer's billing details for GST tax invoices (customer.billing_details / update_billing_details).

Stored in ERPNext, where India Compliance reads them for the invoice:
- an Address of type Billing linked to the account's Customer: the billing name (address_title), the
  address, state and PIN, and the GSTIN with its GST category;
- the Customer's own gstin and gst_category (Registered Regular with a GSTIN, Unregistered without),
  and its customer_name: the billing name, which ERPNext copies onto each invoice as it's made.

`billingName` null means the account's company name (account_name); it is stored as the address
title either way, and read back as null when it equals the company name.
"""

import re

import frappe

from growthx_core.api.v1._core import ApiError, validation_error

COUNTRY = "India"
GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z0-9]{13}$")
PINCODE_RE = re.compile(r"^[1-9][0-9]{5}$")
MAX = 140
REGISTERED, UNREGISTERED = "Registered Regular", "Unregistered"


def available() -> bool:
	return "india_compliance" in frappe.get_installed_apps()


def unavailable() -> ApiError:
	frappe.log_error(
		title="GrowthX: billing details need India Compliance",
		message="india_compliance isn't installed on this site",
		defer_insert=True,
	)
	return ApiError(
		503, "payment_unavailable", "Billing details can't be saved right now. Please try again later."
	)


def states() -> dict[str, str]:
	"""State or union territory -> its GST code, as India Compliance names them (India only)."""
	from india_compliance.gst_india.constants import STATE_NUMBERS

	return {k: v for k, v in STATE_NUMBERS.items() if k not in ("Other Countries", "Other Territory")}


def address_name(customer: str | None) -> str | None:
	"""The customer's billing Address (the primary one if there are several)."""
	if not customer:
		return None
	rows = frappe.get_all(
		"Address",
		filters=[
			["Dynamic Link", "link_doctype", "=", "Customer"],
			["Dynamic Link", "link_name", "=", customer],
			["Address", "address_type", "=", "Billing"],
			["Address", "disabled", "=", 0],
		],
		fields=["name", "is_primary_address"],
		order_by="is_primary_address desc, creation asc",
		limit=1,
	)
	return rows[0].name if rows else None


def is_complete(account) -> bool:
	return bool(available() and address_name(account.customer))


def get(account) -> dict:
	name = address_name(account.customer) if available() else None
	if not name:
		return {"complete": False, "billingName": None, "address": None, "gstin": None, "gstCategory": None}
	a = frappe.get_doc("Address", name)
	return {
		"complete": True,
		"billingName": a.address_title if a.address_title != account.account_name else None,
		"address": {
			"addressLine1": a.address_line1,
			"addressLine2": a.address_line2 or None,
			"city": a.city,
			"state": a.state,
			"pincode": a.pincode,
			"country": COUNTRY,
		},
		"gstin": a.gstin or None,
		"gstCategory": "registered" if a.gstin else "unregistered",
	}


def save(account, data: dict) -> dict:
	"""Validate and store the details; returns get(). The caller ensures the Customer exists."""
	values = _validate(data)
	billing_name = values.pop("billing_name") or account.account_name
	gst_category = REGISTERED if values["gstin"] else UNREGISTERED
	fields = {
		"address_title": billing_name,
		"address_type": "Billing",
		"country": COUNTRY,
		"gst_category": gst_category,
		"is_primary_address": 1,
		**values,
	}
	from growthx_core.billing.payu import _as_administrator

	# ERPNext's own hooks (e.g. Customer.on_update) check the session user's permissions; customers
	# have none in ERPNext.
	with _as_administrator():
		name = address_name(account.customer)
		if name:
			doc = frappe.get_doc("Address", name)
			doc.update(fields)
			doc.save(ignore_permissions=True)
		else:
			doc = frappe.get_doc(
				{
					"doctype": "Address",
					**fields,
					"links": [{"link_doctype": "Customer", "link_name": account.customer}],
				}
			).insert(ignore_permissions=True)

		customer = frappe.get_doc("Customer", account.customer)
		customer.update(
			{
				"customer_name": billing_name,
				"gstin": values["gstin"],
				"gst_category": gst_category,
				"customer_primary_address": doc.name,
			}
		)
		customer.save(ignore_permissions=True)
	return get(account)


def _validate(data: dict) -> dict:
	"""Every field's problem at once, as 422 validation_failed with `fields`."""
	errors = {}

	def text(key, label, required):
		value = data.get(key)
		if value is None or (isinstance(value, str) and not value.strip()):
			if required:
				errors[key] = f"{label} is required."
			return None
		if not isinstance(value, str):
			errors[key] = f"{label} must be text."
			return None
		value = " ".join(value.split())
		if len(value) > MAX:
			errors[key] = f"{label} must be at most {MAX} characters."
		elif any(c in value for c in "<>"):
			errors[key] = f"{label} can't contain < or >."
		return value

	billing_name = text("billingName", "Name on invoice", False)
	line1 = text("addressLine1", "Address", True)
	line2 = text("addressLine2", "Address line 2", False)
	city = text("city", "City", True)

	state = data.get("state")
	gst_states = states()
	if state not in gst_states:
		errors["state"] = "Choose a state or union territory from the list."
		state = None

	pincode = data.get("pincode")
	if not isinstance(pincode, str) or not PINCODE_RE.match(pincode):
		errors["pincode"] = "Enter the 6-digit PIN code."
	elif state and not _pincode_in_state(pincode, state):
		errors["pincode"] = f"PIN code {pincode} isn't in {state}."

	gstin = data.get("gstin")
	if gstin is not None:
		if not isinstance(gstin, str) or not GSTIN_RE.match(gstin):
			errors["gstin"] = "Enter the 15-character GSTIN in capitals, without spaces."
		elif problem := _gstin_problem(gstin):
			errors["gstin"] = problem
		elif gstin == _seller_gstin():
			# India Compliance won't invoice a company's own GSTIN
			errors["gstin"] = "This is GrowthX's own GSTIN. Enter your company's GSTIN, or leave it empty."
		elif state and gstin[:2] != gst_states[state]:
			registered_in = next((s for s, code in gst_states.items() if code == gstin[:2]), None)
			errors["gstin"] = (
				f"This GSTIN is registered in {registered_in}, not {state}."
				if registered_in
				else f"This GSTIN isn't registered in {state}."
			)

	if errors:
		raise validation_error(errors)
	return {
		"billing_name": billing_name,
		"address_line1": line1,
		"address_line2": line2,
		"city": city,
		"state": state,
		"pincode": pincode,
		"gstin": gstin,
	}


def _pincode_in_state(pincode: str, state: str) -> bool:
	from india_compliance.gst_india.constants import STATE_PINCODE_MAPPING

	ranges = STATE_PINCODE_MAPPING.get(state)
	if not ranges:
		return True
	if isinstance(ranges[0], int):
		ranges = (ranges,)
	first = int(pincode[:3])
	return any(low <= first <= high for low, high in ranges)


def _seller_gstin() -> str:
	from growthx_core.setup import gst

	return gst.SELLER.gstin


def _gstin_problem(gstin: str) -> str | None:
	"""India Compliance's checks: the check digit, and the format of a regular registration."""
	from india_compliance.gst_india.utils import validate_gst_category, validate_gstin

	for check in (lambda: validate_gstin(gstin), lambda: validate_gst_category(REGISTERED, gstin)):
		try:
			check()
		except frappe.ValidationError:
			frappe.clear_messages()
			return "This isn't a valid GSTIN. Check it for typing mistakes."
	return None
