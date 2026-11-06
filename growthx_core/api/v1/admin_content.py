"""Website content for the /content portal: posts (admin.content_posts, admin.content_post,
admin.save_content_post, admin.delete_content_post). Re-exported from admin.py, so the paths are
`growthx_core.api.v1.admin.*`.

For the `admin` and `content_editor` roles. The checks are GX Website Post's own (the same as in
desk); this module maps them to the contract's fields. Images must come from admin.upload_content_image.
"""

from zoneinfo import ZoneInfo

import frappe
from frappe.query_builder.functions import Count
from frappe.utils import get_datetime, get_system_timezone
from pypika.functions import Coalesce, Concat

from growthx_core import website_content as wc
from growthx_core.api.v1._core import (
	ApiError,
	audit,
	body,
	endpoint,
	get_choice,
	get_str,
	iso,
	not_found,
	page_meta,
	page_params,
	query,
	validation_error,
)

CONTENT_ROLES = ("admin", "content_editor")
POST = "GX Website Post"

# GX Website Post field -> the contract's field, for error messages.
API_FIELDS = {
	"site": "site",
	"slug": "slug",
	"title": "title",
	"excerpt": "excerpt",
	"body": "body",
	"author_name": "authorName",
	"tags": "tags",
	"status": "status",
	"published_at": "publishedAt",
	"seo_title": "seo.title",
	"seo_description": "seo.description",
	"canonical_url": "canonicalUrl",
	"cover_image": "coverImage",
	"cover_alt": "coverAlt",
	"og_image": "seo.ogImage",
}


@endpoint(roles=CONTENT_ROLES)
def content_posts():
	data = query()
	site = get_choice(data, "site", wc.SITES, required=True)
	status = get_choice(data, "status", wc.STATUSES)
	tag = get_str(data, "tag", max_length=wc.TAG_MAX)
	if tag is not None and not wc.SLUG_RE.match(tag):
		raise validation_error({"tag": "Lowercase letters, numbers and hyphens only."})
	q = get_str(data, "q", max_length=100)
	page, page_size = page_params(data)

	P = frappe.qb.DocType(POST)
	where = P.site == site
	if status:
		where &= P.status == status
	if tag:  # tags are stored as "a, b, c"
		where &= Concat(", ", Coalesce(P.tags, ""), ", ").like(f"%, {tag}, %")
	if q:
		like = f"%{q}%"
		where &= P.title.like(like) | P.slug.like(like) | P.excerpt.like(like)

	total = frappe.qb.from_(P).select(Count(P.name)).where(where).run()[0][0]
	rows = (
		frappe.qb.from_(P)
		.select(*[P[f] for f in ("name", *_SUMMARY_FIELDS)])
		.where(where)
		.orderby(P.modified, order=frappe.qb.desc)
		.limit(page_size)
		.offset((page - 1) * page_size)
		.run(as_dict=True)
	)
	return page_meta(page, page_size, total, [_summary(r) for r in rows])


@endpoint(roles=CONTENT_ROLES)
def content_post():
	return _full(_get(get_str(query(), "id", required=True)))


@endpoint(methods=("POST",), roles=CONTENT_ROLES)
def save_content_post():
	data = body()
	post_id = get_str(data, "id")
	doc = _get(post_id) if post_id else frappe.new_doc(POST)
	expected = get_str(data, "expectedUpdatedAt", max_length=40)
	if post_id and expected and _utc_second(expected) != iso(doc.modified):
		raise ApiError(
			409, "edit_conflict", "Someone else changed this post. Reload it to see their changes."
		)

	values = _input(data)
	taken = frappe.db.get_value(
		POST, {"site": values["site"], "slug": values["slug"], "name": ("!=", post_id or "")}
	)
	if taken:
		raise ApiError(
			409, "slug_taken", "Another post on this site already uses this slug.", fields={"slug": "Taken."}
		)

	was_new = doc.is_new()
	doc.update(values)
	try:
		doc.save()
	except frappe.DuplicateEntryError:
		raise ApiError(409, "slug_taken", "Another post on this site already uses this slug.")
	except frappe.ValidationError as e:
		field = API_FIELDS.get(doc.flags.problem_field)
		if not field:
			raise
		raise validation_error({field: _message(e)})

	audit(
		"content.post_created" if was_new else "content.post_updated",
		POST,
		doc.name,
		details=f"{doc.title} ({doc.site}/blog/{doc.slug}, {doc.status})",
	)
	return _full(doc)


@endpoint(methods=("POST",), roles=CONTENT_ROLES)
def delete_content_post():
	doc = _get(get_str(body(), "id", required=True))
	# Logged first: the audit row's link must point at an existing post when it's written.
	audit("content.post_deleted", POST, doc.name, details=f"{doc.title} ({doc.site}/blog/{doc.slug})")
	frappe.delete_doc(POST, doc.name)
	return {"ok": True}


# ---------------------------------------------------------------- input


