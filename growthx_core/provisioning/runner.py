"""Tenant provisioning job.

    requested -> provisioning -> ready
                             \\-> failed  (an admin retries; resumes from the failed step)

Steps: create_site -> wait_for_site -> install_apps -> configure -> health_check.

- Idempotent: the Press site id is saved as soon as Press returns it, and `create_site` is skipped
  when it is already set, so running the job twice never creates two sites.
- Never sleeps in a worker: `wait_for_site` returns early while Press is still creating the site,
  and the per-minute scheduler (`resume_pending`) runs the job again.
- Every step records its attempt, start/finish time and error on the tenant's `steps` table.
"""

import re
from datetime import timedelta

import frappe
from frappe.utils import cint, now_datetime

from growthx_core.api.v1._serializers import STEP_LABELS
from growthx_core.provisioning import site_login
from growthx_core.provisioning.press import PressClient, PressError, get_client

STEPS = ["create_site", "wait_for_site", "install_apps", "configure", "health_check"]
WAIT_TIMEOUT = timedelta(minutes=30)
STALE_AFTER = timedelta(minutes=2)
FAILED_MESSAGE = (
	"We hit a problem while preparing your site. Our team has been alerted and will fix it shortly."
)
RESERVED_SUBDOMAINS = {"www", "api", "app", "admin", "core", "staging", "mail", "press", "status", "docs"}


class StepPending(Exception):
	"""The step is waiting on Press. Leave it running; the scheduler resumes the job."""


# ---------------------------------------------------------------- creating a tenant


def make_subdomain(display_name: str) -> str:
	base = re.sub(r"[^a-z0-9]+", "-", (display_name or "").lower()).strip("-")[:30].strip("-") or "site"
	if base in RESERVED_SUBDOMAINS:
		base = f"{base}-site"
	candidate, n = base, 1
	while frappe.db.exists("GX Tenant", {"subdomain": candidate}):
		n += 1
		candidate = f"{base}-{n}"
	return candidate


def init_steps(tenant):
	if tenant.get("steps"):
		return
	for key in STEPS:
		tenant.append("steps", {"step_key": key, "label": STEP_LABELS[key], "state": "pending"})


def enqueue(tenant_name: str):
	frappe.enqueue(
		"growthx_core.provisioning.runner.run",
		queue="long",
		job_id=f"gx-provision::{tenant_name}",
		deduplicate=True,
		enqueue_after_commit=True,
		tenant_name=tenant_name,
	)


def retry(tenant_name: str) -> bool:
	"""Admin retry. Returns False if the tenant is not in `failed` status."""
	tenant = frappe.get_doc("GX Tenant", tenant_name, for_update=True)
	if tenant.status != "failed":
		return False
	for step in tenant.steps:
		if step.state == "failed":
			step.state = "pending"
	tenant.status = "requested"
	tenant.status_message = "Preparing your site."
	tenant.save(ignore_permissions=True)
	enqueue(tenant.name)
	return True


# ---------------------------------------------------------------- the job


def run(tenant_name: str, client: PressClient | None = None):
	client = client or get_client()
	tenant = frappe.get_doc("GX Tenant", tenant_name, for_update=True)
	if tenant.status not in ("requested", "provisioning"):
		return

	init_steps(tenant)
	if tenant.status == "requested":
		tenant.attempts = cint(tenant.attempts) + 1
	tenant.status = "provisioning"
	tenant.status_message = "Preparing your site."
	tenant.save(ignore_permissions=True)
	frappe.db.commit()

	for step in tenant.steps:
		if step.state == "done":
			continue
		if step.state != "running":
			step.state = "running"
			step.attempt = tenant.attempts
			step.started_at = now_datetime()
			step.finished_at = None
			step.error = None
			tenant.save(ignore_permissions=True)
			frappe.db.commit()
		try:
			STEP_HANDLERS[step.step_key](tenant, step, client)
		except StepPending:
			tenant.save(ignore_permissions=True)
			frappe.db.commit()
			return
		except Exception as e:
			_fail(tenant, step, e)
			return
		step.state = "done"
		step.finished_at = now_datetime()
		tenant.save(ignore_permissions=True)
		frappe.db.commit()

	tenant.status = "ready"
	tenant.ready_at = now_datetime()
	tenant.status_message = None
	tenant.last_error = None
	tenant.save(ignore_permissions=True)
	frappe.db.commit()
	_send_ready_email(tenant)


