"""Admin portal team: admin.staff, admin.add_staff, admin.update_staff, admin.remove_staff."""

import json
import uuid
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from growthx_core import accounts, tokens
from growthx_core.api.v1 import _core, admin

ADMIN = "GrowthX Admin"
EDITOR = "GrowthX Content Editor"


def _call(fn, data=None):
	frappe.local.form_dict = frappe._dict(data or {})
	frappe.local.request = None
	frappe.local.request_ip = "127.0.0.1"
	with (
		patch.object(_core, "_idempotency_key", return_value=None),
		patch.object(_core, "_require_auth"),  # authz is tested separately below
		patch.object(frappe.db, "rollback"),
	):
		response = fn()
	return response.status_code, json.loads(response.get_data())


class TestAdminStaff(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.me = self._user(ADMIN)
		self.other = self._user(ADMIN)
		self.editor = self._user(EDITOR)
		# Existing admins on a shared test site would hide the last-admin branch: park them.
		self.parked = [n for n in admin._enabled_admins() if n not in (self.me, self.other)]
		for name in self.parked:
			frappe.db.set_value("User", name, "enabled", 0)
		frappe.set_user(self.me)
		self.mail = patch("frappe.sendmail").start()

	def tearDown(self):
		patch.stopall()
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def _user(self, *roles, enabled=1):
		email = f"gx-staff-{uuid.uuid4().hex[:8]}@example.com"
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": "Sam",
				"send_welcome_email": 0,
				"enabled": enabled,
			}
		).insert(ignore_permissions=True)
		for role in roles:
			user.append("roles", {"role": role})
		user.save(ignore_permissions=True)
		return email

	def _roles(self, email):
		return set(frappe.get_all("Has Role", {"parent": email, "parenttype": "User"}, pluck="role"))

	# ------------------------------------------------------------ list

	def test_lists_staff_with_sign_in_methods(self):
		accounts.mark_no_password(self.editor)
		accounts.link_microsoft(self.editor, "ms-1")
		accounts.link_google(self.editor, "g-1")
		customer = self._user("GrowthX Customer")
		status, body = _call(admin.staff)
		self.assertEqual(status, 200, body)
		by_id = {m["id"]: m for m in body["items"]}
		self.assertNotIn(customer, by_id)
		self.assertNotIn("Administrator", by_id)
		self.assertNotIn("Guest", by_id)
		self.assertEqual(body["total"], len(body["items"]))
		editor = by_id[self.editor]
		self.assertEqual(editor["role"], "content_editor")
		self.assertEqual(editor["signIn"], {"password": False, "providers": ["google", "microsoft"]})
		self.assertTrue(editor["enabled"])
		self.assertFalse(editor["isYou"])
		self.assertTrue(by_id[self.me]["isYou"])
		self.assertEqual(by_id[self.me]["role"], "admin")
		self.assertTrue(by_id[self.me]["signIn"]["password"])

	def test_admin_wins_when_user_has_both_roles(self):
		both = self._user(ADMIN, EDITOR)
		_, body = _call(admin.staff)
		self.assertEqual({m["id"]: m for m in body["items"]}[both]["role"], "admin")

	# ------------------------------------------------------------ authz

	def test_every_endpoint_is_admin_only(self):
		for fn in (admin.staff, admin.add_staff, admin.update_staff, admin.remove_staff):
			for who, expected in ((self.editor, 403), (None, 401)):
				frappe.set_user(who or "Guest")
				frappe.local.gx_token = "t" if who else None
				frappe.local.form_dict = frappe._dict({})
				frappe.local.request = None
				frappe.local.request_ip = "127.0.0.1"
				with patch.object(frappe.db, "rollback"):
					response = fn()
				self.assertEqual(response.status_code, expected, (fn.__name__, who))
		frappe.set_user(self.me)

	# ------------------------------------------------------------ add

	def test_add_new_staff_invites_without_password(self):
		email = f"new-{uuid.uuid4().hex[:6]}@example.com"
		status, body = _call(
			admin.add_staff, {"email": email.upper(), "name": "New Person", "role": "content_editor"}
		)
		self.assertEqual(status, 201, body)
		self.assertTrue(body["invited"])
		self.assertEqual(body["role"], "content_editor")
		self.assertFalse(body["signIn"]["password"])
		self.assertEqual(self._roles(email), {EDITOR})
		self.assertEqual(self.mail.call_args.kwargs["recipients"], [email])
		self.assertIn("/reset-password?key=", self.mail.call_args.kwargs["message"])
		self.assertTrue(frappe.db.get_value("User", email, "reset_password_key"))
		self.assertEqual(
			frappe.db.get_value("GX Audit Log", {"action": "staff.add", "reference_name": email}, "actor"),
			self.me,
		)

	def test_add_survives_a_mail_failure(self):
		self.mail.side_effect = frappe.OutgoingEmailError("no account")
		email = f"new-{uuid.uuid4().hex[:6]}@example.com"
		status, body = _call(admin.add_staff, {"email": email, "name": "Mail Fail", "role": "admin"})
		self.assertEqual(status, 201, body)
		self.assertFalse(body["invited"])
		self.assertEqual(self._roles(email), {ADMIN})

	def test_add_role_to_existing_user_keeps_their_roles(self):
		customer = self._user("GrowthX Customer")
		status, body = _call(admin.add_staff, {"email": customer, "name": "X", "role": "admin"})
		self.assertEqual(status, 201, body)
		self.assertFalse(body["invited"])
		self.assertEqual(self._roles(customer), {"GrowthX Customer", ADMIN})
		self.mail.assert_not_called()

	def test_add_existing_staff_is_409(self):
		status, body = _call(admin.add_staff, {"email": self.editor, "name": "X", "role": "admin"})
		self.assertEqual((status, body["error"]["code"]), (409, "already_staff"))
		self.assertEqual(self._roles(self.editor), {EDITOR})

	def test_add_disabled_user_is_409(self):
		disabled = self._user("GrowthX Customer", enabled=0)
		status, body = _call(admin.add_staff, {"email": disabled, "name": "X", "role": "admin"})
		self.assertEqual((status, body["error"]["code"]), (409, "user_disabled"))

	def test_add_validates_input(self):
		for data, field in (
			({"name": "X", "role": "admin"}, "email"),
			({"email": "not-an-email", "name": "X", "role": "admin"}, "email"),
			({"email": "a@example.com", "role": "admin"}, "name"),
			({"email": "a@example.com", "name": "X", "role": "partner"}, "role"),
			({"email": "a@example.com", "name": "X"}, "role"),
		):
			status, body = _call(admin.add_staff, data)
			self.assertEqual(status, 422, data)
			self.assertIn(field, body["error"]["fields"])

	def test_add_cannot_target_administrator(self):
		status, _ = _call(admin.add_staff, {"email": "Administrator", "name": "X", "role": "admin"})
		self.assertEqual(status, 422)

	# ------------------------------------------------------------ update

	def test_change_role_and_revoke_tokens(self):
		token = tokens.issue(self.editor)
		status, body = _call(admin.update_staff, {"id": self.editor, "role": "admin"})
		self.assertEqual(status, 200, body)
		self.assertEqual(body["role"], "admin")
		self.assertEqual(self._roles(self.editor), {ADMIN})
		self.assertEqual(frappe.db.get_value("GX Auth Token", token["_name"], "status"), "Revoked")

	def test_demote_other_admin(self):
		status, body = _call(admin.update_staff, {"id": self.other, "role": "content_editor"})
		self.assertEqual((status, body["role"]), (200, "content_editor"))
		self.assertEqual(self._roles(self.other), {EDITOR})

	def test_demote_keeps_other_roles(self):
		both = self._user(ADMIN, "GrowthX Customer")
		_call(admin.update_staff, {"id": both, "role": "content_editor"})
		self.assertEqual(self._roles(both), {EDITOR, "GrowthX Customer"})

	def test_disable_and_enable_revokes_tokens(self):
		token = tokens.issue(self.editor)
		status, body = _call(admin.update_staff, {"id": self.editor, "enabled": False})
		self.assertEqual((status, body["enabled"]), (200, False))
		self.assertEqual(frappe.db.get_value("User", self.editor, "enabled"), 0)
		self.assertEqual(frappe.db.get_value("GX Auth Token", token["_name"], "status"), "Revoked")
		status, body = _call(admin.update_staff, {"id": self.editor, "enabled": True})
		self.assertEqual((status, body["enabled"]), (200, True))

	def test_update_noop_changes_nothing(self):
		status, body = _call(
			admin.update_staff, {"id": self.editor, "role": "content_editor", "enabled": True}
		)
		self.assertEqual(status, 200, body)
		self.assertFalse(
			frappe.db.exists("GX Audit Log", {"action": "staff.update", "reference_name": self.editor})
		)

	def test_cannot_disable_or_demote_yourself(self):
		for data in ({"enabled": False}, {"role": "content_editor"}):
			status, body = _call(admin.update_staff, {"id": self.me, **data})
			self.assertEqual((status, body["error"]["code"]), (409, "self_lockout"), data)
		self.assertEqual(self._roles(self.me), {ADMIN})
		self.assertTrue(frappe.db.get_value("User", self.me, "enabled"))

	def test_cannot_disable_or_demote_the_last_enabled_admin(self):
		frappe.db.set_value("User", self.me, "enabled", 0)  # the caller is no longer an enabled admin
		for data in ({"enabled": False}, {"role": "content_editor"}):
			status, body = _call(admin.update_staff, {"id": self.other, **data})
			self.assertEqual((status, body["error"]["code"]), (409, "last_admin"), data)
		self.assertEqual(self._roles(self.other), {ADMIN})

	def test_disabled_admin_does_not_count_as_last(self):
		frappe.db.set_value("User", self.other, "enabled", 0)
		status, body = _call(admin.update_staff, {"id": self.other, "role": "content_editor"})
		self.assertEqual(status, 200, body)

	def test_update_validates_and_404s(self):
		status, _ = _call(admin.update_staff, {"id": self.editor})
		self.assertEqual(status, 422)
		status, _ = _call(admin.update_staff, {"id": self.editor, "enabled": "no"})
		self.assertEqual(status, 422)
		status, _ = _call(admin.update_staff, {"id": self.editor, "role": "partner"})
		self.assertEqual(status, 422)
		status, _ = _call(admin.update_staff, {"enabled": False})
		self.assertEqual(status, 422)
		customer = self._user("GrowthX Customer")
		for target in ("nobody@example.com", customer, "Administrator", "Guest"):
			status, _ = _call(admin.update_staff, {"id": target, "enabled": False})
			self.assertEqual(status, 404, target)
		self.assertTrue(frappe.db.get_value("User", "Administrator", "enabled"))

	# ------------------------------------------------------------ remove

	def test_remove_staff_revokes_tokens_and_keeps_the_user(self):
		both = self._user(EDITOR, "GrowthX Customer")
		token = tokens.issue(both)
		status, body = _call(admin.remove_staff, {"id": both})
		self.assertEqual((status, body), (200, {"ok": True}))
		self.assertEqual(self._roles(both), {"GrowthX Customer"})
		self.assertTrue(frappe.db.get_value("User", both, "enabled"))
		self.assertEqual(frappe.db.get_value("GX Auth Token", token["_name"], "status"), "Revoked")
		self.assertTrue(frappe.db.exists("GX Audit Log", {"action": "staff.remove", "reference_name": both}))
		_, listing = _call(admin.staff)
		self.assertNotIn(both, [m["id"] for m in listing["items"]])

	def test_cannot_remove_yourself(self):
		status, body = _call(admin.remove_staff, {"id": self.me})
		self.assertEqual((status, body["error"]["code"]), (409, "self_lockout"))
		self.assertEqual(self._roles(self.me), {ADMIN})

	def test_cannot_remove_the_last_enabled_admin(self):
		frappe.db.set_value("User", self.me, "enabled", 0)
		status, body = _call(admin.remove_staff, {"id": self.other})
		self.assertEqual((status, body["error"]["code"]), (409, "last_admin"))
		self.assertEqual(self._roles(self.other), {ADMIN})

	def test_remove_can_take_another_admin(self):
		status, _ = _call(admin.remove_staff, {"id": self.other})
		self.assertEqual(status, 200)

	def test_remove_404s(self):
		customer = self._user("GrowthX Customer")
		for target in ("nobody@example.com", customer, "Administrator", "Guest"):
			status, _ = _call(admin.remove_staff, {"id": target})
			self.assertEqual(status, 404, target)
