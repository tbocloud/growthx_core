"""contact.submit saves a GX Contact Request and emails it to support."""

import json
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from growthx_core import accounts
from growthx_core.api.v1 import _core, admin, contact

EMAIL = "gx-contact-visitor@example.com"


class TestContactSubmit(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Guest")

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.local.gx_token = None
		frappe.db.rollback()

	def submit(self, **extra):
		data = {
			"name": "Asha <b>Rao</b>",
			"email": EMAIL,
			"company": "Sun Co",
			"message": "Line one\nLine two",
		}
		frappe.local.request = None
		frappe.local.request_ip = "203.0.113.20"
		frappe.local.form_dict = frappe._dict({**data, **extra})
		with patch.object(contact.frappe, "sendmail") as sendmail:
			response = contact.submit()
		return response, sendmail

	def test_saves_and_emails_support(self):
		response, sendmail = self.submit()
		self.assertEqual(response.status_code, 202)
		name = frappe.db.get_value("GX Contact Request", {"email": EMAIL}, "name")
		self.assertTrue(name)
		kwargs = sendmail.call_args.kwargs
		self.assertEqual(kwargs["recipients"], ["support@tbogrowthx.com"])
		self.assertEqual(kwargs["reply_to"], EMAIL)
		self.assertEqual(
			(kwargs["reference_doctype"], kwargs["reference_name"]), ("GX Contact Request", name)
		)
		self.assertIn("Sun Co", kwargs["subject"])
		self.assertIn("Line one<br>Line two", kwargs["message"])
		self.assertIn("Asha &lt;b&gt;Rao&lt;/b&gt;", kwargs["message"])  # escaped
		self.assertNotIn("<b>Rao</b>", kwargs["message"])

	def test_subject_is_one_line(self):
		_, sendmail = self.submit(name="Asha\r\nBcc: x@example.com", company="Sun\n  Co")
		subject = sendmail.call_args.kwargs["subject"]
		self.assertEqual(subject, "Contact request from Asha Bcc: x@example.com (Sun Co)")
		self.assertNotIn("\n", subject)
		self.assertNotIn("\r", subject)

	def test_core_link_says_it_needs_a_login(self):
		_, sendmail = self.submit()
		self.assertIn("(needs a Core login)", sendmail.call_args.kwargs["message"])

	def test_site_config_overrides_the_address(self):
		with patch.dict(frappe.conf, {"growthx_support_email": "help@example.com"}):
			_, sendmail = self.submit()
		self.assertEqual(sendmail.call_args.kwargs["recipients"], ["help@example.com"])

	def test_a_mail_failure_still_saves_the_request(self):
		frappe.local.form_dict = frappe._dict({"name": "A", "email": EMAIL, "message": "Hi"})
		frappe.local.request = None
		frappe.local.request_ip = "203.0.113.21"
		with (
			patch.object(contact.frappe, "sendmail", side_effect=Exception("smtp down")),
			patch.object(contact.frappe, "log_error") as log_error,
		):
			response = contact.submit()
		self.assertEqual(json.loads(response.get_data()), {"ok": True})
		self.assertTrue(frappe.db.exists("GX Contact Request", {"email": EMAIL}))
		log_error.assert_called_once()

	def test_public_form_is_source_contact_without_a_customer(self):
		self.submit()
		row = frappe.db.get_value(
			"GX Contact Request", {"email": EMAIL}, ["source", "customer"], as_dict=True
		)
		self.assertEqual((row.source, row.customer), ("contact", None))

	def test_support_form_takes_the_customer_from_the_token(self):
		frappe.set_user("Administrator")
		user, _ = accounts.create_customer_account(
			email="gx-contact-cust@example.com",
			full_name="Support Cust",
			company="Support Co",
			password="Long-Enough-Pass-91",
		)
		account = frappe.db.get_value("GX Account", {"user": user})
		other = frappe.get_all("GX Account", {"name": ("!=", account)}, pluck="name", limit=1)
		frappe.set_user(user)
		frappe.local.gx_token = "test-token"
		_, sendmail = self.submit(source="support", customer=other[0] if other else "ignored")
		row = frappe.db.get_value(
			"GX Contact Request", {"email": EMAIL}, ["source", "customer"], as_dict=True
		)
		self.assertEqual((row.source, row.customer), ("support", account))  # never from the body
		self.assertTrue(sendmail.call_args.kwargs["subject"].startswith("Support request from"))

	def test_support_without_a_token_has_no_customer(self):
		self.submit(source="support")
		self.assertIsNone(frappe.db.get_value("GX Contact Request", {"email": EMAIL}, "customer"))

	def test_unknown_source_is_rejected(self):
		response, _ = self.submit(source="sales")
		self.assertEqual(response.status_code, 422)
		self.assertIn("source", json.loads(response.get_data())["error"]["fields"])


def _admin_list(**data):
	frappe.local.form_dict = frappe._dict(data)
	frappe.local.request = None
	with patch.object(_core, "_require_auth"):  # the caller is an admin
		response = admin.contact_requests()
	return response.status_code, json.loads(response.get_data())


class TestAdminContactRequests(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		frappe.db.delete("GX Contact Request")  # rolled back in tearDown
		self.old = self._add(
			"Old Visitor", "old@example.com", None, "Hello from last month", "2026-10-01 10:00:00"
		)
		self.new = self._add(
			"Ravi Kumar", "ravi@example.com", "Sun Co", "Invoice question", "2026-11-04 09:00:00"
		)
		self.support = self._add(
			"Asha", "asha@example.com", "Asha Solar", "Site is slow", "2026-11-05 08:00:00", source="support"
		)

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.local.gx_token = None
		frappe.db.rollback()

	def _add(self, name, email, company, message, created, source="contact"):
		doc = frappe.get_doc(
			{
				"doctype": "GX Contact Request",
				"full_name": name,
				"email": email,
				"company": company,
				"message": message,
				"source": source,
			}
		).insert(ignore_permissions=True)
		frappe.db.set_value("GX Contact Request", doc.name, "creation", created, update_modified=False)
		return doc.name

	def test_newest_first_with_the_item_shape(self):
		status, body = _admin_list()
		self.assertEqual(status, 200, body)
		self.assertEqual([i["id"] for i in body["items"]], [self.support, self.new, self.old])
		self.assertEqual(body["total"], 3)
		self.assertEqual(
			body["items"][1],
			{
				"id": self.new,
				"name": "Ravi Kumar",
				"email": "ravi@example.com",
				"company": "Sun Co",
				"message": "Invoice question",
				"source": "contact",
				"customerId": None,
				"createdAt": body["items"][1]["createdAt"],
			},
		)
		self.assertIsNone(body["items"][2]["company"])

	def test_search_source_and_dates(self):
		self.assertEqual([i["id"] for i in _admin_list(q="invoice")[1]["items"]], [self.new])
		self.assertEqual([i["id"] for i in _admin_list(q="asha solar")[1]["items"]], [self.support])
		self.assertEqual([i["id"] for i in _admin_list(source="support")[1]["items"]], [self.support])
		body = _admin_list(**{"from": "2026-11-04", "to": "2026-11-04"})[1]
		self.assertEqual(([i["id"] for i in body["items"]], body["total"]), ([self.new], 1))
		self.assertEqual(len(_admin_list(to="2026-10-31")[1]["items"]), 1)

	def test_pagination(self):
		body = _admin_list(page=2, pageSize=2)[1]
		self.assertEqual(([i["id"] for i in body["items"]], body["total"]), ([self.old], 3))

	def test_bad_dates(self):
		status, body = _admin_list(**{"from": "2026-11-05", "to": "2026-11-01"})
		self.assertEqual((status, list(body["error"]["fields"])), (422, ["from"]))
		self.assertEqual(_admin_list(to="not-a-date")[0], 422)

	def test_admins_only(self):
		frappe.set_user("Administrator")
		user, _ = accounts.create_customer_account(
			email="gx-contact-notadmin@example.com",
			full_name="Not Admin",
			company="Not Admin Co",
			password="Long-Enough-Pass-91",
		)
		frappe.set_user(user)
		frappe.local.gx_token = "test-token"
		frappe.local.form_dict = frappe._dict()
		frappe.local.request = None
		self.assertEqual(admin.contact_requests().status_code, 403)