def _fail(tenant, step, error: Exception):
	frappe.db.rollback()
	tenant.reload()
	detail = str(error) if isinstance(error, PressError | NotImplementedError) else repr(error)
	for row in tenant.steps:
		if row.step_key == step.step_key:
			row.state = "failed"
			row.finished_at = now_datetime()
			row.error = detail[:1000]
	tenant.status = "failed"
	tenant.status_message = FAILED_MESSAGE
	tenant.last_error = f"{step.step_key}: {detail}"[:1000]
	tenant.save(ignore_permissions=True)
	frappe.log_error(title=f"GrowthX provisioning failed: {tenant.name} ({step.step_key})")
	frappe.db.commit()


# ---------------------------------------------------------------- steps


def _create_site(tenant, step, client: PressClient):
	if tenant.press_site_id:
		return  # already created on an earlier attempt
	product = frappe.get_cached_doc("GX Product", tenant.product)
	apps = ["frappe", "erpnext", "growthx_theme"] + ([product.vertical_app] if product.vertical_app else [])
	site = client.find_site(tenant.subdomain) or client.create_site(
		tenant.subdomain, product.press_release_group, apps
	)
	tenant.press_site_id = site.id
	tenant.site_url = client.site_url(tenant.subdomain)
	# Save right away: this is what makes the job idempotent.
	tenant.save(ignore_permissions=True)
	frappe.db.commit()


def _wait_for_site(tenant, step, client: PressClient):
	site = client.get_site(tenant.press_site_id)
	if site.status == "active":
		return
	if site.status == "broken":
		raise PressError(f"Press reports the site as broken ({site.id})")
	if step.started_at and now_datetime() - step.started_at > WAIT_TIMEOUT:
		raise PressError(f"Timed out after {WAIT_TIMEOUT} waiting for Press to create {site.id}")
	raise StepPending


def _install_apps(tenant, step, client: PressClient):
	product = frappe.get_cached_doc("GX Product", tenant.product)
	required = {"erpnext", "growthx_theme"} | ({product.vertical_app} if product.vertical_app else set())
	missing = required - set(client.get_site(tenant.press_site_id).apps)
	if missing:
		raise PressError(f"Apps not installed on {tenant.press_site_id}: {', '.join(sorted(missing))}")


def _configure(tenant, step, client: PressClient):
	client.configure_site(tenant.press_site_id, tenant_settings(tenant))


def tenant_settings(tenant, reset_owner_password: bool = False) -> dict:
	"""What a tenant site needs: company defaults, its owner, and single sign-on.

	Generates a new tenant key every time (the old one stops working), so the caller must pass
	these settings to the site straight away."""
	account = frappe.db.get_value("GX Account", tenant.account, ["account_name", "user"], as_dict=True)
	owner = frappe.db.get_value("User", account.user, ["email", "full_name"], as_dict=True)
	return {
		"company_name": account.account_name,
		"country": "India",
		"currency": "INR",
		"timezone": "Asia/Kolkata",
		"owner_email": owner.email,
		"owner_name": owner.full_name or account.account_name,
		"reset_owner_password": reset_owner_password,
		"core_url": site_login.core_url(),
		"tenant_key": site_login.new_tenant_key(tenant),
	}


def _health_check(tenant, step, client: PressClient):
	if not client.health_check(tenant.site_url):
		raise PressError(f"Health check failed for {tenant.site_url}")


STEP_HANDLERS = {
	"create_site": _create_site,
	"wait_for_site": _wait_for_site,
	"install_apps": _install_apps,
	"configure": _configure,
	"health_check": _health_check,
}


# ---------------------------------------------------------------- scheduler + notifications


def resume_pending():
	"""Every minute: re-run jobs waiting on Press, and jobs whose worker died mid-step."""
	cutoff = now_datetime() - STALE_AFTER
	for name in frappe.get_all(
		"GX Tenant",
		filters={"status": ("in", ["requested", "provisioning"]), "modified": ("<", cutoff)},
		pluck="name",
	):
		enqueue(name)
	# The per-minute "waiting on Press" case: re-check sooner than the stale cutoff.
	for name in frappe.get_all(
		"GX Tenant",
		filters={"status": "provisioning", "modified": (">=", cutoff)},
		pluck="name",
	):
		if frappe.db.exists(
			"GX Provisioning Step", {"parent": name, "step_key": "wait_for_site", "state": "running"}
		):
			enqueue(name)


def _send_ready_email(tenant):
	from growthx_core.api.v1._core import web_url

	email = frappe.db.get_value("GX Account", tenant.account, "contact_email")
	if not email:
		return
	try:
		frappe.sendmail(
			recipients=[email],
			subject="Your GrowthX site is ready",
			message=(
				f"<p>Your site <b>{frappe.utils.escape_html(tenant.display_name or tenant.name)}</b> is ready.</p>"
				f'<p><a href="{web_url("/dashboard")}">Open your GrowthX dashboard</a> and click "Open my app".</p>'
			),
		)
	except Exception:
		frappe.log_error(title=f"GrowthX: could not send site-ready email for {tenant.name}")
