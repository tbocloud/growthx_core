# Copyright (c) 2026, TBO GrowthX and contributors
# For license information, please see license.txt

from frappe.model.document import Document
from frappe.utils import cint

from growthx_core.provisioning.apps import validate_app_rows


class GXProduct(Document):
	def validate(self):
		validate_app_rows(
			self,
			f"Product {self.product_name or self.name}",
			require_theme=bool(cint(self.install_theme if self.install_theme is not None else 1)),
		)
