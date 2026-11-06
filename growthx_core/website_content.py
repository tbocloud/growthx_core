"""Website content (blog) rules shared by GX Website Post and the content.* endpoints.

Phase 1: posts only, edited in Core desk, text-only (no images until S3 arrives in phase 2).
"""

import re

from frappe.utils import get_datetime, now_datetime

# The site slug is the website repo's name without `-web`.
SITES = ("growthx", "printagx", "willpex", "billax", "phoebix", "stayvex")
STATUSES = ("draft", "scheduled", "published")
# Each site's address. A post lives at <base>/blog/<slug>.
SITE_BASE_URLS = {
	"growthx": "https://tbogrowthx.com",
	"printagx": "https://printagx.in",
	"willpex": "https://willpex.in",
	"billax": "https://billax.in",
	"phoebix": "https://phoebix.in",
	"stayvex": "https://stayvex.in",
}

# Where a canonical URL (and, in phase 2, a redirect) may point: https only, these hosts and their www.
ALLOWED_DOMAINS = ("tbogrowthx.com", "printagx.in", "willpex.in", "billax.in", "phoebix.in", "stayvex.in")

SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
SLUG_MAX = 100
TAG_MAX = 30
MAX_TAGS = 10
BODY_MAX = 100_000
EXCERPT_MAX = 300

# Every link destination in the Markdown: inline `[text](dest)` / `![alt](dest)`, reference
# definitions `[id]: dest`, and autolinks `<scheme:...>`. Allow-list, so anything unexpected
# (javascript:, data:, entities, backslashes, protocol-relative //host) is refused.
_INLINE_DEST = re.compile(r"\]\(\s*<?([^\s)>]*)")
_REF_DEF = re.compile(r"^ {0,3}\[[^\]]+\]:\s*<?([^\s>]*)", re.M)
_AUTOLINK = re.compile(r"<([A-Za-z][A-Za-z0-9+.-]{1,31}:[^\s<>]*)>")
_IMAGE = re.compile(r"!\[[^\]]*\]\s*[(\[]")
_INLINE_IMAGE_DEST = re.compile(r"!\[[^\]]*\]\(\s*<?([^\s)>]*)")
DEFAULT_MEDIA_HOST = "media.tbogrowthx.com"


def media_host() -> str:
	import frappe

	return frappe.conf.get("growthx_media_host") or DEFAULT_MEDIA_HOST


def media_url_ok(url: str) -> bool:
	"""Only images uploaded through admin.upload_content_image: our media host and key format."""
	pattern = rf"^https://{re.escape(media_host())}/content/[a-z0-9]+/\d{{4}}/[a-f0-9]{{32}}\.(jpg|png|webp)$"
	return bool(re.match(pattern, (url or "").strip()))


MEDIA_ONLY = "Upload the image in /content first; only images on the media host can be used."


def slugify(text: str) -> str:
	slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
	if len(slug) > SLUG_MAX:
		slug = slug[:SLUG_MAX].rsplit("-", 1)[0] if "-" in slug[:SLUG_MAX] else slug[:SLUG_MAX]
	return slug


def parse_tags(value) -> list[str]:
	"""'GST, Invoicing ,gst' -> ['gst', 'invoicing']. Raises ValueError with a message."""
	tags = []
	for raw in re.split(r"[,\n]", value or ""):
		tag = raw.strip().lower().replace(" ", "-")
		if not tag or tag in tags:
			continue
		if len(tag) > TAG_MAX or not SLUG_RE.match(tag):
			raise ValueError(
				f"Tag '{raw.strip()}' must be lowercase letters, numbers and hyphens, "
				f"at most {TAG_MAX} characters."
			)
		tags.append(tag)
	if len(tags) > MAX_TAGS:
		raise ValueError(f"At most {MAX_TAGS} tags.")
	return tags


def link_allowed(target: str) -> bool:
	t = target.strip()
	if t.startswith("//"):
		return False
	return t.startswith(("/", "#")) or t.lower().startswith(("http://", "https://", "mailto:"))


def body_problem(body: str) -> str | None:
	"""Why the Markdown body can't be saved, or None."""
	if len(body or "") > BODY_MAX:
		return f"The body must be at most {BODY_MAX:,} characters."
	images = _IMAGE.findall(body or "")
	inline = _INLINE_IMAGE_DEST.findall(body or "")
	if len(images) > len(inline):
		return "Write images as ![description](image address), not with a reference."
	for target in inline:
		if not media_url_ok(target):
			return MEDIA_ONLY
	targets = _INLINE_DEST.findall(body or "") + _REF_DEF.findall(body or "") + _AUTOLINK.findall(body or "")
	for target in targets:
		if not link_allowed(target):
			shown = target if len(target) <= 60 else target[:57] + "..."
			return f"Link '{shown}' isn't allowed. Use http(s)://, mailto: or a path starting with / or #."
	return None


def canonical_allowed(url: str) -> bool:
	m = re.match(r"^https://([a-z0-9.-]+)(?::443)?(/[^\s<>\"']*)?$", (url or "").strip(), re.I)
	if not m:
		return False
	host = m.group(1).lower()
	return any(host in (d, "www." + d) for d in ALLOWED_DOMAINS)


