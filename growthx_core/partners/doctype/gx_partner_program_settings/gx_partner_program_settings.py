# Copyright (c) 2026, TBO GrowthX and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.utils import cint, flt


class GXPartnerProgramSettings(Document):
	def validate(self):
		if not 1 <= cint(self.payout_day) <= 28:
			frappe.throw("Payout day must be between 1 and 28.")
		if not 0 <= flt(self.tds_rate) < 100:
			frappe.throw("TDS rate must be between 0 and 100.")
		if flt(self.minimum_payout) < 0:
			frappe.throw("Minimum payout can't be negative.")
		if cint(self.hold_days) < 0:
			frappe.throw("Hold days can't be negative.")
		tiers = sorted(self.tiers, key=lambda t: cint(t.min_active_clients))
		if tiers:
			if cint(tiers[0].min_active_clients) != 0:
				frappe.throw("The first tier must start at 0 active clients.")
			if len({cint(t.min_active_clients) for t in tiers}) != len(tiers):
				frappe.throw("Two tiers can't start at the same number of clients.")
			for t in tiers:
				if not 0 <= flt(t.rate) <= 100:
					frappe.throw(f"Tier {t.tier_name}: the rate must be between 0 and 100.")
