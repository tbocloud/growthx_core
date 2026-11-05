"""contact.submit saves a GX Contact Request and emails it to support."""

import json
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from growthx_core.api.v1 import contact

EMAIL = "gx-contact-visitor@example.com"


class TestContactSubmit(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Guest")

	def tearDown(self):
		frappe.set_user("Administrator")
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
