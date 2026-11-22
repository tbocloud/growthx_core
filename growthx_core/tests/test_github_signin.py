"""Sign in / sign up with GitHub: auth.github. GitHub's token endpoint and API are mocked."""

import json
from unittest.mock import MagicMock, patch

import frappe
import requests
from frappe.tests import IntegrationTestCase

from growthx_core import accounts, github_signin
from growthx_core.api.v1 import _core, auth

CONF = {
	"github_oauth_client_id": "Iv1.test",
	"github_oauth_client_secret": "test-secret",
	"github_oauth_redirect_uri": "http://localhost:3000/api/auth/github/callback",
}
EXISTING = "gx-github-existing@example.com"
NEW = "gx-github-new@example.com"


def _call(fn, data=None):
	frappe.local.form_dict = frappe._dict(data or {})
	frappe.local.request = None
	frappe.local.request_ip = "127.0.0.1"
	with patch.object(_core, "_idempotency_key", return_value=None):
		response = fn()
	return response.status_code, json.loads(response.get_data())


def _response(payload, status=200):
	response = MagicMock(status_code=status)
	response.json.return_value = payload
	return response


def _github(emails=None, token=None, profile=None, user_status=200):
	emails = (
		[
			{"email": "other@example.com", "primary": False, "verified": True},
			{"email": NEW, "primary": True, "verified": True},
		]
		if emails is None
		else emails
	)
	post = patch.object(
		github_signin.requests,
		"post",
		return_value=_response(token or {"access_token": "gho_x", "token_type": "bearer"}),
	)

	def get(url, **kwargs):
		if url == github_signin.USER_URL:
			return _response(profile or {"id": 4242, "login": "noor", "name": "Noor Hub"}, user_status)
		return _response(emails)

	return post, patch.object(github_signin.requests, "get", side_effect=get)


class _Both:
	def __init__(self, **kwargs):
		self.post, self.get = _github(**kwargs)

	def __enter__(self):
		self.post_mock = self.post.start()
		self.get_mock = self.get.start()
		return self

	def __exit__(self, *exc):
		self.post.stop()
		self.get.stop()


class TestGithubSignin(IntegrationTestCase):
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

	def test_the_primary_verified_email_starts_a_signup(self):
		with _Both() as gh:
			status, body = _call(auth.github, {"code": "abc", "codeVerifier": "v" * 43})
		self.assertEqual(status, 202, body)
		self.assertEqual((body["email"], body["name"], body["provider"]), (NEW, "Noor Hub", "github"))
		sent = gh.post_mock.call_args.kwargs["data"]
		self.assertEqual(sent["redirect_uri"], CONF["github_oauth_redirect_uri"])
		self.assertEqual((sent["code"], sent["code_verifier"]), ("abc", "v" * 43))
		self.assertEqual(gh.post_mock.call_args.args[0], "https://github.com/login/oauth/access_token")
		self.assertEqual(gh.get_mock.call_args.args[0], github_signin.EMAILS_URL)

		status, body = _call(auth.signup, {"accountType": "partner", "socialToken": body["signupToken"]})
		self.assertEqual(status, 201, body)
		self.assertEqual((body["user"]["email"], body["user"]["role"]), (NEW, "partner"))
		self.assertEqual(accounts.social_providers(NEW), ["github"])
		self.assertTrue(frappe.db.exists("User Social Login", {"parent": NEW, "userid": "4242"}))

		with _Both(emails=[{"email": "x@example.com", "primary": True, "verified": True}]):
			status, body = _call(auth.github, {"code": "abc"})
		self.assertEqual((status, body["user"]["email"]), (200, NEW))  # by GitHub id

	def test_an_existing_email_signs_in_and_links(self):
		with _Both(emails=[{"email": EXISTING.upper(), "primary": True, "verified": True}]):
			status, body = _call(auth.github, {"code": "abc"})
		self.assertEqual((status, body["user"]["email"]), (200, EXISTING))
		self.assertEqual(accounts.social_providers(EXISTING), ["github"])

	def test_only_the_primary_verified_email_counts(self):
		for emails in (
			[{"email": EXISTING, "primary": False, "verified": True}],
			[{"email": EXISTING, "primary": True, "verified": False}],
			[{"email": "1+noor@users.noreply.github.com", "primary": True, "verified": True}],
			[],
		):
			with _Both(emails=emails):
				status, body = _call(auth.github, {"code": "abc"})
			self.assertEqual((status, body["error"]["code"]), (403, "email_not_verified"), emails)

	def test_github_never_opens_an_admin_account(self):
		frappe.set_user("Administrator")
		doc = frappe.get_doc("User", EXISTING)
		doc.append_roles("GrowthX Admin")
		doc.save(ignore_permissions=True)
		frappe.set_user("Guest")
		with _Both(emails=[{"email": EXISTING, "primary": True, "verified": True}]):
			status, body = _call(auth.github, {"code": "abc"})
		self.assertEqual((status, body["error"]["code"]), (403, "account_unavailable"))

	def test_a_disabled_account_cannot_sign_in(self):
		frappe.set_user("Administrator")
		frappe.db.set_value("User", EXISTING, "enabled", 0)
		frappe.set_user("Guest")
		with _Both(emails=[{"email": EXISTING, "primary": True, "verified": True}]):
			status, body = _call(auth.github, {"code": "abc"})
		self.assertEqual((status, body["error"]["code"]), (403, "account_unavailable"))

	def test_sign_in_only(self):
		with _Both():
			status, body = _call(auth.github, {"code": "abc", "signInOnly": True})
		self.assertEqual((status, body["error"]["code"]), (404, "no_account"))

	def test_bad_code(self):
		with _Both(token={"error": "bad_verification_code"}):
			status, body = _call(auth.github, {"code": "used"})
		self.assertEqual((status, body["error"]["code"]), (400, "github_failed"))

	def test_api_errors_are_github_failed(self):
		with _Both(user_status=401):
			status, body = _call(auth.github, {"code": "abc"})
		self.assertEqual((status, body["error"]["code"]), (400, "github_failed"))

	def test_github_unreachable(self):
		with patch.object(github_signin.requests, "post", side_effect=requests.ConnectionError):
			status, body = _call(auth.github, {"code": "abc"})
		self.assertEqual((status, body["error"]["code"]), (503, "github_unavailable"))

	def test_not_configured(self):
		with patch.dict(frappe.conf, {"github_oauth_client_secret": None}), _Both() as gh:
			status, body = _call(auth.github, {"code": "abc"})
		self.assertEqual((status, body["error"]["code"]), (503, "github_unavailable"))
		gh.post_mock.assert_not_called()

	def test_default_redirect_uri_is_on_the_website(self):
		with patch.dict(
			frappe.conf, {"github_oauth_redirect_uri": None, "growthx_web_url": "https://tbogrowthx.com"}
		):
			self.assertEqual(github_signin.redirect_uri(), "https://tbogrowthx.com/api/auth/github/callback")


