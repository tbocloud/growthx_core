"""Sign in with Microsoft: auth.microsoft with one multi-tenant app. Staff directory members become
GrowthX Admins; everyone else is a customer (sign-in or sign-up). Microsoft's token endpoint is mocked."""

import base64
import json
import time
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from growthx_core import accounts, microsoft_signin
from growthx_core.api.v1 import _core, auth

STAFF_TENANT = "11111111-2222-3333-4444-555555555555"
OTHER_TENANT = "99999999-8888-7777-6666-555555555555"
PERSONAL = microsoft_signin.PERSONAL_TENANT
CLIENT_ID = "66666666-7777-8888-9999-000000000000"
CONF = {
	"microsoft_oauth_staff_tenant_id": STAFF_TENANT,
	"microsoft_oauth_tenant_id": None,
	"microsoft_oauth_client_id": CLIENT_ID,
	"microsoft_oauth_client_secret": "test-secret",
	"microsoft_oauth_redirect_uri": "http://localhost:3000/api/auth/microsoft/callback",
}
EXISTING = "gx-microsoft-existing@example.com"
NEW = "gx-microsoft-new@example.com"
STAFF = "gx-staff-person@teambackoffice.com"
OID = "aaaaaaaa-0000-0000-0000-000000000001"


def _call(fn, data=None):
	frappe.local.form_dict = frappe._dict(data or {})
	frappe.local.request = None
	frappe.local.request_ip = "127.0.0.1"
	with patch.object(_core, "_idempotency_key", return_value=None):
		response = fn()
	return response.status_code, json.loads(response.get_data())


def _id_token(**claims):
	tid = claims.get("tid", OTHER_TENANT)
	claims = {
		"iss": f"https://login.microsoftonline.com/{tid}/v2.0",
		"aud": CLIENT_ID,
		"tid": tid,
		"sub": "pairwise-id",
		"oid": OID,
		"email": NEW,
		"name": "Noor Microsoft",
		"exp": int(time.time()) + 3600,
		**claims,
	}
	claims = {k: v for k, v in claims.items() if v is not None}
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


def _staff(**claims):
	return _microsoft(**{"tid": STAFF_TENANT, "email": STAFF, **claims})


def _work(**claims):  # a customer's work account, optional claim xms_edov present
	return _microsoft(tid=OTHER_TENANT, xms_edov=True, **claims)


