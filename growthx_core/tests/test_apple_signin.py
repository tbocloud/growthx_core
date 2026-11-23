"""Sign in / sign up with Apple: auth.apple. Apple's token endpoint is mocked; the client-secret JWT is
checked against a throwaway P-256 key made here."""

import base64
import json
import time
from unittest.mock import MagicMock, patch

import frappe
import jwt
import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from frappe.tests import IntegrationTestCase

from growthx_core import accounts, apple_signin
from growthx_core.api.v1 import _core, auth

_KEY = ec.generate_private_key(ec.SECP256R1())
PEM = _KEY.private_bytes(
	serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
).decode()
PUBLIC_PEM = _KEY.public_key().public_bytes(
	serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
)
CONF = {
	"apple_oauth_client_id": "com.example.growthx.web",
	"apple_oauth_team_id": "TEAM123456",
	"apple_oauth_key_id": "KEY1234567",
	"apple_oauth_private_key": PEM,
	"apple_oauth_redirect_uri": "http://localhost:3000/api/auth/apple/callback",
}
EXISTING = "gx-apple-existing@example.com"
NEW = "gx-apple-new@example.com"
RELAY = "abc123xyz@privaterelay.appleid.com"


def _call(fn, data=None):
	frappe.local.form_dict = frappe._dict(data or {})
	frappe.local.request = None
	frappe.local.request_ip = "127.0.0.1"
	with patch.object(_core, "_idempotency_key", return_value=None):
		response = fn()
	return response.status_code, json.loads(response.get_data())


def _id_token(**claims):
	payload = {
		"iss": "https://appleid.apple.com",
		"aud": CONF["apple_oauth_client_id"],
		"exp": int(time.time()) + 600,
		"sub": "001234.abcdef.5678",
		"email": NEW,
		"email_verified": "true",
		"is_private_email": "false",
		"nonce": "n" * 43,
		**claims,
	}
	payload = {k: v for k, v in payload.items() if v is not None}
	encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
	return f"eyJhbGciOiJSUzI1NiJ9.{encoded}.signature"


def _response(payload, status=200):
	response = MagicMock(status_code=status)
	response.json.return_value = payload
	return response


def _apple(status=200, **claims):
	payload = {"id_token": _id_token(**claims), "access_token": "a"} if status == 200 else claims
	return patch.object(apple_signin.requests, "post", return_value=_response(payload, status))


