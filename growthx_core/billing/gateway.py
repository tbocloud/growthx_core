"""Payment gateway adapter (decision 9 in docs/api-decisions.md: gateway-neutral contract).

Only Razorpay exists today (D5 is still open). Keys come from site_config, never from Git:
    "razorpay_key_id": "rzp_test_...", "razorpay_key_secret": "..."

The webhook (renewals, failures, refunds -> Sales Invoice + Payment Entry) is Phase 3 work and is
not implemented yet. `verify` only gives the customer instant feedback after checkout.
"""

import hashlib
import hmac

import frappe
import requests

RAZORPAY_API = "https://api.razorpay.com/v1"
# How many billing cycles a gateway subscription runs before it needs renewing (~10 years).
TOTAL_COUNT = {"month": 120, "year": 10}


class GatewayUnavailable(Exception):
	pass


class GatewayError(Exception):
	pass


def _keys() -> tuple[str, str]:
	key_id = frappe.conf.get("razorpay_key_id")
	secret = frappe.conf.get("razorpay_key_secret")
	if not key_id or not secret:
		raise GatewayUnavailable("Razorpay keys are not configured (razorpay_key_id / razorpay_key_secret)")
	return key_id, secret


def provider() -> str:
	return "razorpay"


def public_key() -> str:
	return _keys()[0]


def create_subscription(gateway_plan_id: str, interval: str, notes: dict) -> str:
	"""Create a Razorpay subscription; returns its id (the checkout `reference`)."""
	key_id, secret = _keys()
	try:
		response = requests.post(
			f"{RAZORPAY_API}/subscriptions",
			auth=(key_id, secret),
			json={
				"plan_id": gateway_plan_id,
				"total_count": TOTAL_COUNT[interval],
				"customer_notify": 1,
				"notes": notes,
			},
			timeout=20,
		)
	except requests.RequestException as e:
		raise GatewayError(f"Razorpay unreachable: {e}")
	if response.status_code >= 300:
		raise GatewayError(f"Razorpay returned {response.status_code}: {response.text[:500]}")
	return response.json()["id"]


def verify_payment(reference: str, payload: dict) -> str:
	"""Check the checkout signature. Returns the payment id, or raises GatewayError."""
	_, secret = _keys()
	payment_id = payload.get("razorpay_payment_id")
	subscription_id = payload.get("razorpay_subscription_id")
	signature = payload.get("razorpay_signature")
	if not (isinstance(payment_id, str) and isinstance(subscription_id, str) and isinstance(signature, str)):
		raise GatewayError("Missing razorpay_payment_id, razorpay_subscription_id or razorpay_signature")
	if subscription_id != reference:
		raise GatewayError("The payment is for a different checkout")
	expected = hmac.new(
		secret.encode(), f"{payment_id}|{subscription_id}".encode(), hashlib.sha256
	).hexdigest()
	if not hmac.compare_digest(expected, signature):
		raise GatewayError("Signature mismatch")
	return payment_id
