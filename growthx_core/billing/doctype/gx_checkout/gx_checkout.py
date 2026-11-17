# Copyright (c) 2026, TBO GrowthX and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class GXCheckout(Document):
	"""Created and updated only by growthx_core.billing.payu (read-only in desk)."""