class TestAppleSignin(IntegrationTestCase):
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

	def test_client_secret_is_an_es256_jwt_for_the_services_id(self):
		secret = apple_signin.client_secret(now=1_700_000_000)
		self.assertEqual(
			jwt.get_unverified_header(secret), {"alg": "ES256", "kid": "KEY1234567", "typ": "JWT"}
		)
		claims = jwt.decode(
			secret,
			PUBLIC_PEM,
			algorithms=["ES256"],
			audience="https://appleid.apple.com",
			options={"verify_exp": False, "verify_iat": False},
		)
		self.assertEqual(
			claims,
			{
				"iss": "TEAM123456",
				"iat": 1_700_000_000,
				"exp": 1_700_000_600,
				"aud": "https://appleid.apple.com",
				"sub": "com.example.growthx.web",
			},
		)

	def test_the_private_key_may_be_stored_with_escaped_newlines(self):
		with patch.dict(frappe.conf, {"apple_oauth_private_key": PEM.strip().replace("\n", "\\n")}):
			jwt.decode(
				apple_signin.client_secret(),
				PUBLIC_PEM,
				algorithms=["ES256"],
				audience="https://appleid.apple.com",
			)

	def test_a_new_email_starts_a_signup_and_the_name_comes_from_the_form(self):
		with _apple() as post:
			status, body = _call(auth.apple, {"code": "abc", "nonce": "n" * 43, "name": " Noor  Apple\n"})
		self.assertEqual(status, 202, body)
		self.assertEqual((body["email"], body["name"], body["provider"]), (NEW, "Noor Apple", "apple"))
		sent = post.call_args.kwargs["data"]
		self.assertEqual(post.call_args.args[0], "https://appleid.apple.com/auth/token")
		self.assertEqual(sent["code"], "abc")
		self.assertEqual(sent["grant_type"], "authorization_code")
		self.assertEqual(sent["client_id"], CONF["apple_oauth_client_id"])
		self.assertEqual(sent["redirect_uri"], CONF["apple_oauth_redirect_uri"])
		self.assertEqual(jwt.get_unverified_header(sent["client_secret"])["alg"], "ES256")

		status, body = _call(
			auth.signup, {"accountType": "partner", "socialToken": body["signupToken"], "name": "Noor Apple"}
		)
		self.assertEqual(status, 201, body)
		self.assertEqual((body["user"]["email"], body["user"]["role"]), (NEW, "partner"))
		self.assertEqual(accounts.social_providers(NEW), ["apple"])
		self.assertTrue(
			frappe.db.exists("User Social Login", {"parent": NEW, "userid": "001234.abcdef.5678"})
		)

		# Apple sends the name only once: the next sign-in is by Apple id, whatever the email says.
		with _apple(email="somebody-else@example.com"):
			status, body = _call(auth.apple, {"code": "abc", "nonce": "n" * 43})
		self.assertEqual((status, body["user"]["email"]), (200, NEW))

	def test_no_name_is_fine(self):
		with _apple():
			status, body = _call(auth.apple, {"code": "abc"})
		self.assertEqual((status, body["name"]), (202, ""))

	def test_an_existing_email_signs_in_and_links(self):
		with _apple(email=EXISTING.upper(), email_verified=True):
			status, body = _call(auth.apple, {"code": "abc"})
		self.assertEqual((status, body["user"]["email"]), (200, EXISTING))
		self.assertEqual(accounts.social_providers(EXISTING), ["apple"])

	def test_a_private_relay_address_is_an_ordinary_new_email(self):
		with _apple(email=RELAY, is_private_email=True):
			status, body = _call(auth.apple, {"code": "abc"})
		self.assertEqual((status, body["email"], body["provider"]), (202, RELAY, "apple"))
		self.assertTrue(apple_signin._bool("true") and apple_signin._bool(True))

	def test_a_private_relay_address_never_matches_another_account(self):
		# The relay address of an Apple user is not the real email of anyone else's account.
		with _apple(email=RELAY, is_private_email="true", sub="relay-sub"):
			status, body = _call(auth.apple, {"code": "abc"})
		self.assertEqual(status, 202)
		self.assertIsNone(accounts.user_by_social("apple", "relay-sub"))
		self.assertFalse(frappe.db.exists("User Social Login", {"userid": "relay-sub"}))

	def test_identity_flags(self):
		with _apple(email=RELAY, is_private_email="true"):
			identity = apple_signin.verified_identity("abc")
		self.assertEqual(
			(identity["privateEmail"], identity["emailVerified"], identity["id"]),
			(True, True, "001234.abcdef.5678"),
		)
		with _apple(email_verified=True, is_private_email=False):
			self.assertFalse(apple_signin.verified_identity("abc")["privateEmail"])

	def test_apple_never_opens_an_admin_account(self):
		frappe.set_user("Administrator")
		doc = frappe.get_doc("User", EXISTING)
		doc.append_roles("GrowthX Admin")
		doc.save(ignore_permissions=True)
		frappe.set_user("Guest")
		with _apple(email=EXISTING):
			status, body = _call(auth.apple, {"code": "abc"})
		self.assertEqual((status, body["error"]["code"]), (403, "account_unavailable"))

	def test_a_disabled_account_cannot_sign_in(self):
		frappe.set_user("Administrator")
		frappe.db.set_value("User", EXISTING, "enabled", 0)
		frappe.set_user("Guest")
		with _apple(email=EXISTING):
			status, body = _call(auth.apple, {"code": "abc"})
		self.assertEqual((status, body["error"]["code"]), (403, "account_unavailable"))

	def test_sign_in_only(self):
		with _apple():
			status, body = _call(auth.apple, {"code": "abc", "signInOnly": True})
		self.assertEqual((status, body["error"]["code"]), (404, "no_account"))

	def test_unverified_email_is_refused(self):
		with _apple(email_verified="false"):
			status, body = _call(auth.apple, {"code": "abc"})
		self.assertEqual((status, body["error"]["code"]), (403, "email_not_verified"))

	def test_missing_email_is_refused(self):
		with _apple(email=None):
			status, body = _call(auth.apple, {"code": "abc"})
		self.assertEqual((status, body["error"]["code"]), (403, "email_not_verified"))

	def test_wrong_nonce_is_refused(self):
		with _apple():
			status, body = _call(auth.apple, {"code": "abc", "nonce": "x" * 43})
		self.assertEqual((status, body["error"]["code"]), (400, "apple_failed"))

	def test_nonce_missing_from_the_token_is_refused(self):
		with _apple(nonce=None):
			status, body = _call(auth.apple, {"code": "abc", "nonce": "n" * 43})
		self.assertEqual((status, body["error"]["code"]), (400, "apple_failed"))

	def test_wrong_audience_is_refused(self):
		with _apple(aud="com.someone.else"):
			status, body = _call(auth.apple, {"code": "abc"})
		self.assertEqual((status, body["error"]["code"]), (400, "apple_failed"))

	def test_wrong_issuer_is_refused(self):
		with _apple(iss="https://evil.example.com"):
			status, body = _call(auth.apple, {"code": "abc"})
		self.assertEqual((status, body["error"]["code"]), (400, "apple_failed"))

	def test_expired_token_is_refused(self):
		with _apple(exp=int(time.time()) - 3600):
			status, body = _call(auth.apple, {"code": "abc"})
		self.assertEqual((status, body["error"]["code"]), (400, "apple_failed"))

	def test_token_without_a_subject_is_refused(self):
		with _apple(sub=None):
			status, body = _call(auth.apple, {"code": "abc"})
		self.assertEqual((status, body["error"]["code"]), (400, "apple_failed"))

	def test_garbage_id_token_is_refused(self):
		with patch.object(apple_signin.requests, "post", return_value=_response({"id_token": "nope"})):
			status, body = _call(auth.apple, {"code": "abc"})
		self.assertEqual((status, body["error"]["code"]), (400, "apple_failed"))

	def test_bad_code(self):
		with _apple(status=400, error="invalid_grant"):
			status, body = _call(auth.apple, {"code": "used"})
		self.assertEqual((status, body["error"]["code"]), (400, "apple_failed"))

	def test_invalid_client_is_apple_failed_too(self):
		with _apple(status=400, error="invalid_client"):
			status, body = _call(auth.apple, {"code": "abc"})
		self.assertEqual((status, body["error"]["code"]), (400, "apple_failed"))

	def test_apple_unreachable(self):
		with patch.object(apple_signin.requests, "post", side_effect=requests.ConnectionError):
			status, body = _call(auth.apple, {"code": "abc"})
		self.assertEqual((status, body["error"]["code"]), (503, "apple_unavailable"))

	def test_not_configured(self):
		with patch.dict(frappe.conf, {"apple_oauth_private_key": None}), _apple() as post:
			status, body = _call(auth.apple, {"code": "abc"})
		self.assertEqual((status, body["error"]["code"]), (503, "apple_unavailable"))
		post.assert_not_called()

	def test_a_key_that_cannot_sign_is_unavailable(self):
		with patch.dict(frappe.conf, {"apple_oauth_private_key": "not a key"}), _apple() as post:
			status, body = _call(auth.apple, {"code": "abc"})
		self.assertEqual((status, body["error"]["code"]), (503, "apple_unavailable"))
		post.assert_not_called()

	def test_code_is_required(self):
		status, body = _call(auth.apple, {})
		self.assertEqual(status, 422, body)

	def test_default_redirect_uri_is_on_the_website(self):
		with patch.dict(
			frappe.conf, {"apple_oauth_redirect_uri": None, "growthx_web_url": "https://tbogrowthx.com"}
		):
			self.assertEqual(apple_signin.redirect_uri(), "https://tbogrowthx.com/api/auth/apple/callback")


