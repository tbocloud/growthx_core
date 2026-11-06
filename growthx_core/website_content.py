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
	if _IMAGE.search(body or ""):
		return "Images in posts aren't supported yet. Remove them for now."
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
