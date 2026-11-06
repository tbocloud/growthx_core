"""Public website content for the six marketing sites' servers: SEO settings (content.site) and the
blog (content.posts, content.post, content.sitemap).

Live posts only: `published`, or `scheduled` once `published_at` has passed, and only while the
site's blog is on. Editors work in Core desk or the /content portal. Images are on the media host (S3).
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

SUMMARY_FIELDS = [
	"slug",
	"title",
	"excerpt",
	"body",
	"author_name",
	"tags",
	"published_at",
	"modified",
	"cover_image",
	"cover_alt",
]


@endpoint(auth=False, rate_limit=PUBLIC_LIMIT)
def posts():
	data = query()
	site = _site(data)
	page, _ = page_params({"page": data.get("page")})
	tag = get_str(data, "tag", max_length=wc.TAG_MAX)
	if tag is not None and not wc.SLUG_RE.match(tag):
		raise validation_error({"tag": "Lowercase letters, numbers and hyphens only."})

	if not wc.blog_enabled(site):
		return page_meta(page, POSTS_PAGE_SIZE, 0, [])
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
	if not wc.SLUG_RE.match(slug) or not wc.blog_enabled(site):
		raise not_found()
	rows = frappe.get_all(
		"GX Website Post",
		filters={**_live_filters(site), "slug": slug},
		fields=[*SUMMARY_FIELDS, "seo_title", "seo_description", "og_image", "noindex", "canonical_url"],
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
			"ogImage": r.og_image or None,
			"noindex": bool(r.noindex),
		},
		"canonicalUrl": r.canonical_url or None,
	}


@endpoint(auth=False, rate_limit=PUBLIC_LIMIT)
def sitemap():
	site = _site(query())
	if not wc.blog_enabled(site):
		return {"items": []}
	rows = frappe.get_all(
		"GX Website Post",
		filters={**_live_filters(site), "noindex": 0},
		or_filters=[["canonical_url", "is", "not set"], ["canonical_url", "=", ""]],
		fields=["slug", "modified"],
		order_by="published_at desc, creation desc",
	)
	return {"items": [{"slug": r.slug, "updatedAt": iso(r.modified)} for r in rows]}


@endpoint(auth=False, rate_limit=PUBLIC_LIMIT)
def site():
	"""Everything a site needs besides posts: SEO defaults, page SEO and (later) redirects."""
	slug = _site(query())
	wc.ensure_sites()
	doc = frappe.get_doc("GX Content Site", slug)
	pages = frappe.get_all(
		"GX Content Page",
		{"site": slug},
		["path", "seo_title", "seo_description", "og_image", "noindex"],
		order_by="path asc",
	)
	return {
		"site": doc.site,
		"siteName": doc.site_name,
		"baseUrl": doc.base_url,
		"titleTemplate": doc.title_template,
		"defaults": {
			"title": doc.default_title or None,
			"description": doc.default_description or None,
			"ogImage": doc.default_og_image or None,
		},
		"verification": {"google": doc.google_verification or None, "bing": doc.bing_verification or None},
		# Saved by editors, sent only once management approves tracking.
		"analyticsId": (doc.analytics_id or None) if wc.analytics_active() else None,
		"blogEnabled": bool(doc.blog_enabled),
		"pages": [
			{
				"path": p.path,
				"title": p.seo_title or None,
				"description": p.seo_description or None,
				"ogImage": p.og_image or None,
				"noindex": bool(p.noindex),
			}
			for p in pages
		],
		"redirects": [],  # redirects come in a later step
		"version": wc.site_version(slug),
	}


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
		"coverImage": r.cover_image or None,
		"coverAlt": r.cover_alt or None,
		"authorName": r.author_name or None,
		"tags": [t.strip() for t in (r.tags or "").split(",") if t.strip()],
		"publishedAt": iso(r.published_at),
		"updatedAt": iso(r.modified),
	}
