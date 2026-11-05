# Copyright (c) 2026, TBO GrowthX and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import get_datetime, now_datetime

from growthx_core import website_content as wc


class GXWebsitePost(Document):
	def validate(self):
		self.title = (self.title or "").strip()
		self._validate_slug()
		self._validate_tags()
		self._validate_status()
		self._validate_text()

	def _validate_slug(self):
		self.slug = (self.slug or "").strip().lower() or wc.slugify(self.title)
		if not self.slug or len(self.slug) > wc.SLUG_MAX or not wc.SLUG_RE.match(self.slug):
			frappe.throw(
				_(
					"Slug must be lowercase letters, numbers and single hyphens, at most {0} characters."
				).format(wc.SLUG_MAX),
				title=_("Slug"),
			)
		taken = frappe.db.exists(
			"GX Website Post", {"site": self.site, "slug": self.slug, "name": ("!=", self.name or "")}
		)
		if taken:
			frappe.throw(
				_("Another post on {0} already uses the slug '{1}'.").format(self.site, self.slug),
				frappe.DuplicateEntryError,
				title=_("Slug"),
			)

	def _validate_tags(self):
		try:
			self.tags = ", ".join(wc.parse_tags(self.tags))
		except ValueError as e:
			frappe.throw(str(e), title=_("Tags"))

	def _validate_status(self):
		changed = self.is_new() or self.has_value_changed("status") or self.has_value_changed("published_at")
		if self.status == "published" and not self.published_at:
			self.published_at = now_datetime()
		if self.status == "scheduled":
			if not self.published_at:
				frappe.throw(_("Set Published At for a scheduled post."), title=_("Published At"))
			if changed and get_datetime(self.published_at) <= now_datetime():
				frappe.throw(
					_(
						"Published At must be in the future for a scheduled post. To go live now, choose published."
					),
					title=_("Published At"),
				)
		if self.status == "published" and changed and get_datetime(self.published_at) > now_datetime():
			frappe.throw(
				_("Published At is in the future. Choose scheduled to publish it later."),
				title=_("Published At"),
			)

	def _validate_text(self):
		problem = wc.body_problem(self.body)
		if problem:
			frappe.throw(problem, title=_("Body"))
		limits = (("excerpt", wc.EXCERPT_MAX), ("seo_title", 70), ("seo_description", 200))
		for field, limit in limits:
			value = (self.get(field) or "").strip()
			self.set(field, value or None)
			if len(value) > limit:
				frappe.throw(
					_("{0} must be at most {1} characters.").format(self.meta.get_label(field), limit)
				)
		self.author_name = (self.author_name or "").strip() or None
		# Plain-text fields keep Frappe's XSS filter (desk shows them unescaped), which would store
		# "A > B" as "A &gt; B". Refuse < and > instead, so they are always stored exactly as typed.
		# The body skips the filter (ignore_xss_filter): it is Markdown, kept as typed.
		for field in ("title", "author_name", "excerpt", "seo_title", "seo_description"):
			if any(c in (self.get(field) or "") for c in "<>"):
				frappe.throw(
					_("{0} can't contain < or >.").format(self.meta.get_label(field)),
					title=self.meta.get_label(field),
				)
		self.canonical_url = (self.canonical_url or "").strip() or None
		if self.canonical_url and not wc.canonical_allowed(self.canonical_url):
			frappe.throw(
				_("Canonical URL must be an https:// address on {0} (or www.).").format(
					", ".join(wc.ALLOWED_DOMAINS)
				),
				title=_("Canonical URL"),
			)
