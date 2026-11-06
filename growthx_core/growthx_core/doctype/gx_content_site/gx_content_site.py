# Copyright (c) 2026, TBO GrowthX and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from growthx_core import website_content as wc

TEXT_LIMITS = {"site_name": 60, "title_template": 80, "default_title": 70, "default_description": 200}
CODES = {
	"google_verification": (wc.GOOGLE_VERIFICATION_RE, "10 to 100 letters, digits, - or _ (only the code)."),
	"bing_verification": (wc.BING_VERIFICATION_RE, "32 hexadecimal characters (only the code)."),
	"analytics_id": (wc.GA4_RE, "A GA4 measurement ID such as G-AB12CD34EF."),
}


class GXContentSite(Document):
	"""Same checks for desk and /content (admin.update_content_site). A failed check sets
	`flags.problem_field`, which the API reports as that field's error."""

	def validate(self):
		if self.site not in wc.SITES:
			self._fail("site", _("Unknown site."))
		self.base_url = wc.SITE_BASE_URLS[self.site]  # Core owns it
		for field, limit in TEXT_LIMITS.items():
			value = (self.get(field) or "").strip()
			self.set(field, value or None)
			if field in ("site_name", "title_template") and not value:
				self._fail(field, _("{0} is required.").format(self.meta.get_label(field)))
			if len(value) > limit:
				self._fail(field, _("At most {0} characters.").format(limit))
			# Frappe's XSS filter would store "A > B" as "A &gt; B": refuse < and > instead.
			if any(c in value for c in "<>"):
				self._fail(field, _("{0} can't contain < or >.").format(self.meta.get_label(field)))
		if (self.title_template or "").count("%s") != 1:
			self._fail("title_template", _("Must contain %s exactly once, where the page title goes."))
		for field, (pattern, message) in CODES.items():
			value = (self.get(field) or "").strip()
			self.set(field, value or None)
			if value and not pattern.match(value):
				self._fail(field, message)
		if self.default_og_image:
			self._fail("default_og_image", _("Images aren't supported yet."))

	def on_trash(self):
		frappe.throw(_("Sites are set up by Core and can't be deleted."))

	def _fail(self, field, message):
		self.flags.problem_field = field
		frappe.throw(message, title=self.meta.get_label(field))
