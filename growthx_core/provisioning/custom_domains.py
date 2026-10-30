"""A customer's own domain for their site (e.g. app.noorsolar.com), through Press.

    add()      -> pending_dns: the customer adds one CNAME, domain -> <subdomain>.tbogrowthx.com
    check()    -> Press's site.check_dns; when it's right, site.add_domain   -> verifying
               -> site.domains says the certificate is ready, set_host_name -> active
    remove()   -> back to the tbogrowthx.com address only

check() runs on "Check now" and every 10 minutes (check_pending). Once active, "Open my app"
uses the custom domain (public_url). Subdomains only for now, one per site, paid plans only.
Fields live on GX Tenant (custom_domain*).
"""

import re
from urllib.parse import urlsplit

import frappe
from frappe.utils import add_days, now_datetime

from growthx_core.api.v1._core import ApiError, iso, validation_error
from growthx_core.provisioning import press

# A domain left at pending_dns is checked on schedule for this long; "Check now" works after.
SCHEDULED_CHECK_DAYS = 3

LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
# Two-label suffixes a root domain can sit under (noorsolar.co.in is a root domain, not a subdomain).
# Not the whole public suffix list: Press's DNS check catches anything this misses.
TWO_LABEL_SUFFIXES = {
	*(f"{x}.in" for x in ("co", "net", "org", "firm", "gen", "ind", "ac", "edu", "gov", "res")),
	*(f"{x}.uk" for x in ("co", "org", "me", "ltd", "plc")),
	*(f"{x}.ae" for x in ("co", "net", "org")),
	*(f"{x}.au" for x in ("com", "net", "org")),
	*(f"{x}.sa" for x in ("com", "net", "org")),
	"com.sg",
	"com.my",
	"co.za",
	"co.nz",
	"com.bd",
	"com.pk",
	"com.np",
	"com.qa",
	"com.om",
	"com.kw",
	"com.bh",
}
SYSTEM_DOMAINS = ("tbogrowthx.com", "tbocloud.in", "frappe.cloud", "frappe.dev")

MESSAGES = {
	"not_found": "We can't find the CNAME record yet. Changes can take up to an hour to show; check again later.",
	"wrong_target": "The record points somewhere else. Change it to point to {target}.",
	"multiple_records": "The domain has more than one CNAME or A record. Keep only the CNAME to {target}.",
	"aaaa_record": "The domain has an AAAA (IPv6) record. Delete it, keeping only the CNAME to {target}.",
	"caa_blocks": "A CAA record on your domain doesn't allow Let's Encrypt. Add letsencrypt.org to it, or remove it.",
	"cloudflare_proxied": 'The record is behind Cloudflare\'s proxy. Set it to "DNS only" (grey cloud).',
	"domain_taken": "This domain is already used by another site on our hosting. Contact support if it's yours.",
	"broken": "We couldn't get an HTTPS certificate for this domain. Remove it and add it again, or contact support.",
}


# ---------------------------------------------------------------- rules


def normalize(domain: str) -> str:
	"""Lower-case `domain` and check it's a subdomain the customer can use. Raises 422."""
	domain = (domain or "").strip().lower().rstrip(".")

	def invalid(message):
		return validation_error({"domain": message})

	if "://" in domain or "/" in domain:
		raise invalid("Enter just the domain, like app.yourcompany.com.")
	labels = domain.split(".")
	if not domain.isascii() or len(domain) > 253 or len(labels) < 2 or not all(map(LABEL.match, labels)):
		raise invalid(
			"That isn't a valid domain. Use letters, numbers and hyphens, like app.yourcompany.com."
		)
	if labels[-1].isdigit():
		raise invalid("Enter a domain name, not an IP address.")
	for system in (*SYSTEM_DOMAINS, press.tenant_domain()):
		if domain == system or domain.endswith(f".{system}"):
			raise invalid("Use a domain your company owns.")
	if domain == root_of(domain):
		raise invalid(f"Use a subdomain, like app.{domain}. Root domains aren't supported yet.")
	return domain


def root_of(domain: str) -> str:
	labels = domain.split(".")
	size = 3 if ".".join(labels[-2:]) in TWO_LABEL_SUFFIXES else 2
	return ".".join(labels[-size:])


def check_plan(tenant):
	"""Custom domains need a paid plan (management to confirm: today, any non-trial subscription)."""
	status = (
		frappe.db.get_value("GX Subscription", tenant.subscription, "status") if tenant.subscription else None
	)
	if status != "active":
		raise ApiError(403, "plan_required", "Custom domains are available once your site is on a paid plan.")


def public_url(tenant) -> str:
	"""The address the customer uses: the custom domain once it's active, else the site's own."""
	if tenant.custom_domain and tenant.custom_domain_status == "active":
		return f"https://{tenant.custom_domain}"
	return tenant.site_url


# ---------------------------------------------------------------- actions


