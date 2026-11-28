# Copyright (c) 2026, TBO GrowthX and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class GXPlan(Document):
	def validate(self):
		# Every plan price is the final price, GST included.
		self.gst_included = 1
