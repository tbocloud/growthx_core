"""Content editors sign in through auth.login (the /content portal). Their token reaches only the
content endpoints and their own account; `growthx_content_portal_login: 0` turns it all off."""

import json
import uuid
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils.password import update_password

from growthx_core.api.v1 import _core, admin, auth, customer

EDITOR_ROLE = "GrowthX Content Editor"
PASSWORD = "Long-Enough-Pass-91"


def _call(fn, **data):
	frappe.local.form_dict = frappe._dict(data)
	frappe.local.request = None
	response = fn()
	return response.status_code, json.loads(response.get_data())


class TestContentEditorLogin(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		frappe.local.gx_token = None
		self.email = self._user(EDITOR_ROLE)

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.local.gx_token = None
		frappe.db.rollback()

	def _user(self, *roles):
		email = f"gx-editor-{uuid.uuid4().hex[:8]}@example.com"
		user = frappe.get_doc(
			{"doctype": "User", "email": email, "first_name": "Sree", "send_welcome_email": 0}
		).insert(ignore_permissions=True)
		user.add_roles(*roles)
		update_password(email, PASSWORD)
		return email

	def _login(self, email=None):
		frappe.set_user("Guest")
		return _call(auth.login, email=email or self.email, password=PASSWORD)

	def _as(self, email):
		frappe.set_user(email)
		frappe.local.gx_token = "test-token"

	def test_login_returns_the_content_editor_role(self):
		status, body = self._login()
		self.assertEqual(status, 200, body)
		self.assertEqual(body["user"]["role"], "content_editor")
		self.assertTrue(body["accessToken"])
		self._as(self.email)
		status, body = _call(auth.me)
		self.assertEqual((status, body["role"]), (200, "content_editor"))

	def test_token_reaches_only_content_and_own_account(self):
		self._as(self.email)
		_core._require_auth(("admin", "content_editor"), "admin.content_posts")  # content: allowed
		for name in ("auth.me", "auth.logout", "auth.logout_others", "auth.change_password"):
			_core._require_auth(None, name)
		with self.assertRaises(_core.ApiError) as e:
			_core._require_auth(None, "auth.site_login")  # open to any signed-in user, not editors
		self.assertEqual(e.exception.status, 403)
		for fn in (admin.customers, admin.summary, admin.contact_requests, admin.audit_log, customer.sites):
			self.assertEqual(_call(fn)[0], 403, fn.__name__)
		self.assertEqual(_call(auth.site_login, site="x.tbocloud.in")[0], 403)

	def test_an_admin_who_also_edits_stays_admin(self):
		email = self._user("GrowthX Admin", EDITOR_ROLE)
		self.assertEqual(self._login(email)[1]["user"]["role"], "admin")

	def test_google_sign_in_stays_closed_to_editors(self):
		self.assertIsNone(auth._growthx_user(self.email))  # Google uses this default
		self.assertEqual(auth._growthx_user(self.email, editors=True), self.email)

	def test_forgot_password_works_for_editors(self):
		frappe.set_user("Guest")
		with patch.object(auth.frappe, "sendmail") as sendmail:
			_call(auth.forgot_password, email=self.email)
		self.assertEqual(sendmail.call_args.kwargs["recipients"], [self.email])

	def test_switching_portal_sign_in_off(self):
		_, session = self._login()
		with patch.dict(frappe.conf, {"growthx_content_portal_login": 0}):
			self.assertEqual(self._login()[0], 401)  # no new sign-in
			self._as(self.email)
			self.assertEqual(_call(auth.me)[0], 401)  # tokens they hold stop working
			frappe.set_user("Guest")
			frappe.local.gx_token = None
			status, _ = _call(auth.refresh, refreshToken=session["refreshToken"])
			self.assertEqual(status, 401)
			self.assertEqual(
				frappe.db.count("GX Auth Token", {"user": self.email, "status": "Active"}), 0
			)  # and are revoked on refresh
		# Desk is untouched: the role keeps desk access and two-factor.
		self.assertEqual(frappe.db.get_value("Role", EDITOR_ROLE, ["desk_access", "two_factor_auth"]), (1, 1))


class TestEditorTokenOnlyOnGrowthXApi(IntegrationTestCase):
	"""The bearer token of a content editor doesn't sign them in to Frappe's own API or desk methods."""

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.local.gx_token = None
		frappe.local.request = None
		frappe.db.rollback()

	def _authenticate(self, email, path):
		from werkzeug.test import EnvironBuilder
		from werkzeug.wrappers import Request

		from growthx_core import tokens

		pair = tokens.issue(email)
		frappe.set_user("Guest")
		frappe.local.gx_token = None
		frappe.local.request = Request(
			EnvironBuilder(
				path=path, headers={"Authorization": f"Bearer {pair['accessToken']}"}
			).get_environ()
		)
		tokens.authenticate_request()
		return frappe.session.user

	def _user(self, *roles):
		frappe.set_user("Administrator")
		email = f"gx-editor-{uuid.uuid4().hex[:8]}@example.com"
		frappe.get_doc(
			{"doctype": "User", "email": email, "first_name": "Sree", "send_welcome_email": 0}
		).insert(ignore_permissions=True).add_roles(*roles)
		return email

	def test_editor_token_only_on_v1_paths(self):
		email = self._user(EDITOR_ROLE)
		self.assertEqual(self._authenticate(email, "/api/method/growthx_core.api.v1.auth.me"), email)
		for path in (
			"/api/resource/GX Website Post",
			"/api/method/frappe.client.get_list",
			"/api/method/frappe.auth.get_logged_user",
			"/api/method/growthx_core.api.v1x.evil",
		):
			self.assertEqual(self._authenticate(email, path), "Guest", path)

	def test_other_roles_unchanged(self):
		email = self._user("GrowthX Admin", EDITOR_ROLE)
		self.assertEqual(self._authenticate(email, "/api/method/frappe.auth.get_logged_user"), email)
