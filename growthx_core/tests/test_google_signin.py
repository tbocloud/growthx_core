"""Sign in with Google: auth.google and auth.signup with a Google sign-up token. Google's token
endpoint is mocked."""

import base64
import json
import time
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from growthx_core import accounts, google_signin
from growthx_core.api.v1 import _core, auth

CLIENT_ID = "test-client.apps.googleusercontent.com"
CONF = {
	"google_oauth_client_id": CLIENT_ID,
	"google_oauth_client_secret": "test-secret",
	"google_oauth_redirect_uri": "http://localhost:3000/api/auth/google/callback",
}
EXISTING = "gx-google-existing@example.com"
NEW = "gx-google-new@example.com"


def _call(fn, data=None):
	frappe.local.form_dict = frappe._dict(data or {})
	frappe.local.request = None
	frappe.local.request_ip = "127.0.0.1"
	with patch.object(_core, "_idempotency_key", return_value=None):
		response = fn()
	return response.status_code, json.loads(response.get_data())


def _id_token(**claims):
	claims = {
		"iss": "https://accounts.google.com",
		"aud": CLIENT_ID,
		"sub": "1234567890",
		"email": NEW,
		"email_verified": True,
		"name": "Noor Google",
		"exp": int(time.time()) + 3600,
		**claims,
	}
	part = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
	return f"{part({'alg': 'RS256'})}.{part(claims)}.signature"


def _google(status=200, **claims):
	response = MagicMock(status_code=status)
	response.json.return_value = (
		{"id_token": _id_token(**claims), "access_token": "x"}
		if status == 200
		else {"error": "invalid_grant"}
	)
	return patch.object(google_signin.requests, "post", return_value=response)


class TestGoogleSignin(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.conf = patch.dict(frappe.conf, CONF)
		self.conf.start()
		accounts.create_partner_account(
			email=EXISTING, full_name="Existing Partner", password="Long-Enough-Pass-91"
		)
		frappe.set_user("Guest")

	def tearDown(self):
		self.conf.stop()
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def test_existing_account_is_logged_in(self):
		with _google(email=EXISTING.upper()) as post:
			status, body = _call(auth.google, {"code": "4/abc", "codeVerifier": "v" * 43})
		self.assertEqual(status, 200)
		self.assertTrue(body["accessToken"].startswith("gxa_"))
		self.assertEqual(body["user"]["email"], EXISTING)
		sent = post.call_args.kwargs["data"]
		self.assertEqual(sent["redirect_uri"], CONF["google_oauth_redirect_uri"])
		self.assertEqual((sent["code"], sent["code_verifier"]), ("4/abc", "v" * 43))

	def test_new_email_signs_up_with_the_token_and_no_password(self):
		with _google():
			status, body = _call(auth.google, {"code": "4/abc"})
		self.assertEqual(status, 202)
		self.assertEqual((body["email"], body["name"], body["signupRequired"]), (NEW, "Noor Google", True))

		token = body["signupToken"]
		status, body = _call(
			auth.signup,
			# The email in the body is ignored: the account gets the one Google verified.
			{"accountType": "partner", "googleSignupToken": token, "email": "other@example.com"},
		)
		self.assertEqual(status, 201, body)
		self.assertEqual(body["user"]["email"], NEW)
		self.assertEqual(frappe.db.get_value("User", {"email": NEW}, "full_name"), "Noor Google")

		# The token works for one account only.
		status, body = _call(auth.signup, {"accountType": "partner", "googleSignupToken": token})
		self.assertEqual((status, body["error"]["code"]), (400, "google_signup_expired"))

	def test_unknown_signup_token(self):
		status, body = _call(auth.signup, {"accountType": "partner", "googleSignupToken": "gxg_nope"})
		self.assertEqual((status, body["error"]["code"]), (400, "google_signup_expired"))

	def test_a_form_error_keeps_the_signup_token(self):
		token = google_signin.issue_signup_token({"email": NEW, "name": "Noor Google", "sub": "1"})
		status, body = _call(auth.signup, {"accountType": "customer", "googleSignupToken": token})
		self.assertEqual((status, list(body["error"]["fields"])), (422, ["company"]))
		self.assertIsNotNone(google_signin.signup_identity(token))

	def test_bad_or_used_code(self):
		with _google(status=400):
			status, body = _call(auth.google, {"code": "4/used"})
		self.assertEqual((status, body["error"]["code"]), (400, "google_failed"))

	def test_token_for_another_client_is_refused(self):
		with _google(aud="someone-else.apps.googleusercontent.com", email=EXISTING):
			status, body = _call(auth.google, {"code": "4/abc"})
		self.assertEqual((status, body["error"]["code"]), (400, "google_failed"))

	def test_unverified_email_is_refused(self):
		with _google(email=EXISTING, email_verified=False):
			status, body = _call(auth.google, {"code": "4/abc"})
		self.assertEqual((status, body["error"]["code"]), (403, "email_not_verified"))

	def test_staff_account_cannot_sign_in(self):
		with _google(email="admin@example.com"):
			frappe.set_user("Administrator")
			if not frappe.db.exists("User", "admin@example.com"):
				frappe.get_doc(
					{"doctype": "User", "email": "admin@example.com", "first_name": "Staff"}
				).insert()
			frappe.set_user("Guest")
			status, body = _call(auth.google, {"code": "4/abc"})
		self.assertEqual((status, body["error"]["code"]), (403, "account_unavailable"))

	def test_not_configured(self):
		with patch.dict(frappe.conf, {"google_oauth_client_secret": None}), _google() as post:
			status, body = _call(auth.google, {"code": "4/abc"})
		self.assertEqual((status, body["error"]["code"]), (503, "google_unavailable"))
		post.assert_not_called()

	def test_default_redirect_uri_is_on_the_website(self):
		with patch.dict(
			frappe.conf, {"google_oauth_redirect_uri": None, "growthx_web_url": "https://tbogrowthx.com"}
		):
			self.assertEqual(google_signin.redirect_uri(), "https://tbogrowthx.com/api/auth/google/callback")
