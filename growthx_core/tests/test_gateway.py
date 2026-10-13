import hashlib
import hmac
from unittest.mock import patch

from frappe.tests import IntegrationTestCase

from growthx_core.billing import gateway

SECRET = "test_secret"


def _signed(payment_id, subscription_id, secret=SECRET):
	signature = hmac.new(
		secret.encode(), f"{payment_id}|{subscription_id}".encode(), hashlib.sha256
	).hexdigest()
	return {
		"razorpay_payment_id": payment_id,
		"razorpay_subscription_id": subscription_id,
		"razorpay_signature": signature,
	}


@patch.object(gateway, "_keys", return_value=("rzp_test_key", SECRET))
class TestGateway(IntegrationTestCase):
	def test_valid_signature(self, _keys):
		self.assertEqual(gateway.verify_payment("sub_1", _signed("pay_1", "sub_1")), "pay_1")

	def test_wrong_secret_fails(self, _keys):
		with self.assertRaises(gateway.GatewayError):
			gateway.verify_payment("sub_1", _signed("pay_1", "sub_1", secret="other"))

	def test_payment_for_another_checkout_fails(self, _keys):
		with self.assertRaises(gateway.GatewayError):
			gateway.verify_payment("sub_2", _signed("pay_1", "sub_1"))

	def test_missing_fields_fail(self, _keys):
		with self.assertRaises(gateway.GatewayError):
			gateway.verify_payment("sub_1", {})