class TestAppleLinking(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.conf = patch.dict(frappe.conf, CONF)
		self.conf.start()
		accounts.create_partner_account(email=EXISTING, full_name="Existing Partner", password="x" * 20)
		frappe.set_user(EXISTING)

	def tearDown(self):
		self.conf.stop()
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def _as_user(self, fn, data):
		frappe.set_user(EXISTING)  # before the form: set_user resets frappe.local.form_dict
		frappe.local.form_dict = frappe._dict(data)
		frappe.local.request = None
		frappe.local.request_ip = "127.0.0.1"
		with (
			patch.object(_core, "_idempotency_key", return_value=None),
			patch.object(_core, "_require_auth", return_value=None),
		):
			response = fn()
		return response.status_code, json.loads(response.get_data())

	def test_link_then_unlink(self):
		with _apple(email=RELAY):
			status, body = self._as_user(
				auth.link_provider, {"provider": "apple", "code": "abc", "nonce": "n" * 43}
			)
		self.assertEqual((status, body["socialLogins"]), (200, ["apple"]), body)
		status, body = self._as_user(auth.unlink_provider, {"provider": "apple"})
		self.assertEqual((status, body["socialLogins"]), (200, []), body)

	def test_linking_checks_the_nonce(self):
		with _apple():
			status, body = self._as_user(
				auth.link_provider, {"provider": "apple", "code": "abc", "nonce": "x" * 43}
			)
		self.assertEqual((status, body["error"]["code"]), (400, "apple_failed"))

	def test_an_apple_account_of_another_user_cannot_be_linked(self):
		frappe.set_user("Administrator")
		accounts.create_partner_account(
			email="gx-other-apple@example.com", full_name="O U", password="x" * 20
		)
		accounts.link_social("gx-other-apple@example.com", "apple", "001234.abcdef.5678")
		with _apple():
			status, body = self._as_user(auth.link_provider, {"provider": "apple", "code": "abc"})
		self.assertEqual((status, body["error"]["code"]), (409, "already_linked"))

	def test_the_last_sign_in_method_stays_without_a_password(self):
		accounts.link_social(EXISTING, "apple", "a1")
		accounts.mark_no_password(EXISTING)
		status, body = self._as_user(auth.unlink_provider, {"provider": "apple"})
		self.assertEqual((status, body["error"]["code"]), (409, "last_sign_in_method"))

	def test_apple_can_go_while_another_provider_stays_without_a_password(self):
		accounts.link_social(EXISTING, "apple", "a1")
		accounts.link_social(EXISTING, "google", "g1")
		accounts.mark_no_password(EXISTING)
		status, body = self._as_user(auth.unlink_provider, {"provider": "apple"})
		self.assertEqual((status, body["socialLogins"]), (200, ["google"]))
