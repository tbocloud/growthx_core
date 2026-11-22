"""Sign in with Microsoft: auth.microsoft. Microsoft's token endpoint is mocked."""

import base64
import json
import time
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from growthx_core import accounts, microsoft_signin
from growthx_core.api.v1 import _core, auth

TENANT = "11111111-2222-3333-4444-555555555555"
CLIENT_ID = "66666666-7777-8888-9999-000000000000"
CONF = {
	"microsoft_oauth_tenant_id": TENANT,
	"microsoft_oauth_client_id": CLIENT_ID,
	"microsoft_oauth_client_secret": "test-secret",
	"microsoft_oauth_redirect_uri": "http://localhost:3000/api/auth/microsoft/callback",
}
EXISTING = "gx-microsoft-existing@example.com"
NEW = "gx-microsoft-new@example.com"


def _call(fn, data=None):
	frappe.local.form_dict = frappe._dict(data or {})
	frappe.local.request = None
	frappe.local.request_ip = "127.0.0.1"
	with patch.object(_core, "_idempotency_key", return_value=None):
		response = fn()
	return response.status_code, json.loads(response.get_data())


def _id_token(**claims):
	claims = {
		"iss": f"https://login.microsoftonline.com/{TENANT}/v2.0",
		"aud": CLIENT_ID,
		"tid": TENANT,
		"sub": "pairwise-id",
		"preferred_username": NEW,
		"name": "Noor Microsoft",
		"exp": int(time.time()) + 3600,
		**claims,
	}
	part = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
	return f"{part({'alg': 'RS256'})}.{part(claims)}.signature"


def _microsoft(status=200, **claims):
	response = MagicMock(status_code=status)
	response.json.return_value = (
		{"id_token": _id_token(**claims), "access_token": "x"}
		if status == 200
		else {"error": "invalid_grant"}
	)
	return patch.object(microsoft_signin.requests, "post", return_value=response)


class TestMicrosoftSignin(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.conf = patch.dict(frappe.conf, CONF)
		self.conf.start()
		accounts.create_partner_account(email=EXISTING, full_name="Existing Partner", password="x" * 20)
		frappe.set_user("Guest")

	def tearDown(self):
		self.conf.stop()
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def test_existing_account_is_logged_in(self):
		with _microsoft(preferred_username=EXISTING.upper()) as post:
			status, body = _call(auth.microsoft, {"code": "0.Abc", "codeVerifier": "v" * 43})
		self.assertEqual(status, 200, body)
		self.assertTrue(body["accessToken"].startswith("gxa_"))
		self.assertEqual(body["user"]["email"], EXISTING)
		# Linked on first Microsoft sign-in, so the welcome email knows how they sign in.
		self.assertTrue(
			frappe.db.exists("User Social Login", {"parent": EXISTING, "provider": accounts.MICROSOFT})
		)
		sent = post.call_args.kwargs["data"]
		self.assertEqual(sent["redirect_uri"], CONF["microsoft_oauth_redirect_uri"])
		self.assertEqual((sent["code"], sent["code_verifier"]), ("0.Abc", "v" * 43))
		self.assertEqual(post.call_args.args[0], microsoft_signin.token_url(TENANT))

	def test_the_email_claim_is_used_when_there_is_no_preferred_username(self):
		with _microsoft(preferred_username=None, email=EXISTING):
			status, body = _call(auth.microsoft, {"code": "0.Abc"})
		self.assertEqual(status, 200, body)
		self.assertEqual(body["user"]["email"], EXISTING)

	def test_an_unknown_email_does_not_start_a_sign_up(self):
		with _microsoft():
			status, body = _call(auth.microsoft, {"code": "0.Abc"})
		self.assertEqual((status, body["error"]["code"]), (404, "no_account"))
		self.assertFalse(frappe.db.exists("User", {"email": NEW}))

	def test_a_disabled_account_cannot_sign_in(self):
		frappe.set_user("Administrator")
		frappe.db.set_value("User", EXISTING, "enabled", 0)
		frappe.set_user("Guest")
		with _microsoft(preferred_username=EXISTING):
			status, body = _call(auth.microsoft, {"code": "0.Abc"})
		self.assertEqual((status, body["error"]["code"]), (403, "account_unavailable"))

	def test_a_staff_account_cannot_sign_in(self):
		frappe.set_user("Administrator")
		if not frappe.db.exists("User", "microsoft-staff@example.com"):
			frappe.get_doc(
				{"doctype": "User", "email": "microsoft-staff@example.com", "first_name": "Staff"}
			).insert()
		frappe.set_user("Guest")
		with _microsoft(preferred_username="microsoft-staff@example.com"):
			status, body = _call(auth.microsoft, {"code": "0.Abc"})
		self.assertEqual((status, body["error"]["code"]), (403, "account_unavailable"))

	def test_bad_or_used_code(self):
		with _microsoft(status=400):
			status, body = _call(auth.microsoft, {"code": "0.Used"})
		self.assertEqual((status, body["error"]["code"]), (400, "microsoft_failed"))

	def test_token_for_another_app_is_refused(self):
		with _microsoft(aud="another-app-id", preferred_username=EXISTING):
			status, body = _call(auth.microsoft, {"code": "0.Abc"})
		self.assertEqual((status, body["error"]["code"]), (400, "microsoft_failed"))

	def test_token_from_another_directory_is_refused(self):
		other = "99999999-8888-7777-6666-555555555555"
		with _microsoft(
			tid=other, iss=f"https://login.microsoftonline.com/{other}/v2.0", preferred_username=EXISTING
		):
			status, body = _call(auth.microsoft, {"code": "0.Abc"})
		self.assertEqual((status, body["error"]["code"]), (400, "microsoft_failed"))

	def test_a_mismatched_issuer_is_refused(self):
		with _microsoft(iss=f"https://login.microsoftonline.com/{TENANT}/v1.0", preferred_username=EXISTING):
			status, body = _call(auth.microsoft, {"code": "0.Abc"})
		self.assertEqual((status, body["error"]["code"]), (400, "microsoft_failed"))

	def test_organizations_allows_any_work_directory(self):
		with (
			patch.dict(frappe.conf, {"microsoft_oauth_tenant_id": "organizations"}),
			_microsoft(preferred_username=EXISTING),
		):
			status, body = _call(auth.microsoft, {"code": "0.Abc"})
		self.assertEqual(status, 200, body)

	def test_not_configured(self):
		with patch.dict(frappe.conf, {"microsoft_oauth_client_secret": None}), _microsoft() as post:
			status, body = _call(auth.microsoft, {"code": "0.Abc"})
		self.assertEqual((status, body["error"]["code"]), (503, "microsoft_unavailable"))
		post.assert_not_called()

	def test_default_redirect_uri_is_on_the_website(self):
		with patch.dict(
			frappe.conf, {"microsoft_oauth_redirect_uri": None, "growthx_web_url": "https://tbogrowthx.com"}
		):
			self.assertEqual(
				microsoft_signin.redirect_uri(), "https://tbogrowthx.com/api/auth/microsoft/callback"
			)
