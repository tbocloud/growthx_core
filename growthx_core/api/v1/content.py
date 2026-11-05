"""Public website content (blog) for the six marketing sites' servers. Phase 1: posts only.

Live posts only: `published`, or `scheduled` once `published_at` has passed. Editors write posts in
Core desk (GX Website Post). Text-only until S3 arrives in phase 2, so image fields are always null.
"""

import frappe
from frappe.utils import now_datetime

from growthx_core import website_content as wc
from growthx_core.api.v1._core import (
	ApiError,
	RateLimit,
	endpoint,
	get_str,
	iso,
	not_found,
	page_meta,
	page_params,
	query,
	validation_error,
)

POSTS_PAGE_SIZE = 12
# Visitors' requests reach Core through the sites' servers, which send the web key and are not limited.
PUBLIC_LIMIT = RateLimit(60, 60, skip_with_web_key=True)

SUMMARY_FIELDS = ["slug", "title", "excerpt", "body", "author_name", "tags", "published_at", "modified"]


@endpoint(auth=False, rate_limit=PUBLIC_LIMIT)
def posts():
	data = query()
	site = _site(data)
	page, _ = page_params({"page": data.get("page")})
	tag = get_str(data, "tag", max_length=wc.TAG_MAX)
	if tag is not None and not wc.SLUG_RE.match(tag):
		raise validation_error({"tag": "Lowercase letters, numbers and hyphens only."})

	filters, or_filters = _live_filters(site), None
	if tag:
		or_filters = [
			["tags", "=", tag],
			["tags", "like", f"{tag}, %"],
			["tags", "like", f"%, {tag}"],
			["tags", "like", f"%, {tag}, %"],
		]
	rows = frappe.get_all(
		"GX Website Post",
		filters=filters,
		or_filters=or_filters,
		fields=SUMMARY_FIELDS,
		order_by="published_at desc, creation desc",
		offset=(page - 1) * POSTS_PAGE_SIZE,
		limit=POSTS_PAGE_SIZE,
	)
	total = (
		len(frappe.get_all("GX Website Post", filters=filters, or_filters=or_filters, pluck="name"))
		if or_filters
		else frappe.db.count("GX Website Post", filters)
	)
	return page_meta(page, POSTS_PAGE_SIZE, total, [_summary(r) for r in rows])


@endpoint(auth=False, rate_limit=PUBLIC_LIMIT)
def post():
	data = query()
	site = _site(data)
	slug = get_str(data, "slug", required=True, max_length=wc.SLUG_MAX, label="Slug")
	if not wc.SLUG_RE.match(slug):
		raise not_found()
	rows = frappe.get_all(
		"GX Website Post",
		filters={**_live_filters(site), "slug": slug},
		fields=[*SUMMARY_FIELDS, "seo_title", "seo_description", "noindex", "canonical_url"],
		limit=1,
	)
	if not rows:
		raise not_found()
	r = rows[0]
	return {
		**_summary(r),
		"body": r.body,
		"seo": {
			"title": r.seo_title or None,
			"description": r.seo_description or None,
			"ogImage": None,  # phase 2 (S3)
			"noindex": bool(r.noindex),
		},
		"canonicalUrl": r.canonical_url or None,
	}


@endpoint(auth=False, rate_limit=PUBLIC_LIMIT)
def sitemap():
	site = _site(query())
	rows = frappe.get_all(
		"GX Website Post",
		filters={**_live_filters(site), "noindex": 0},
		or_filters=[["canonical_url", "is", "not set"], ["canonical_url", "=", ""]],
		fields=["slug", "modified"],
		order_by="published_at desc, creation desc",
	)
	return {"items": [{"slug": r.slug, "updatedAt": iso(r.modified)} for r in rows]}


def _site(data) -> str:
	site = get_str(data, "site", required=True, label="Site")
	if site not in wc.SITES:
		raise ApiError(404, "site_not_found", "Unknown site.")
	return site


def _live_filters(site: str) -> dict:
	return {
		"site": site,
		"status": ("in", ("published", "scheduled")),
		"published_at": ("<=", now_datetime()),
	}


def _summary(r) -> dict:
	return {
		"slug": r.slug,
		"title": r.title,
		"excerpt": r.excerpt or wc.plain_excerpt(r.body),
		"coverImage": None,  # phase 2 (S3)
		"coverAlt": None,
		"authorName": r.author_name or None,
		"tags": [t.strip() for t in (r.tags or "").split(",") if t.strip()],
		"publishedAt": iso(r.published_at),
		"updatedAt": iso(r.modified),
	}
