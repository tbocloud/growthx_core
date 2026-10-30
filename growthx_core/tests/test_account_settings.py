"""Account settings: auth.change_password, auth.logout_others, and hasPassword on the user."""

import json
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils.password import check_password

from growthx_core import accounts, google_signin, tokens
from growthx_core.api.v1 import _core, auth

EMAIL = "gx-settings-test@example.com"
OLD = "Long-Enough-Pass-91"
NEW = "Brand-New-Secret-42"


class TestAccountSettings(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.user = accounts.create_partner_account(email=EMAIL, full_name="Settings Test", password=OLD)
		self.mine = tokens.issue(self.user, "mobile")
		self.other = tokens.issue(self.user, "web")

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.local.gx_token = None
		frappe.db.rollback()

	def call(self, fn, data=None, token=None):
		frappe.set_user(self.user)
		frappe.local.gx_token = (token or self.mine)["_name"]
		frappe.local.form_dict = frappe._dict(data or {})
		frappe.local.request = None
		frappe.local.request_ip = "127.0.0.1"
		with (
			patch.object(_core, "_idempotency_key", return_value=None),
			patch.object(frappe.db, "rollback"),  # an error response would roll back this test's tokens
		):
			response = fn()
		return response.status_code, json.loads(response.get_data())

	def _active(self, pair) -> bool:
		return frappe.db.get_value("GX Auth Token", pair["_name"], "status") == "Active"

	def test_change_password_logs_out_everywhere_else(self):
		status, body = self.call(auth.change_password, {"currentPassword": OLD, "newPassword": NEW})
		self.assertEqual(status, 200, body)
		self.assertTrue(body["accessToken"].startswith("gxa_"))
		self.assertTrue(body["user"]["hasPassword"])
		self.assertFalse(self._active(self.mine) or self._active(self.other))
		new_row = frappe.db.get_value(
			"GX Auth Token",
			{"access_hash": tokens._hash(body["accessToken"])},
			["status", "client"],
			as_dict=True,
		)
		self.assertEqual((new_row.status, new_row.client), ("Active", "mobile"))  # same kind of device
		check_password(self.user, NEW)  # raises if not changed

	def test_wrong_current_password(self):
		status, body = self.call(auth.change_password, {"currentPassword": "nope-nope-1", "newPassword": NEW})
		self.assertEqual((status, body["error"]["code"]), (422, "wrong_password"))
		self.assertIn("currentPassword", body["error"]["fields"])
		self.assertTrue(self._active(self.other))

	def test_new_password_rule(self):
		for bad in ("short1", "no-numbers-here", "1234567890123"):
			status, body = self.call(auth.change_password, {"currentPassword": OLD, "newPassword": bad})
			self.assertEqual((status, list(body["error"]["fields"])), (422, ["newPassword"]), bad)

	def test_google_only_account_has_no_password(self):
		frappe.set_user("Administrator")
		token = google_signin.issue_signup_token(
			{"email": "gx-settings-g@example.com", "name": "G User", "sub": "9"}
		)
		frappe.set_user("Guest")
		frappe.local.form_dict = frappe._dict({"accountType": "partner", "googleSignupToken": token})
		frappe.local.request = None
		frappe.local.request_ip = "127.0.0.1"
		with patch.object(_core, "_idempotency_key", return_value=None):
			body = json.loads(auth.signup().get_data())
		self.assertFalse(body["user"]["hasPassword"])

		self.user = body["user"]["id"]
		self.mine = tokens.issue(self.user)
		status, body = self.call(auth.change_password, {"currentPassword": "anything1", "newPassword": NEW})
		self.assertEqual((status, body["error"]["code"]), (409, "no_password"))

		# Setting one with Forgot password makes it a password account.
		accounts.mark_has_password(self.user)
		self.assertTrue(accounts.has_password(self.user))

	def test_logout_others_keeps_only_this_device(self):
		third = tokens.issue(self.user, "web")
		status, body = self.call(auth.logout_others)
		self.assertEqual((status, body), (200, {"ok": True, "revoked": 2}))
		self.assertTrue(self._active(self.mine))
		self.assertFalse(self._active(self.other) or self._active(third))
		self.assertEqual(self.call(auth.logout_others)[1]["revoked"], 0)

	def test_me_has_has_password(self):
		status, body = self.call(auth.me)
		self.assertEqual((status, body["hasPassword"]), (200, True))