def _personal(**claims):
	return _microsoft(tid=PERSONAL, **claims)


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

	# ------------------------------------------------------------ the request to Microsoft

	def test_the_code_is_exchanged_on_the_common_authority(self):
		with _personal(email=EXISTING) as post:
			status, body = _call(auth.microsoft, {"code": "0.Abc", "codeVerifier": "v" * 43})
		self.assertEqual(status, 200, body)
		sent = post.call_args.kwargs["data"]
		self.assertEqual(sent["redirect_uri"], CONF["microsoft_oauth_redirect_uri"])
		self.assertEqual((sent["code"], sent["code_verifier"]), ("0.Abc", "v" * 43))
		self.assertEqual(post.call_args.args[0], "https://login.microsoftonline.com/common/oauth2/v2.0/token")

	def test_default_redirect_uri_is_on_the_website(self):
		with patch.dict(
			frappe.conf, {"microsoft_oauth_redirect_uri": None, "growthx_web_url": "https://tbogrowthx.com"}
		):
			self.assertEqual(
				microsoft_signin.redirect_uri(), "https://tbogrowthx.com/api/auth/microsoft/callback"
			)

	def test_not_configured(self):
		with patch.dict(frappe.conf, {"microsoft_oauth_client_secret": None}), _staff() as post:
			status, body = _call(auth.microsoft, {"code": "0.Abc"})
		self.assertEqual((status, body["error"]["code"]), (503, "microsoft_unavailable"))
		post.assert_not_called()

	def test_the_old_tenant_key_is_the_staff_tenant_fallback(self):
		with patch.dict(
			frappe.conf, {"microsoft_oauth_staff_tenant_id": None, "microsoft_oauth_tenant_id": STAFF_TENANT}
		):
			self.assertEqual(microsoft_signin.staff_tenant(), STAFF_TENANT)
		with patch.dict(
			frappe.conf,
			{"microsoft_oauth_staff_tenant_id": None, "microsoft_oauth_tenant_id": "organizations"},
		):
			self.assertIsNone(microsoft_signin.staff_tenant())

	# ------------------------------------------------------------ token checks

	def test_bad_or_used_code(self):
		with _microsoft(status=400):
			status, body = _call(auth.microsoft, {"code": "0.Used"})
		self.assertEqual((status, body["error"]["code"]), (400, "microsoft_failed"))

	def test_a_200_that_is_not_json_is_refused(self):
		response = MagicMock(status_code=200)
		response.json.side_effect = ValueError("no body")
		with patch.object(microsoft_signin.requests, "post", return_value=response):
			status, body = _call(auth.microsoft, {"code": "0.Abc"})
		self.assertEqual((status, body["error"]["code"]), (400, "microsoft_failed"))

	def test_token_for_another_app_is_refused(self):
		with _work(aud="another-app-id"):
			status, body = _call(auth.microsoft, {"code": "0.Abc"})
		self.assertEqual((status, body["error"]["code"]), (400, "microsoft_failed"))

	def test_the_issuer_must_match_the_tokens_own_tid(self):
		with _work(iss=f"https://login.microsoftonline.com/{STAFF_TENANT}/v2.0"):
			status, body = _call(auth.microsoft, {"code": "0.Abc"})
		self.assertEqual((status, body["error"]["code"]), (400, "microsoft_failed"))
		with _staff(iss=f"https://login.microsoftonline.com/{STAFF_TENANT}/v1.0"):
			status, body = _call(auth.microsoft, {"code": "0.Abc"})
		self.assertEqual((status, body["error"]["code"]), (400, "microsoft_failed"))

	def test_an_expired_token_is_refused(self):
		with _work(exp=int(time.time()) - 3600):
			status, body = _call(auth.microsoft, {"code": "0.Abc"})
		self.assertEqual((status, body["error"]["code"]), (400, "microsoft_failed"))

	# ------------------------------------------------------------ staff path

	def test_a_new_staff_member_becomes_an_admin(self):
		with _staff():
			status, body = _call(auth.microsoft, {"code": "0.Abc"})
		self.assertEqual(status, 200, body)
		self.assertEqual((body["user"]["email"], body["user"]["role"]), (STAFF, "admin"))
		self.assertFalse(body["user"]["hasPassword"])
		self.assertIn("GrowthX Admin", frappe.get_roles(STAFF))
		self.assertTrue(
			frappe.db.exists(
				"User Social Login",
				{"parent": STAFF, "provider": "microsoft", "userid": f"{STAFF_TENANT}.{OID}"},
			)
		)
		self.assertTrue(frappe.db.exists("GX Audit Log", {"action": "staff.auto_provision"}))

	def test_an_existing_user_in_the_staff_directory_gets_the_admin_role(self):
		with _staff(email=EXISTING):
			status, body = _call(auth.microsoft, {"code": "0.Abc"})
		self.assertEqual(status, 200, body)
		self.assertEqual(body["user"]["role"], "admin")
		self.assertIn("GrowthX Admin", frappe.get_roles(EXISTING))
		self.assertTrue(frappe.db.exists("GX Audit Log", {"action": "staff.grant_admin"}))

	def test_a_user_disabled_in_growthx_stays_blocked(self):
		frappe.set_user("Administrator")
		frappe.db.set_value("User", EXISTING, "enabled", 0)
		frappe.set_user("Guest")
		with _staff(email=EXISTING):
			status, body = _call(auth.microsoft, {"code": "0.Abc"})
		self.assertEqual((status, body["error"]["code"]), (403, "account_unavailable"))
		self.assertNotIn("GrowthX Admin", frappe.get_roles(EXISTING))

	def test_a_staff_directory_guest_is_not_staff(self):
		with _staff(
			email="friend@example.com", preferred_username="friend_example.com#EXT#@staff.onmicrosoft.com"
		):
			status, body = _call(auth.microsoft, {"code": "0.Abc"})
		# No xms_edov: treated as an unverified customer with a new email -> sign-up, never admin
		self.assertEqual(status, 202, body)
		self.assertFalse(frappe.db.exists("User", {"email": "friend@example.com"}))

	# ------------------------------------------------------------ customer path

	def test_a_personal_account_with_a_verified_email_signs_in_to_the_matching_user(self):
		with _personal(email=EXISTING.upper()):
			status, body = _call(auth.microsoft, {"code": "0.Abc"})
		self.assertEqual(status, 200, body)
		self.assertEqual((body["user"]["email"], body["user"]["role"]), (EXISTING, "partner"))
		self.assertEqual(accounts.social_providers(EXISTING), ["microsoft"])

	def test_a_work_account_with_xms_edov_signs_in(self):
		with _work(email=EXISTING):
			status, body = _call(auth.microsoft, {"code": "0.Abc"})
		self.assertEqual(status, 200, body)

	def test_a_linked_microsoft_id_signs_in_without_a_verified_email(self):
		accounts.link_social(EXISTING, "microsoft", f"{OTHER_TENANT}.{OID}")
		with _microsoft(tid=OTHER_TENANT, email="renamed@other.example"):  # no xms_edov
			status, body = _call(auth.microsoft, {"code": "0.Abc"})
		self.assertEqual((status, body["user"]["email"]), (200, EXISTING))

	def test_an_unverified_email_never_matches_an_existing_user(self):
		with _microsoft(tid=OTHER_TENANT, email=EXISTING):  # work account, no xms_edov
			status, body = _call(auth.microsoft, {"code": "0.Abc"})
		self.assertEqual((status, body["error"]["code"]), (409, "email_unverified"))
		self.assertEqual(accounts.social_providers(EXISTING), [])

	def test_a_new_email_gets_a_signup_token_that_auth_signup_accepts(self):
		with _personal():
			status, body = _call(auth.microsoft, {"code": "0.Abc"})
		self.assertEqual(status, 202, body)
		self.assertEqual((body["email"], body["name"], body["emailVerified"]), (NEW, "Noor Microsoft", True))
		self.assertEqual(body["provider"], "microsoft")
		self.assertFalse(frappe.db.exists("User", {"email": NEW}))

		status, body = _call(auth.signup, {"accountType": "partner", "socialToken": body["signupToken"]})
		self.assertEqual(status, 201, body)
		self.assertEqual((body["user"]["email"], body["user"]["role"]), (NEW, "partner"))
		self.assertFalse(body["user"]["hasPassword"])
		self.assertEqual(accounts.social_providers(NEW), ["microsoft"])
		# Next time the Microsoft id alone is enough.
		with _personal(email="changed@example.com"):
			status, body = _call(auth.microsoft, {"code": "0.Abc"})
		self.assertEqual((status, body["user"]["email"]), (200, NEW))

	def test_the_old_google_token_field_still_accepts_a_microsoft_token(self):
		with _personal():
			_, body = _call(auth.microsoft, {"code": "0.Abc"})
		status, body = _call(
			auth.signup, {"accountType": "partner", "googleSignupToken": body["signupToken"]}
		)
		self.assertEqual(status, 201, body)

	def test_an_unverified_new_email_needs_the_emailed_code_to_sign_up(self):
		with _microsoft(tid=OTHER_TENANT):  # no xms_edov
			status, body = _call(auth.microsoft, {"code": "0.Abc"})
		self.assertEqual((status, body["emailVerified"]), (202, False))
		status, body = _call(auth.signup, {"accountType": "partner", "socialToken": body["signupToken"]})
		self.assertEqual(status, 422, body)
		self.assertIn("emailCode", body["error"]["fields"])

	def test_sign_in_only_does_not_start_a_signup(self):
		with _personal():
			status, body = _call(auth.microsoft, {"code": "0.Abc", "signInOnly": True})
		self.assertEqual((status, body["error"]["code"]), (404, "no_account"))

	def test_a_disabled_customer_cannot_sign_in(self):
		frappe.set_user("Administrator")
		frappe.db.set_value("User", EXISTING, "enabled", 0)
		frappe.set_user("Guest")
		with _personal(email=EXISTING):
			status, body = _call(auth.microsoft, {"code": "0.Abc"})
		self.assertEqual((status, body["error"]["code"]), (403, "account_unavailable"))

	def test_a_teambackoffice_email_outside_the_staff_directory_is_never_admin(self):
		for patcher in (_personal(email=STAFF), _work(email=STAFF)):
			with patcher:
				status, body = _call(auth.microsoft, {"code": "0.Abc"})
			self.assertEqual(status, 202, body)  # a plain customer sign-up
		status, body = _call(auth.signup, {"accountType": "partner", "socialToken": body["signupToken"]})
		self.assertEqual((status, body["user"]["role"]), (201, "partner"))
		self.assertNotIn("GrowthX Admin", frappe.get_roles(STAFF))

	def test_a_non_staff_identity_cannot_sign_in_to_an_existing_admin(self):
		frappe.set_user("Administrator")
		doc = frappe.get_doc("User", EXISTING)
		doc.append_roles("GrowthX Admin")
		doc.save(ignore_permissions=True)
		frappe.set_user("Guest")
		with _personal(email=EXISTING):
			status, body = _call(auth.microsoft, {"code": "0.Abc"})
		self.assertEqual((status, body["error"]["code"]), (403, "account_unavailable"))
