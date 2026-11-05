"""Email verification at sign-up: auth.send_signup_code, and emailCode on auth.signup."""

import json
import re
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from growthx_core import accounts, signup_codes
from growthx_core.api.v1 import _core, auth

EMAIL = "gx-code-test@example.com"


def _call(fn, data):
	frappe.local.form_dict = frappe._dict(data)
	frappe.local.request = None
	frappe.local.request_ip = "127.0.0.1"
	with patch.object(_core, "_idempotency_key", return_value=None), patch.object(_core, "_check_rate_limit"):
		response = fn()
	return response.status_code, json.loads(response.get_data()), response.headers


class TestSignupEmailCode(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Guest")
		signup_codes.use(EMAIL)
		self.sendmail = patch.object(frappe, "sendmail")
		self.mail = self.sendmail.start()

	def tearDown(self):
		self.sendmail.stop()
		signup_codes.use(EMAIL)
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def _send(self, email=EMAIL):
		status, body, headers = _call(auth.send_signup_code, {"email": email, "client": "web"})
		code = None
		if status == 202:
			code = re.search(r"<b>(\d{6})</b>", self.mail.call_args.kwargs["message"]).group(1)
		return status, body, headers, code

	def _signup(self, code, email=EMAIL):
		status, body, _ = _call(
			auth.signup,
			{
				"accountType": "partner",
				"name": "Code Test",
				"email": email,
				"password": "Long-Enough-Pass-91",
				"emailCode": code,
			},
		)
		return status, body

	def test_send_then_sign_up(self):
		status, body, _, code = self._send()
		self.assertEqual(status, 202, body)
		self.assertEqual(set(body), {"expiresAt", "resendAfter"})
		self.assertEqual(self.mail.call_args.kwargs["recipients"], [EMAIL])

		status, body = self._signup(code)
		self.assertEqual(status, 201, body)
		self.assertEqual(body["user"]["email"], EMAIL)
		# Used up with the account.
		self.assertIsNone(signup_codes._get(EMAIL))

	def test_code_is_required_and_six_digits(self):
		self._send()
		for code, expected in (
			(None, "Code is required."),
			("12345", "Enter the 6-digit code from the email."),
		):
			status, body = self._signup(code)
			self.assertEqual((status, body["error"]["fields"]["emailCode"]), (422, expected))

	def test_wrong_code_then_five_wrong_tries_expire_it(self):
		_, _, _, code = self._send()
		wrong = "000000" if code != "000000" else "111111"
		for left in (4, 3, 2, 1):
			status, body = self._signup(wrong)
			self.assertEqual(status, 422)
			self.assertIn(f"{left} tr", body["error"]["fields"]["emailCode"])
		status, body = self._signup(wrong)
		self.assertEqual((status, body["error"]["code"]), (400, "email_code_expired"))
		# Even the right code no longer works.
		status, body = self._signup(code)
		self.assertEqual((status, body["error"]["code"]), (400, "email_code_expired"))

	def test_never_sent_or_expired(self):
		status, body = self._signup("123456")
		self.assertEqual((status, body["error"]["code"]), (400, "email_code_expired"))

		_, _, _, code = self._send()
		entry = signup_codes._get(EMAIL)
		entry["expires"] -= signup_codes.CODE_TTL + 1
		frappe.cache.set_value(signup_codes._key(EMAIL), json.dumps(entry))
		status, body = self._signup(code)
		self.assertEqual((status, body["error"]["code"]), (400, "email_code_expired"))

	def test_resend_waits_60_seconds_and_replaces_the_code(self):
		_, _, _, first = self._send()
		status, body, headers, _ = self._send()
		self.assertEqual((status, body["error"]["code"]), (429, "rate_limited"))
		self.assertTrue(1 <= int(headers["Retry-After"]) <= 61)

		entry = signup_codes._get(EMAIL)
		entry["sent"] -= signup_codes.RESEND_AFTER
		signup_codes._put(EMAIL, entry)
		status, _, _, second = self._send()
		self.assertEqual(status, 202)
		if first != second:
			status, body = self._signup(first)
			self.assertEqual(status, 422)
		status, body = self._signup(second)
		self.assertEqual(status, 201, body)

	def test_taken_email_gets_no_code(self):
		frappe.set_user("Administrator")
		accounts.create_partner_account(email=EMAIL, full_name="Taken", password="Long-Enough-Pass-91")
		frappe.set_user("Guest")
		status, body, _, _ = self._send()
		self.assertEqual(
			(status, body["error"]["code"], list(body["error"]["fields"])), (409, "email_taken", ["email"])
		)
		self.mail.assert_not_called()

	def test_email_that_cannot_be_sent(self):
		self.mail.side_effect = Exception("no outgoing account")
		status, body, _, _ = self._send()
		self.assertEqual((status, body["error"]["code"]), (503, "email_unavailable"))
		self.assertIsNone(signup_codes._get(EMAIL))  # so the user can try again at once

	def test_google_sign_up_needs_no_code(self):
		from growthx_core import google_signin

		token = google_signin.issue_signup_token({"email": EMAIL, "name": "Google User", "sub": "1"})
		status, body, _ = _call(auth.signup, {"accountType": "partner", "googleSignupToken": token})
		self.assertEqual(status, 201, body)

	def test_switch_for_the_rollout(self):
		with patch.dict(frappe.conf, {"growthx_signup_email_code": 0}):
			status, body = self._signup(None)
		self.assertEqual(status, 201, body)