def add(tenant, domain: str):
	if tenant.status != "ready" or not tenant.press_site_id:
		raise ApiError(409, "site_not_ready", "Your site isn't ready yet.")
	check_plan(tenant)
	domain = normalize(domain)
	if tenant.custom_domain:
		raise ApiError(409, "domain_exists", "This site already has a custom domain. Remove it first.")
	if frappe.db.exists("GX Tenant", {"custom_domain": domain, "name": ("!=", tenant.name)}):
		raise ApiError(409, "domain_taken", "Another site already uses this domain.")
	tenant.update(
		{
			"custom_domain": domain,
			"custom_domain_status": "pending_dns",
			"custom_domain_problem": None,
			"custom_domain_message": None,
			"custom_domain_added_at": now_datetime(),
			"custom_domain_checked_at": None,
			"custom_domain_active_at": None,
		}
	)
	tenant.save(ignore_permissions=True)


def check(tenant, client=None):
	"""Move the domain on as far as Press allows now. Press being down leaves it as it was."""
	if not tenant.custom_domain or tenant.status != "ready":
		return
	client = client or press.get_client()
	site_id, domain = tenant.press_site_id, tenant.custom_domain
	tenant.custom_domain_checked_at = now_datetime()
	just_added = False
	try:
		if tenant.custom_domain_status == "pending_dns":
			client.check_dns(site_id, domain)
			client.add_domain(site_id, domain)
			_set(tenant, "verifying")
			just_added = True
		if tenant.custom_domain_status == "verifying":
			state = client.domain_status(site_id, domain)
			if state == "active":
				client.set_host_name(site_id, domain)
				_set(tenant, "active")
				tenant.custom_domain_active_at = now_datetime()
			elif state == "broken" or (state is None and not just_added):
				_set(tenant, "failed", MESSAGES["broken"])
	except press.DnsProblem as e:
		if e.problem == "domain_taken":
			_set(tenant, "failed", MESSAGES["domain_taken"])
		else:
			_set(tenant, "pending_dns", MESSAGES[e.problem].format(target=target_host(tenant)), e.problem)
	except press.PressError:
		frappe.log_error(title=f"GrowthX: custom domain check failed for {tenant.name}")
		tenant.custom_domain_message = (
			"We couldn't check the domain just now. Please try again in a few minutes."
		)
	tenant.save(ignore_permissions=True)


def remove(tenant, client=None):
	if not tenant.custom_domain:
		return False
	client = client or press.get_client()
	site_id, domain = tenant.press_site_id, tenant.custom_domain
	try:
		if tenant.custom_domain_status == "active":
			client.set_host_name(site_id, site_id)  # back to <subdomain>.tbogrowthx.com first
		if tenant.custom_domain_status != "pending_dns":
			try:
				client.remove_domain(site_id, domain)
			except press.PressNotFound:
				pass
	except press.PressError:
		frappe.log_error(title=f"GrowthX: could not remove custom domain from {tenant.name}")
		raise ApiError(
			503, "hosting_unavailable", "We couldn't remove the domain just now. Please try again shortly."
		) from None
	for field in (
		"custom_domain",
		"custom_domain_status",
		"custom_domain_problem",
		"custom_domain_message",
		"custom_domain_added_at",
		"custom_domain_checked_at",
		"custom_domain_active_at",
	):
		tenant.set(field, None)
	tenant.save(ignore_permissions=True)
	return True


def _set(tenant, status, message=None, problem=None):
	tenant.custom_domain_status = status
	tenant.custom_domain_problem = problem
	tenant.custom_domain_message = message


def target_host(tenant) -> str:
	"""Where the customer's CNAME points: the site's own address, e.g. noor-solar.tbogrowthx.com."""
	return urlsplit(tenant.site_url or "").hostname or tenant.press_site_id


def check_pending():
	"""Scheduler, every 10 minutes: domains waiting on DNS (for a few days) or on a certificate."""
	since = add_days(now_datetime(), -SCHEDULED_CHECK_DAYS)
	names = frappe.get_all(
		"GX Tenant",
		filters={"status": "ready", "custom_domain_status": "verifying"},
		pluck="name",
	) + frappe.get_all(
		"GX Tenant",
		filters={
			"status": "ready",
			"custom_domain_status": "pending_dns",
			"custom_domain_added_at": (">=", since),
		},
		pluck="name",
	)
	if not names:
		return
	client = press.get_client()
	for name in names:
		try:
			check(frappe.get_doc("GX Tenant", name, for_update=True), client)
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			frappe.log_error(title=f"GrowthX: custom domain check failed for {name}")


# ---------------------------------------------------------------- API shape


def serialize(tenant) -> dict | None:
	if not tenant.custom_domain:
		return None
	domain, status = tenant.custom_domain, tenant.custom_domain_status
	records = []
	if status != "active":
		host = domain.removesuffix("." + root_of(domain))
		records = [{"type": "CNAME", "host": host, "value": target_host(tenant)}]
	return {
		"domain": domain,
		"status": status,
		"records": records,
		"dnsProblem": tenant.custom_domain_problem or None,
		"message": tenant.custom_domain_message or None,
		"checkedAt": iso(tenant.custom_domain_checked_at),
		"activeAt": iso(tenant.custom_domain_active_at),
	}
