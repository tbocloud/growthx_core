"""Tenant sites.

`check_subdomain` is public: the sign-up form calls it as the customer types.
`redeem_login` is server to server, called by tenant sites (not by Web or Flutter): the caller
authenticates with its own key in the `X-GX-Tenant-Key` header (see provisioning/site_login.py).
"""

import frappe

from growthx_core.api.v1._core import ApiError, RateLimit, audit, body, endpoint, get_str, query
from growthx_core.provisioning import runner, site_login
from growthx_core.provisioning.press import tenant_domain


@endpoint(methods=("GET",), auth=False, rate_limit=RateLimit(120, 60))
def check_subdomain():
	"""Is this subdomain free? Checked within Core only (auth.signup also asks Press)."""
	subdomain = get_str(query(), "subdomain", required=True, max_length=64, label="Subdomain").lower()
	reason = runner.subdomain_problem(subdomain)
	return {
		"subdomain": subdomain,
		"address": f"{runner.site_subdomain(subdomain)}.{tenant_domain()}",
		"available": reason is None,
		"reason": reason,
		"suggestion": runner.suggest_subdomain(subdomain) if reason in ("taken", "reserved") else None,
	}


def _invalid_link():
	return ApiError(404, "invalid_login_token", "This link has expired. Open your app again from GrowthX.")


@endpoint(
	methods=("POST",),
	auth=False,
	rate_limit=(
		RateLimit(30, 60, by_header=site_login.TENANT_KEY_HEADER),  # per tenant
		RateLimit(300, 60),  # per IP: many tenants can share one server
	),
)
def redeem_login():
	"""Exchange a one-time "Open my app" token for the user the tenant should log in."""
	tenant = site_login.tenant_for_key(frappe.get_request_header(site_login.TENANT_KEY_HEADER))
	if not tenant:
		raise ApiError(401, "invalid_tenant_key", "Unknown tenant key.")

	token = get_str(body(), "token", required=True, max_length=200, label="Token")
	# Redeeming always consumes the token, even if it turns out to be for another tenant,
	# so a leaked link can't be retried elsewhere.
	grant = site_login.redeem(token)
	if not grant:
		raise _invalid_link()

	if grant.get("tenant") != tenant:
		# The key is valid but for a different tenant: record it (never the token) and refuse.
		audit(
			"tenant_login_rejected",
			"GX Tenant",
			tenant,
			details=f"token was issued for {grant.get('tenant')}",
		)
		frappe.db.commit()
		raise _invalid_link()

	user = frappe.db.get_value(
		"User", grant.get("user"), ["name", "email", "full_name", "enabled"], as_dict=True
	)
	owner = frappe.db.get_value("GX Account", frappe.db.get_value("GX Tenant", tenant, "account"), "user")
	status = frappe.db.get_value("GX Tenant", tenant, "status")
	if not user or not user.enabled or user.name != owner or status != "ready":
		raise _invalid_link()

	audit("tenant_login_redeemed", "GX Tenant", tenant, actor=user.name)
	return {"email": user.email, "fullName": user.full_name or user.email}
