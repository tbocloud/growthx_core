# Copyright (c) 2026, TBO GrowthX and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

from growthx_core.provisioning.apps import copy_product_apps, validate_app_rows


class GXTenant(Document):
	def before_insert(self):
		copy_product_apps(self)

	def validate(self):
		if self.get("apps") or self.is_new():
			validate_app_rows(self, f"Site {self.display_name or self.name}")

	@frappe.whitelist()
	def archive_failed_site(self):
		"""The form's "Archive site" button (failed sites only)."""
		from growthx_core.api.v1._core import audit
		from growthx_core.provisioning import runner

		frappe.only_for(("System Manager", "GrowthX Admin"))
		self.check_permission("write")
		if not runner.archive_failed(self.name):
			frappe.throw("Only a failed site can be archived.")
		audit("archive_tenant", "GX Tenant", self.name)
