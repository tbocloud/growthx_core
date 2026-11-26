"""What the Press-side job (tbocloud-site-billing-sync) reads and reports for ERPNext Direct sites.

    GET  /api/method/growthx_core.billing.site_sync.get_site_states
    POST /api/method/growthx_core.billing.site_sync.report_site_states    states=[{site, platform_status, action, message}]
    POST /api/method/growthx_core.billing.site_sync.report_platform_sites sites=[{site, group, created_on, domains}]

The same contract as frappe_payu's Site Billing. Core never calls Press for these sites: it lists the
state each imported tenant should be in, and records what the job says it did. A tenant's own status is
never changed by a report.
"""

from urllib.parse import urlsplit

import frappe
from frappe import _
from frappe.utils import cint, now_datetime

SYNC_ROLE = "GrowthX Site Sync"
ACTIONS = ("", "suspended", "reactivated", "held", "error")
SETTINGS = "GX Billing Settings"


def _require_sync_role():
	roles = frappe.get_roles()
	if SYNC_ROLE not in roles and "System Manager" not in roles:
		frappe.throw(_("Not permitted"), frappe.PermissionError)


@frappe.whitelist(methods=["GET"])
def get_site_states() -> dict:
	_require_sync_role()
	settings = frappe.get_single(SETTINGS)
	tenant, sub = frappe.qb.DocType("GX Tenant"), frappe.qb.DocType("GX Subscription")
	rows = (
		frappe.qb.from_(tenant)
		.left_join(sub)
		.on(sub.name == tenant.subscription)
		.select(
			tenant.platform_site,
			tenant.status,
			sub.name.as_("subscription"),
			sub.status.as_("billing_status"),
			sub.suspended_by,
		)
		.where(tenant.origin == "imported")
		.where(tenant.status != "archived")
		.where(tenant.platform_site.isnotnull())
		.where(tenant.platform_site != "")
		.orderby(tenant.platform_site)
		.run(as_dict=True)
	)
	sites = []
	for row in rows:
		suspended = row.status == "suspended"
		sites.append(
			{
				"site": row.platform_site,
				"desired_state": "Suspended" if suspended else "Active",
				"billing_status": row.billing_status or "",
				"overdue_invoice": row.subscription or "",
				"suspended_by_billing": 1 if suspended and row.suspended_by == "billing" else 0,
			}
		)
	return {
		"enabled": bool(cint(settings.enabled) and cint(settings.suspend_sites)),
		"max_suspensions_per_run": cint(settings.sync_max_suspensions_per_run) or 1,
		"sites": sites,
	}


@frappe.whitelist(methods=["POST"])
def report_site_states(states) -> dict:
	_require_sync_role()
	states = frappe.parse_json(states) or []
	by_site = {
		(site or "").lower(): name
		for name, site in frappe.get_all(
			"GX Tenant",
			filters={"origin": "imported", "platform_site": ("is", "set")},
			fields=["name", "platform_site"],
			as_list=True,
		)
	}
	now = now_datetime()
	updated = 0
	for state in states if isinstance(states, list) else []:
		if not isinstance(state, dict):
			continue
		site = (state.get("site") or "").strip().lower()
		action = state.get("action") or ""
		if action not in ACTIONS or site not in by_site:
			continue
		frappe.db.set_value(
			"GX Tenant",
			by_site[site],
			{
				"sync_status": (action or state.get("platform_status") or "")[:140],
				"sync_message": (state.get("message") or "")[:500],
				"sync_checked_at": now,
			},
			update_modified=False,
		)
		updated += 1
	return {"updated": updated}


@frappe.whitelist(methods=["POST"])
def report_platform_sites(sites) -> dict:
	"""Every non-archived site the platform hosts, so tenants can be matched to it."""
	_require_sync_role()
	sites = [row for row in (frappe.parse_json(sites) or []) if isinstance(row, dict) and row.get("site")]
	frappe.db.set_single_value(SETTINGS, "platform_sites", frappe.as_json(sites))
	frappe.db.set_single_value(SETTINGS, "platform_sites_at", now_datetime())
	return {"stored": len(sites), "matched": match_all()}


def _platform_list() -> list | None:
	"""The stored list, or None when the job has never reported one."""
	if not frappe.db.get_single_value(SETTINGS, "platform_sites_at"):
		return None
	raw = frappe.db.get_single_value(SETTINGS, "platform_sites")
	try:
		rows = frappe.parse_json(raw) if raw else []
	except ValueError:
		return None
	return rows if isinstance(rows, list) else None


def _index(rows: list) -> tuple[dict, dict]:
	"""Lower-cased site name -> site name, and custom domain -> site name (names win over domains)."""
	names, domains = {}, {}
	for row in rows:
		if not isinstance(row, dict) or not row.get("site"):
			continue
		site = str(row["site"]).strip()
		names[site.lower()] = site
		for domain in row.get("domains") or []:
			if isinstance(domain, str) and domain.strip():
				domains.setdefault(domain.strip().lower(), site)
	return names, domains


def _lookup(index: tuple[dict, dict], host: str) -> str | None:
	host = (host or "").strip().lower().rstrip(".")
	names, domains = index
	return names.get(host) or domains.get(host)


def match_platform_site(host: str) -> str | None:
	"""The Press site this host is: its exact name first, then any custom domain. None if the list is
	unknown or has no such site; never a partial match."""
	rows = _platform_list()
	if rows is None:
		return None
	return _lookup(_index(rows), host)


def platform_list_known() -> bool:
	return _platform_list() is not None


def host_of(site_url: str | None) -> str:
	text = (site_url or "").strip().lower()
	if text and "://" not in text:
		text = "//" + text
	try:
		return (urlsplit(text).hostname or "").rstrip(".")
	except ValueError:
		return ""


def match_all() -> int:
	"""Set `platform_site` on every imported tenant from its site address; returns how many matched."""
	rows = _platform_list()
	if rows is None:
		return 0
	index = _index(rows)
	matched = 0
	for name, site_url, current in frappe.get_all(
		"GX Tenant",
		filters={"origin": "imported"},
		fields=["name", "site_url", "platform_site"],
		as_list=True,
	):
		site = _lookup(index, host_of(site_url))
		matched += bool(site)
		if (site or None) != (current or None):
			frappe.db.set_value("GX Tenant", name, "platform_site", site, update_modified=False)
	return matched
