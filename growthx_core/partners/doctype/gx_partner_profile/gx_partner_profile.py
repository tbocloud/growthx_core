# Copyright (c) 2026, TBO GrowthX and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class GXPartnerProfile(Document):
	def validate(self):
		self.referral_code = (self.referral_code or "").strip().upper()
		if self.status == "active" and not self.sales_partner:
			self.sales_partner = self._create_sales_partner()

	def _create_sales_partner(self) -> str:
		"""ERPNext Sales Partner is the system of record for commission (rate set by an admin, D8)."""
		name = self.partner_name
		if frappe.db.exists("Sales Partner", name):
			name = f"{self.partner_name} ({self.referral_code})"
		territory = frappe.db.get_single_value("Selling Settings", "territory") or "All Territories"
		doc = frappe.get_doc(
			{
				"doctype": "Sales Partner",
				"partner_name": name,
				"territory": territory,
				"commission_rate": frappe.conf.get("growthx_default_commission_rate") or 0,
			}
		)
		doc.insert(ignore_permissions=True)
		return doc.name
