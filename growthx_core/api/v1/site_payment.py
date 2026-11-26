"""Where the Pay now button on Press's suspended-site page lands.

    GET /api/method/growthx_core.api.v1.site_payment.go?site=<host the visitor used>

Public and read-only. A site GrowthX bills (and hasn't archived) goes to sign-in, then the Billing page;
anything else gets the same neutral page, so the endpoint never reveals which hosts exist or who owns them.
"""

import frappe
from werkzeug.wrappers import Response

from growthx_core.api.v1._core import RateLimit, endpoint, query, web_url
from growthx_core.billing.site_sync import host_of

BILLING_PATH = "/login?next=/customer/billing"
NEUTRAL_PAGE = (
	'<!doctype html><html lang="en"><head><meta charset="utf-8">'
	'<meta name="viewport" content="width=device-width, initial-scale=1">'
	'<meta name="robots" content="noindex"><title>Site paused</title></head>'
	'<body style="font-family:system-ui,sans-serif;text-align:center;margin:20vh 1rem 0">'
	"<p>This site is paused. Please contact the provider who manages it.</p></body></html>"
)


def _is_billed(host: str) -> bool:
	if not host:
		return False
	tenant = frappe.qb.DocType("GX Tenant")
	rows = (
		frappe.qb.from_(tenant)
		.select(
			tenant.name,
			tenant.origin,
			tenant.site_url,
			tenant.custom_domain,
			tenant.custom_domain_status,
			tenant.platform_site,
		)
		.where(tenant.status != "archived")
		.where(
			(tenant.site_url.like(f"%{_escape(host)}%"))
			| (tenant.custom_domain == host)
			| (tenant.platform_site == host)
		)
		.limit(50)
		.run(as_dict=True)
	)
	for row in rows:
		if host_of(row.site_url) == host:
			return True
		if row.origin == "provisioned":
			if row.custom_domain_status == "active" and (row.custom_domain or "").strip().lower() == host:
				return True
		elif (row.platform_site or "").strip().lower() == host:
			return True
	return False


def _escape(text: str) -> str:
	return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _neutral() -> Response:
	response = Response(NEUTRAL_PAGE, status=200, mimetype="text/html")
	response.headers["Cache-Control"] = "no-store"
	return response


@endpoint(methods=("GET",), auth=False, rate_limit=RateLimit(30, 60))
def go():
	site = query().get("site")
	host = host_of(site) if isinstance(site, str) else ""
	if not _is_billed(host):
		return _neutral()
	response = Response("", status=302)
	response.headers["Location"] = web_url(BILLING_PATH)
	response.headers["Cache-Control"] = "no-store"
	return response
