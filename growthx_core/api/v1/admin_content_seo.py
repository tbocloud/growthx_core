"""Website content for the /content portal: site SEO settings and page SEO (admin.content_sites,
admin.update_content_site, admin.content_pages, admin.save_content_page, admin.delete_content_page).
Re-exported from admin.py, so the paths are `growthx_core.api.v1.admin.*`.

For the `admin` and `content_editor` roles. The checks are the doctypes' own (the same as in desk);
this module maps them to the contract's fields. Images must come from admin.upload_content_image.
"""

import frappe

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
	query,
	validation_error,
)

CONTENT_ROLES = ("admin", "content_editor")
SITE = "GX Content Site"
PAGE = "GX Content Page"

# Contract field -> doctype field, for sites (partial updates).
SITE_INPUT = {
	"siteName": "site_name",
	"titleTemplate": "title_template",
	"defaultTitle": "default_title",
	"defaultDescription": "default_description",
	"googleVerification": "google_verification",
	"bingVerification": "bing_verification",
	"analyticsId": "analytics_id",
	"defaultOgImage": "default_og_image",
}
SITE_FIELDS = {v: k for k, v in SITE_INPUT.items()} | {"site": "site"}
PAGE_FIELDS = {
	"site": "site",
	"path": "path",
	"seo_title": "title",
	"seo_description": "description",
	"og_image": "ogImage",
}


# ---------------------------------------------------------------- sites


@endpoint(roles=CONTENT_ROLES)
def content_sites():
	wc.ensure_sites()
	counts = {}
	for site, status, n in frappe.db.sql(
		"select site, status, count(*) from `tabGX Website Post` group by site, status"
	):
		counts.setdefault(site, {})[status] = n
	return {"items": [_admin_site(frappe.get_doc(SITE, s), counts.get(s, {})) for s in wc.SITES]}


@endpoint(methods=("POST",), roles=CONTENT_ROLES)
def update_content_site():
	data = body()
	site = get_choice(data, "site", wc.SITES, required=True)
	wc.ensure_sites()
	doc = frappe.get_doc(SITE, site)

	errors = {}
	for key, field in SITE_INPUT.items():
		if key not in data:
			continue  # send only what changes
		value = data[key]
		if value is not None and not isinstance(value, str):
			errors[key] = "Must be a string or null."
			continue
		doc.set(field, value)
	if "blogEnabled" in data:
		if not isinstance(data["blogEnabled"], bool):
			errors["blogEnabled"] = "Must be true or false."
		else:
			doc.blog_enabled = 1 if data["blogEnabled"] else 0
	if errors:
		raise validation_error(errors)

	_save(doc, SITE_FIELDS)
	audit("content.site_updated", SITE, doc.name, details=f"SEO settings for {doc.site}")
	return _admin_site(doc, _post_counts(site))


# ---------------------------------------------------------------- pages


@endpoint(roles=CONTENT_ROLES)
def content_pages():
	site = get_choice(query(), "site", wc.SITES, required=True)
	rows = frappe.get_all(PAGE, {"site": site}, _PAGE_COLUMNS, order_by="path asc")
	return {"items": [_admin_page(r) for r in rows]}


@endpoint(methods=("POST",), roles=CONTENT_ROLES)
def save_content_page():
	data = body()
	errors = {}

	def take(fn, *args, **kwargs):
		try:
			return fn(*args, **kwargs)
		except ApiError as e:
			errors.update(e.fields or {})

	site = take(get_choice, data, "site", wc.SITES, required=True)
	path = take(get_str, data, "path", required=True, max_length=wc.PAGE_PATH_MAX, label="Path")
	title = take(get_str, data, "title", max_length=70, label="Title")
	description = take(get_str, data, "description", max_length=200, label="Description")
	og_image = take(get_str, data, "ogImage", max_length=500, label="Share image")
	if "noindex" in data and not isinstance(data["noindex"], bool):
		errors["noindex"] = "Must be true or false."
	if errors:
		raise validation_error(errors)

	path = wc.normalise_path(path)
	name = frappe.db.get_value(PAGE, {"site": site, "path": path})  # keyed by site + path
	doc = frappe.get_doc(PAGE, name) if name else frappe.new_doc(PAGE)
	doc.update(
		{
			"site": site,
			"path": path,
			"seo_title": title,
			"seo_description": description,
			"noindex": 1 if data.get("noindex") is True else 0,
			"og_image": og_image,
		}
	)
	_save(doc, PAGE_FIELDS)
	audit("content.page_saved", PAGE, doc.name, details=f"{site} {path}")
	return _admin_page(doc)


@endpoint(methods=("POST",), roles=CONTENT_ROLES)
def delete_content_page():
	name = get_str(body(), "id", required=True)
	if not frappe.db.exists(PAGE, name):
		raise not_found()
	doc = frappe.get_doc(PAGE, name)
	# Logged first (the row must still exist for the link); `force` skips only the link check.
	audit("content.page_deleted", PAGE, doc.name, details=f"{doc.site} {doc.path}")
	frappe.delete_doc(PAGE, doc.name, force=True, flags={"audit_logged": True})
	return {"ok": True}


# ---------------------------------------------------------------- helpers

_PAGE_COLUMNS = [
	"name",
	"site",
	"path",
	"seo_title",
	"seo_description",
	"og_image",
	"noindex",
	"modified",
	"modified_by",
]


def _save(doc, field_map: dict):
	try:
		doc.save()
	except frappe.ValidationError as e:
		field = field_map.get(doc.flags.problem_field)
		if not field:
			raise
		raise validation_error({field: str(e).strip() or "Invalid value."})


def _post_counts(site: str) -> dict:
	rows = frappe.db.sql(
		"select status, count(*) from `tabGX Website Post` where site=%s group by status", site
	)
	return dict(rows)


def _admin_site(doc, counts: dict) -> dict:
	return {
		"site": doc.site,
		"siteName": doc.site_name,
		"baseUrl": doc.base_url,
		"titleTemplate": doc.title_template,
		"defaultTitle": doc.default_title or None,
		"defaultDescription": doc.default_description or None,
		"defaultOgImage": doc.default_og_image or None,
		"googleVerification": doc.google_verification or None,
		"bingVerification": doc.bing_verification or None,
		"analyticsId": doc.analytics_id or None,
		"analyticsActive": wc.analytics_active(),
		"blogEnabled": bool(doc.blog_enabled),
		"postCounts": {s: int(counts.get(s, 0)) for s in wc.STATUSES},
		"updatedAt": iso(doc.modified),
	}


def _admin_page(r) -> dict:
	return {
		"id": r.name,
		"site": r.site,
		"path": r.path,
		"title": r.seo_title or None,
		"description": r.seo_description or None,
		"ogImage": r.og_image or None,
		"noindex": bool(r.noindex),
		"updatedAt": iso(r.modified),
		"updatedBy": r.modified_by if "@" in (r.modified_by or "") else None,
	}