def _input(data: dict) -> dict:
	"""The request body as GX Website Post values. Replaces the whole post (send every field)."""
	errors = {}

	def take(fn, *args, **kwargs):
		try:
			return fn(*args, **kwargs)
		except ApiError as e:
			errors.update(e.fields or {})

	site = take(get_choice, data, "site", wc.SITES, required=True)
	slug = take(get_str, data, "slug", required=True, max_length=wc.SLUG_MAX, label="Slug")
	if slug and not wc.SLUG_RE.match(slug):
		errors["slug"] = "Lowercase letters, numbers and single hyphens only."
	title = take(get_str, data, "title", required=True, max_length=150, label="Title")
	excerpt = take(get_str, data, "excerpt", max_length=wc.EXCERPT_MAX, label="Excerpt")
	author = take(get_str, data, "authorName", max_length=100, label="Author name")
	status = take(get_choice, data, "status", wc.STATUSES, required=True)
	canonical = take(get_str, data, "canonicalUrl", max_length=500, label="Canonical URL")

	post_body = data.get("body")  # kept exactly as typed: no strip
	if not isinstance(post_body, str) or not post_body.strip():
		errors["body"] = "Body is required."
		post_body = None

	tags = data.get("tags") or []
	if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
		errors["tags"] = "Tags must be a list of strings."
		tags = []

	published_at = None
	if data.get("publishedAt") not in (None, ""):
		published_at = _site_datetime(data.get("publishedAt"))
		if published_at is None:
			errors["publishedAt"] = "Must be an ISO 8601 date and time."

	seo = data.get("seo") or {}
	if not isinstance(seo, dict):
		errors["seo"] = "Must be an object."
		seo = {}
	seo_title = take(_seo_str, seo, "title", 70, "SEO title")
	seo_description = take(_seo_str, seo, "description", 200, "SEO description")

	# Images: addresses from admin.upload_content_image (checked by the doctype).
	cover_image = take(get_str, data, "coverImage", max_length=500, label="Cover image")
	cover_alt = take(get_str, data, "coverAlt", max_length=200, label="Cover image description")
	og_image = take(_seo_str, seo, "ogImage", 500, "Share image")
	if errors:
		raise validation_error(errors)

	return {
		"site": site,
		"slug": slug,
		"title": title,
		"excerpt": excerpt,
		"body": post_body,
		"author_name": author,
		"tags": ", ".join(tags),
		"status": status,
		"published_at": published_at,
		"seo_title": seo_title,
		"seo_description": seo_description,
		"noindex": 1 if seo.get("noindex") is True else 0,
		"canonical_url": canonical,
		"cover_image": cover_image,
		"cover_alt": cover_alt,
		"og_image": og_image,
	}


def _seo_str(seo: dict, key: str, max_length: int, label: str):
	"""get_str for a key inside `seo`, with errors reported as `seo.<key>`."""
	try:
		return get_str(seo, key, max_length=max_length, label=label)
	except ApiError as e:
		raise validation_error({f"seo.{key}": (e.fields or {}).get(key, e.message)})


def _site_datetime(value):
	"""ISO 8601 (with Z or an offset; without one it's site time) -> naive site-timezone datetime."""
	if not isinstance(value, str):
		return None
	try:
		dt = get_datetime(value.replace("Z", "+00:00"))
	except Exception:
		return None
	if dt is None:
		return None
	if dt.tzinfo is not None:
		dt = dt.astimezone(ZoneInfo(get_system_timezone())).replace(tzinfo=None)
	return dt


def _utc_second(value: str) -> str | None:
	dt = _site_datetime(value)
	return iso(dt) if dt else None


def _message(e: Exception) -> str:
	return str(e).strip() or "Invalid value."


# ---------------------------------------------------------------- output

_SUMMARY_FIELDS = (
	"site",
	"slug",
	"title",
	"status",
	"published_at",
	"tags",
	"author_name",
	"modified",
	"modified_by",
)


def _get(name: str):
	if not name or not frappe.db.exists(POST, name):
		raise not_found()
	return frappe.get_doc(POST, name)


def _summary(r) -> dict:
	return {
		"id": r.name,
		"site": r.site,
		"slug": r.slug,
		"title": r.title,
		"status": r.status,
		"isLive": wc.is_live(r.status, r.published_at),
		"publishedAt": iso(r.published_at),
		"tags": [t.strip() for t in (r.tags or "").split(",") if t.strip()],
		"authorName": r.author_name or None,
		"updatedAt": iso(r.modified),
		"updatedBy": r.modified_by if "@" in (r.modified_by or "") else None,
	}


def _full(doc) -> dict:
	return {
		**_summary(doc),
		"excerpt": doc.excerpt or "",
		"body": doc.body,
		"coverImage": doc.cover_image or None,
		"coverAlt": doc.cover_alt or None,
		"seo": {
			"title": doc.seo_title or None,
			"description": doc.seo_description or None,
			"ogImage": doc.og_image or None,
			"noindex": bool(doc.noindex),
		},
		"canonicalUrl": doc.canonical_url or None,
		"url": f"{wc.SITE_BASE_URLS[doc.site]}/blog/{doc.slug}",
		"createdAt": iso(doc.creation),
	}
