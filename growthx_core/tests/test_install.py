import frappe
from frappe.email.email_body import get_footer
from frappe.tests import IntegrationTestCase

from growthx_core.setup import install


class TestEmailFooter(IntegrationTestCase):
	"""No "Sent via ERPNext" (a white-label leak that also looks like phishing to spam filters)."""

	def tearDown(self):
		frappe.db.rollback()

	def _reset(self, footer_address=""):
		for field, value in (("disable_standard_email_footer", 0), ("email_footer_address", footer_address)):
			frappe.db.set_single_value("System Settings", field, value)
			frappe.db.set_default(field, value)

	def test_emails_have_no_erpnext_footer(self):
		self._reset()
		self.assertIn("ERPNext", get_footer(None))  # Frappe's default, before

		install.apply_settings()

		footer = get_footer(None)
		self.assertNotIn("ERPNext", footer)
		self.assertNotIn("frappe.io", footer)
		self.assertIn("TBO GrowthX", footer)

	def test_a_footer_someone_set_is_kept(self):
		self._reset(footer_address="GrowthX, Kochi")
		install.apply_settings()
		footer = get_footer(None)
		self.assertIn("GrowthX, Kochi", footer)
		self.assertNotIn("ERPNext", footer)
