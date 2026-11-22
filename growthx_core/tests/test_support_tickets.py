"""Support tickets: customer endpoints (support.*) and the team's (admin.support_*)."""

import json
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from growthx_core import accounts, tokens
from growthx_core import support as rules
from growthx_core.api.v1 import _core, admin, support

PASSWORD = "Long-Enough-Pass-91"


class TicketCase(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.alice, _ = accounts.create_customer_account(
			email="gx-tkt-alice@example.com", full_name="Alice", company="Alice Co", password=PASSWORD
		)
		self.bob, _ = accounts.create_customer_account(
			email="gx-tkt-bob@example.com", full_name="Bob", company="Bob Co", password=PASSWORD
		)
		self.staff = "gx-tkt-staff@example.com"
		self.staff2 = "gx-tkt-staff2@example.com"
		for email in (self.staff, self.staff2):
			frappe.get_doc(
				{"doctype": "User", "email": email, "first_name": "Staff", "send_welcome_email": 0}
			).insert(ignore_permissions=True).add_roles("GrowthX Admin")
		self.tokens = {u: tokens.issue(u, "web") for u in (self.alice, self.bob, self.staff)}
		self.mail = patch.object(rules.frappe, "sendmail")
		self.sendmail = self.mail.start()

	def tearDown(self):
		self.mail.stop()
		frappe.set_user("Administrator")
		frappe.local.gx_token = None
		frappe.db.rollback()

	def call(self, fn, user, data=None):
		frappe.set_user(user)
		frappe.local.gx_token = self.tokens[user]["_name"]
		frappe.local.form_dict = frappe._dict(data or {})
		frappe.local.request = None
		frappe.local.request_ip = "127.0.0.1"
		with (
			patch.object(_core, "_idempotency_key", return_value=None),
			patch.object(frappe.db, "rollback"),
		):
			response = fn()
		return response.status_code, json.loads(response.get_data())

	def new_ticket(self, user=None, **extra):
		data = {"subject": "Invoice <b>wrong</b>", "message": "Hello\nteam", "category": "billing", **extra}
		status, out = self.call(support.create, user or self.alice, data)
		self.assertEqual(status, 201, out)
		return out["id"]

	def status_of(self, ticket_id):
		return frappe.db.get_value("GX Support Ticket", ticket_id, "status")


class TestCustomerTickets(TicketCase):
	def test_create_saves_ticket_and_emails_support(self):
		tid = self.new_ticket()
		row = frappe.get_doc("GX Support Ticket", tid)
		self.assertEqual((row.status, row.priority, row.customer_user), ("open", "normal", self.alice))
		self.assertEqual(row.account, frappe.db.get_value("GX Account", {"user": self.alice}))
		kwargs = self.sendmail.call_args.kwargs
		self.assertEqual(kwargs["recipients"], ["support@tbogrowthx.com"])
		self.assertEqual(kwargs["reply_to"], self.alice)
		self.assertEqual(kwargs["reference_name"], tid)
		self.assertNotIn("<b>wrong</b>", kwargs["message"])
		self.assertIn("Hello<br>team", kwargs["message"])

	def test_subject_in_email_header_is_one_line(self):
		self.new_ticket(subject="Hi\r\nBcc: evil@example.com")
		subject = self.sendmail.call_args.kwargs["subject"]
		self.assertNotIn("\n", subject)
		self.assertNotIn("\r", subject)

	def test_validation(self):
		for data, field in (
			({"message": "x"}, "subject"),
			({"subject": "x"}, "message"),
			({"subject": "x", "message": "y" * 5001}, "message"),
			({"subject": "x" * 141, "message": "y"}, "subject"),
			({"subject": "x", "message": "y", "category": "nope"}, "category"),
			({"subject": "x", "message": "y", "priority": "urgent"}, "priority"),  # the team's call
			({"subject": "x", "message": "y", "tenantId": "GXT-99999"}, "tenantId"),
		):
			status, out = self.call(support.create, self.alice, data)
			self.assertEqual(status, 422, data)
			self.assertIn(field, out["error"]["fields"])

	def test_cannot_attach_another_accounts_site(self):
		bob_account = frappe.db.get_value("GX Account", {"user": self.bob})
		tenant = frappe.db.get_value("GX Tenant", {"account": bob_account})
		if not tenant:
			self.skipTest("no tenant was provisioned for the test account")
		status, _ = self.call(
			support.create, self.alice, {"subject": "x", "message": "y", "tenantId": tenant}
		)
		self.assertEqual(status, 422)

	def test_customers_only_see_their_own_tickets(self):
		mine = self.new_ticket(self.alice)
		theirs = self.new_ticket(self.bob)
		_, listing = self.call(support.tickets, self.alice)
		self.assertEqual([t["id"] for t in listing["items"]], [mine])
		self.assertEqual(listing["total"], 1)
		for fn, data in (
			(support.ticket, {"id": theirs}),
			(support.reply, {"id": theirs, "message": "hi"}),
			(support.close, {"id": theirs}),
		):
			status, out = self.call(fn, self.alice, data)
			self.assertEqual((status, out["error"]["code"]), (404, "not_found"), fn.__name__)
		self.assertEqual(self.status_of(theirs), "open")
		self.assertEqual(frappe.db.count("GX Ticket Message", {"ticket": theirs}), 1)

	def test_customer_never_sees_internal_notes(self):
		tid = self.new_ticket()
		self.call(admin.support_note, self.staff, {"id": tid, "note": "SECRET refund plan"})
		self.call(admin.support_reply, self.staff, {"id": tid, "message": "Looking into it"})
		status, out = self.call(support.ticket, self.alice, {"id": tid})
		self.assertEqual(status, 200)
		self.assertNotIn("SECRET", json.dumps(out))
		self.assertEqual([m["fromStaff"] for m in out["messages"]], [False, True])
		self.assertNotIn("internal", out["messages"][0])
		self.assertNotIn("assignee", out)
		_, reply = self.call(support.reply, self.alice, {"id": tid, "message": "thanks"})
		self.assertNotIn("SECRET", json.dumps(reply))

	def test_a_staff_token_cannot_use_customer_endpoints_and_vice_versa(self):
		status, _ = self.call(support.tickets, self.staff)
		self.assertEqual(status, 403)
		for fn in (admin.support_tickets, admin.support_summary, admin.support_ticket):
			status, _ = self.call(fn, self.alice, {"id": "x"})
			self.assertEqual(status, 403, fn.__name__)
		for fn in (admin.support_reply, admin.support_note, admin.support_update):
			status, _ = self.call(fn, self.alice, {"id": "x", "message": "y", "note": "y"})
			self.assertEqual(status, 403, fn.__name__)

	def test_customer_cannot_set_status_or_assignee_through_reply(self):
		tid = self.new_ticket()
		self.call(
			support.reply,
			self.alice,
			{"id": tid, "message": "more", "status": "closed", "assigneeId": self.staff},
		)
		doc = frappe.get_doc("GX Support Ticket", tid)
		self.assertEqual((doc.status, doc.assigned_to), ("open", None))

	def test_close_and_closed_takes_no_replies(self):
		tid = self.new_ticket()
		status, out = self.call(support.close, self.alice, {"id": tid})
		self.assertEqual((status, out["status"]), (200, "closed"))
		self.assertTrue(out["closedAt"])
		status, out = self.call(support.reply, self.alice, {"id": tid, "message": "hello?"})
		self.assertEqual((status, out["error"]["code"]), (409, "ticket_closed"))
		status, out = self.call(support.close, self.alice, {"id": tid})
		self.assertEqual(status, 409)

	def test_customer_reply_reopens_resolved(self):
		tid = self.new_ticket()
		self.call(admin.support_update, self.staff, {"id": tid, "status": "resolved"})
		self.assertEqual(self.status_of(tid), "resolved")
		status, out = self.call(support.reply, self.alice, {"id": tid, "message": "Not fixed"})
		self.assertEqual((status, out["status"], out["resolvedAt"]), (200, "open", None))

	def test_customer_reply_emails_assignee_else_support(self):
		tid = self.new_ticket()
		self.call(support.reply, self.alice, {"id": tid, "message": "a <i>b</i>"})
		kwargs = self.sendmail.call_args.kwargs
		self.assertEqual(kwargs["recipients"], ["support@tbogrowthx.com"])
		self.assertIn("a &lt;i&gt;b&lt;/i&gt;", kwargs["message"])
		self.call(admin.support_update, self.staff, {"id": tid, "assigneeId": self.staff2})
		self.call(support.reply, self.alice, {"id": tid, "message": "again"})
		self.assertEqual(self.sendmail.call_args.kwargs["recipients"], [self.staff2])

	def test_a_mail_failure_still_saves(self):
		self.sendmail.side_effect = Exception("smtp down")
		with patch.object(rules.frappe, "log_error") as log_error:
			tid = self.new_ticket()
		self.assertTrue(frappe.db.exists("GX Support Ticket", tid))
		log_error.assert_called()


class TestAdminTickets(TicketCase):
	def test_staff_reply_sets_pending_customer_and_emails_customer(self):
		tid = self.new_ticket()
		status, out = self.call(admin.support_reply, self.staff, {"id": tid, "message": "On it <script>"})
		self.assertEqual((status, out["status"]), (200, "pending_customer"))
		self.assertEqual(out["assignee"]["id"], self.staff)  # a first reply claims it
		self.assertTrue(out["firstResponseAt"])
		kwargs = self.sendmail.call_args.kwargs
		self.assertEqual(kwargs["recipients"], [self.alice])
		# Frappe strips the tag when the message is saved; either way no markup reaches the email.
		self.assertIn("On it", kwargs["message"])
		self.assertNotIn("<script>", kwargs["message"])
		first = out["firstResponseAt"]
		_, out = self.call(admin.support_reply, self.staff, {"id": tid, "message": "More"})
		self.assertEqual(out["firstResponseAt"], first)

	def test_internal_note_is_not_emailed_and_keeps_status(self):
		tid = self.new_ticket()
		self.sendmail.reset_mock()
		status, out = self.call(admin.support_note, self.staff, {"id": tid, "note": "call them"})
		self.assertEqual((status, out["status"]), (200, "open"))
		self.assertTrue(out["messages"][-1]["internal"])
		self.sendmail.assert_not_called()
		self.assertIsNone(out["firstResponseAt"])

	def test_state_transitions(self):
		tid = self.new_ticket()
		ok = [("pending_customer", 200), ("resolved", 200), ("closed", 200), ("open", 200)]
		for target, expected in ok:
			status, out = self.call(admin.support_update, self.staff, {"id": tid, "status": target})
			self.assertEqual((status, out.get("status")), (expected, target))
		self.call(admin.support_update, self.staff, {"id": tid, "status": "closed"})
		status, out = self.call(admin.support_update, self.staff, {"id": tid, "status": "resolved"})
		self.assertEqual((status, out["error"]["code"]), (409, "invalid_transition"))
		status, out = self.call(admin.support_reply, self.staff, {"id": tid, "message": "x"})
		self.assertEqual((status, out["error"]["code"]), (409, "ticket_closed"))
		status, out = self.call(admin.support_update, self.staff, {"id": tid, "status": "bogus"})
		self.assertEqual(status, 422)

	def test_update_priority_assignee_and_audit(self):
		tid = self.new_ticket()
		status, out = self.call(
			admin.support_update, self.staff, {"id": tid, "priority": "urgent", "assigneeId": self.staff2}
		)
		self.assertEqual((status, out["priority"], out["assignee"]["id"]), (200, "urgent", self.staff2))
		details = frappe.db.get_value(
			"GX Audit Log", {"action": "support.update", "reference_name": tid}, "details"
		)
		self.assertIn("priority normal -> urgent", details)
		_, out = self.call(admin.support_update, self.staff, {"id": tid, "assigneeId": None})
		self.assertIsNone(out["assignee"])
		status, _ = self.call(admin.support_update, self.staff, {"id": tid, "assigneeId": self.alice})
		self.assertEqual(status, 422)  # a customer is not staff
		status, _ = self.call(admin.support_update, self.staff, {"id": tid})
		self.assertEqual(status, 422)  # nothing to change
		status, _ = self.call(admin.support_update, self.staff, {"id": "TKT-99999", "priority": "low"})
		self.assertEqual(status, 404)

	def test_replies_and_notes_are_audited(self):
		tid = self.new_ticket()
		self.call(admin.support_reply, self.staff, {"id": tid, "message": "hi"})
		self.call(admin.support_note, self.staff, {"id": tid, "note": "n"})
		actions = frappe.get_all("GX Audit Log", {"reference_name": tid}, pluck="action")
		self.assertTrue({"support.create", "support.reply", "support.note"} <= set(actions))

	def test_list_filters_and_search(self):
		a = self.new_ticket(self.alice, subject="Printer jam")
		b = self.new_ticket(self.bob, subject="Billing question", priority="high", category="billing")
		self.call(admin.support_update, self.staff, {"id": a, "assigneeId": self.staff})

		def ids(**q):
			status, out = self.call(admin.support_tickets, self.staff, q)
			self.assertEqual(status, 200, out)
			return {t["id"] for t in out["items"]}

		self.assertTrue({a, b} <= ids())
		self.assertEqual(ids(priority="high") & {a, b}, {b})
		self.assertEqual(ids(assignee="me") & {a, b}, {a})
		self.assertEqual(ids(assignee="unassigned") & {a, b}, {b})
		self.assertEqual(ids(assignee=self.staff) & {a, b}, {a})
		self.assertEqual(ids(q="printer") & {a, b}, {a})
		self.assertEqual(ids(q="Bob Co") & {a, b}, {b})  # the company name
		self.assertEqual(ids(q=b) & {a, b}, {b})  # the ticket number
		status, out = self.call(admin.support_tickets, self.staff, {"status": "nope"})
		self.assertEqual(status, 422)
		_, listing = self.call(admin.support_tickets, self.staff, {"q": "Bob Co"})
		self.assertEqual(listing["items"][0]["company"], "Bob Co")

	def test_summary_counts(self):
		self.new_ticket()
		tid = self.new_ticket(self.bob)
		self.call(admin.support_update, self.staff, {"id": tid, "priority": "urgent"})
		status, out = self.call(admin.support_summary, self.staff)
		self.assertEqual(status, 200)
		self.assertGreaterEqual(out["open"], 2)
		self.assertGreaterEqual(out["unassigned"], 2)
		self.assertGreaterEqual(out["urgent"], 1)
		self.assertIn(self.staff, [u["id"] for u in out["staff"]])
		self.assertNotIn(self.alice, [u["id"] for u in out["staff"]])

	def test_detail_has_internal_notes_for_staff(self):
		tid = self.new_ticket()
		self.call(admin.support_note, self.staff, {"id": tid, "note": "private"})
		_, out = self.call(admin.support_ticket, self.staff, {"id": tid})
		self.assertEqual([m["internal"] for m in out["messages"]], [False, True])