def plain_excerpt(body: str, limit: int = EXCERPT_MAX) -> str:
	"""The start of a Markdown body as plain text, cut at a word boundary."""
	text = re.sub(r"```.*?```", " ", body or "", flags=re.S)
	text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)  # links -> their text
	text = re.sub(r"^ {0,3}\[[^\]]+\]:.*$", " ", text, flags=re.M)  # reference definitions
	text = re.sub(r"<[^>]+>", " ", text)
	text = re.sub(r"^\s*(#{1,6}|>|[-*+]|\d+[.)])\s+", "", text, flags=re.M)
	text = re.sub(r"[*_`~]+", "", text)
	text = " ".join(text.split())
	if len(text) <= limit:
		return text
	cut = text[: limit - 1].rsplit(" ", 1)[0]
	return cut.rstrip(" ,.;:") + "…"


def is_live(status: str, published_at) -> bool:
	return (
		status in ("published", "scheduled")
		and bool(published_at)
		and get_datetime(published_at) <= now_datetime()
	)


# ---------------------------------------------------------------- site SEO settings and page SEO

# Seeded once into GX Content Site; editors can rename (siteName) from /content.
SITE_NAMES = {
	"growthx": "TBO GrowthX",
	"printagx": "PrintagX",
	"willpex": "WillpeX",
	"billax": "BillaX",
	"phoebix": "Phoebix",
	"stayvex": "Stayvex",
}
GOOGLE_VERIFICATION_RE = re.compile(r"^[A-Za-z0-9_-]{10,100}$")
BING_VERIFICATION_RE = re.compile(r"^[A-Fa-f0-9]{32}$")
GA4_RE = re.compile(r"^G-[A-Z0-9]{4,20}$")
PAGE_PATH_MAX = 200


def ensure_sites():
	"""One GX Content Site per site (after_migrate). Existing rows are left as editors set them,
	except the base URL, which Core owns."""
	import frappe

	for site in SITES:
		if frappe.db.exists("GX Content Site", site):
			if frappe.db.get_value("GX Content Site", site, "base_url") != SITE_BASE_URLS[site]:
				frappe.db.set_value("GX Content Site", site, "base_url", SITE_BASE_URLS[site])
			continue
		frappe.get_doc(
			{
				"doctype": "GX Content Site",
				"site": site,
				"site_name": SITE_NAMES[site],
				"base_url": SITE_BASE_URLS[site],
				"title_template": f"%s · {SITE_NAMES[site]}",
				"blog_enabled": 1,
			}
		).insert(ignore_permissions=True)


def analytics_active() -> bool:
	"""GA4 reaches the sites only once management approves tracking (site_config, not the editor)."""
	import frappe

	return bool(frappe.utils.cint(frappe.conf.get("growthx_content_analytics")))


def blog_enabled(site: str) -> bool:
	import frappe

	value = frappe.db.get_value("GX Content Site", site, "blog_enabled")
	return value is None or bool(value)  # no settings row yet: on


def page_path_problem(path: str) -> str | None:
	if not path.startswith("/") or path.startswith("//"):
		return "Must start with a single /."
	if len(path) > PAGE_PATH_MAX:
		return f"At most {PAGE_PATH_MAX} characters."
	if any(c in path for c in "?#") or any(c.isspace() for c in path) or any(c in path for c in "<>\"'\\"):
		return "Just the path: no query (?), fragment (#), spaces or quotes."
	if path.startswith("/blog/"):
		return "Posts under /blog/ carry their own SEO; edit the post instead."
	return None


def normalise_path(path: str) -> str:
	path = (path or "").strip()
	return path.rstrip("/") or "/" if path.startswith("/") else path


def site_version(site: str) -> str:
	"""Opaque; changes on any change for the site, including a scheduled post going live."""
	import hashlib

	import frappe

	parts = [
		frappe.db.get_value("GX Content Site", site, "modified"),
		frappe.db.sql("select count(*), max(modified) from `tabGX Content Page` where site=%s", site)[0],
		frappe.db.sql("select count(*), max(modified) from `tabGX Website Post` where site=%s", site)[0],
		frappe.db.sql(
			"""select count(*), max(published_at) from `tabGX Website Post`
			where site=%s and status in ('published', 'scheduled') and published_at <= %s""",
			(site, now_datetime()),
		)[0],
	]
	return hashlib.sha256(repr(parts).encode()).hexdigest()[:16]


def detach_audit_on_desk_delete(doc, details: str):
	"""Desk delete of a content record: its audit rows would block it (they link to it). They keep
	their details and type (still listed under entityType=content) but drop the record's name, and the
	delete is logged. The /content endpoints delete with `force` and log it themselves (flag
	`audit_logged`), keeping the links. Audit rows still protect every other kind of record."""
	import frappe

	if doc.flags.audit_logged:
		return
	frappe.db.set_value(
		"GX Audit Log",
		{"reference_doctype": doc.doctype, "reference_name": doc.name},
		"reference_name",
		None,
		update_modified=False,
	)
	from growthx_core.api.v1._core import audit

	audit(f"content.{doc.doctype.split()[-1].lower()}_deleted", doc.doctype, details=f"{doc.name}: {details}")