class TestLinkedProviders(IntegrationTestCase):
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
		from growthx_core import tokens

		pair = tokens.issue(EXISTING, "web")
		frappe.local.form_dict = frappe._dict(data)
		frappe.local.request = None
		frappe.local.request_ip = "127.0.0.1"
		with (
			patch.object(_core, "_idempotency_key", return_value=None),
			patch.object(_core, "_require_auth", return_value=None),
		):
			frappe.set_user(EXISTING)
			response = fn()
		return response.status_code, json.loads(response.get_data()), pair

	def test_link_then_unlink(self):
		with _Both(emails=[{"email": "elsewhere@example.com", "primary": True, "verified": True}]):
			status, body, _ = self._as_user(auth.link_provider, {"provider": "github", "code": "abc"})
		self.assertEqual((status, body["socialLogins"]), (200, ["github"]), body)
		status, body, _ = self._as_user(auth.unlink_provider, {"provider": "github"})
		self.assertEqual((status, body["socialLogins"]), (200, []), body)

	def test_a_provider_account_of_another_user_cannot_be_linked(self):
		frappe.set_user("Administrator")
		accounts.create_partner_account(email="gx-other-user@example.com", full_name="O U", password="x" * 20)
		accounts.link_social("gx-other-user@example.com", "github", "4242")
		with _Both():
			status, body, _ = self._as_user(auth.link_provider, {"provider": "github", "code": "abc"})
		self.assertEqual((status, body["error"]["code"]), (409, "already_linked"))

	def test_the_last_sign_in_method_stays_without_a_password(self):
		accounts.link_social(EXISTING, "google", "g1")
		accounts.mark_no_password(EXISTING)
		status, body, _ = self._as_user(auth.unlink_provider, {"provider": "google"})
		self.assertEqual((status, body["error"]["code"]), (409, "last_sign_in_method"))
		accounts.link_social(EXISTING, "github", "4242")
		status, body, _ = self._as_user(auth.unlink_provider, {"provider": "google"})
		self.assertEqual((status, body["socialLogins"]), (200, ["github"]))

	def test_unlinking_with_a_password_is_fine_and_unknown_is_404(self):
		accounts.link_social(EXISTING, "google", "g1")
		status, body, _ = self._as_user(auth.unlink_provider, {"provider": "google"})
		self.assertEqual(status, 200, body)
		status, body, _ = self._as_user(auth.unlink_provider, {"provider": "google"})
		self.assertEqual((status, body["error"]["code"]), (404, "not_linked"))
