# Copyright (c) 2026, TBO GrowthX and contributors
# For license information, please see license.txt

from frappe.model.document import Document

from growthx_core.provisioning.apps import copy_product_apps, validate_app_rows


class GXTenant(Document):
	def before_insert(self):
		copy_product_apps(self)

	def validate(self):
		if self.get("apps") or self.is_new():
			validate_app_rows(self, f"Site {self.display_name or self.name}")
