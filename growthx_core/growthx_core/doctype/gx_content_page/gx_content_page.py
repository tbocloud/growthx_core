# Copyright (c) 2026, TBO GrowthX and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from growthx_core import website_content as wc

TEXT_LIMITS = {"seo_title": 70, "seo_description": 200}


class GXContentPage(Document):
	"""Same checks for desk and /content (admin.save_content_page). A failed check sets
	`flags.problem_field`, which the API reports as that field's error."""

	def validate(self):
		self.path = wc.normalise_path(self.path)
		problem = wc.page_path_problem(self.path)
		if problem:
			self._fail("path", problem)
		taken = frappe.db.exists(
			"GX Content Page", {"site": self.site, "path": self.path, "name": ("!=", self.name or "")}
		)
		if taken:
			self._fail("path", _("{0} already has SEO for {1}.").format(self.site, self.path))
		for field, limit in TEXT_LIMITS.items():
			value = (self.get(field) or "").strip()
			self.set(field, value or None)
			if len(value) > limit:
				self._fail(field, _("At most {0} characters.").format(limit))
			if any(c in value for c in "<>"):
				self._fail(field, _("{0} can't contain < or >.").format(self.meta.get_label(field)))
		self.og_image = (self.og_image or "").strip() or None
		if self.og_image and not wc.media_url_ok(self.og_image):
			self._fail("og_image", wc.MEDIA_ONLY)

	def on_trash(self):
		wc.detach_audit_on_desk_delete(self, f"{self.site} {self.path}")

	def _fail(self, field, message):
		self.flags.problem_field = field
		frappe.throw(message, title=self.meta.get_label(field))
