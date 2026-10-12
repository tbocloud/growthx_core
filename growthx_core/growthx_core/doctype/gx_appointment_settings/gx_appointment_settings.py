# Copyright (c) 2026, TBO GrowthX and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.utils import cint, get_time, validate_email_address


class GXAppointmentSettings(Document):
	def validate(self):
		if cint(self.duration_minutes) < 5:
			frappe.throw("Call length must be at least 5 minutes.")
		if get_time(self.day_end) <= get_time(self.day_start):
			frappe.throw("Closing time must be after opening time.")
		for email in self.expert_list():
			if not validate_email_address(email):
				frappe.throw(f"Not a valid expert email: {email}")

	def expert_list(self) -> list[str]:
		return [e.strip() for e in (self.experts or "").splitlines() if e.strip()]
