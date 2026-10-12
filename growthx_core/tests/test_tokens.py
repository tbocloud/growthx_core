from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from growthx_core import tokens


class TestTokens(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.user = "Administrator"

	def _authenticate(self, token):
		frappe.local.gx_token = None
		with patch("frappe.get_request_header", return_value=f"Bearer {token}"):
			tokens.authenticate_request()
		return getattr(frappe.local, "gx_token", None)

	def test_only_hashes_are_stored(self):
		pair = tokens.issue(self.user)
		doc = frappe.get_doc("GX Auth Token", pair["_name"])
		self.assertNotIn(pair["accessToken"], (doc.access_hash, doc.refresh_hash))
		self.assertEqual(doc.access_hash, tokens._hash(pair["accessToken"]))

	def test_valid_token_authenticates_and_keeps_form_dict(self):
		pair = tokens.issue(self.user)
		frappe.local.form_dict = frappe._dict(id="GXT-1")
		self.assertEqual(self._authenticate(pair["accessToken"]), pair["_name"])
		self.assertEqual(frappe.local.form_dict.id, "GXT-1")

	def test_revoked_and_expired_tokens_are_rejected(self):
		pair = tokens.issue(self.user)
		tokens.revoke(pair["_name"])
		self.assertIsNone(self._authenticate(pair["accessToken"]))

		pair = tokens.issue(self.user)
		frappe.db.set_value("GX Auth Token", pair["_name"], "access_expires_at", "2000-01-01 00:00:00")
		self.assertIsNone(self._authenticate(pair["accessToken"]))

	def test_refresh_rotates_once(self):
		pair = tokens.issue(self.user, "mobile")
		new = tokens.rotate(pair["refreshToken"])
		self.assertIsNotNone(new)
		self.assertEqual(frappe.db.get_value("GX Auth Token", new["_name"], "client"), "mobile")
		self.assertIsNone(tokens.rotate(pair["refreshToken"]))  # the old refresh token is spent
		self.assertIsNone(self._authenticate(pair["accessToken"]))  # and its access token revoked

	def test_non_growthx_bearer_is_ignored(self):
		self.assertIsNone(self._authenticate("some-oauth-token"))
