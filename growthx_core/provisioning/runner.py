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
from growthx_core.provisioning import countries, site_login
from growthx_core.provisioning.apps import site_apps
from growthx_core.provisioning.press import SUBDOMAIN, PressClient, PressError, get_client

STEPS = ["create_site", "wait_for_site", "install_apps", "configure", "health_check"]
WAIT_TIMEOUT = timedelta(minutes=30)
STALE_AFTER = timedelta(minutes=2)
FAILED_MESSAGE = (
	"We hit a problem while preparing your site. Our team has been alerted and will fix it shortly."
)
# Must match Press's Blocked Domains (DevOps) and the website's list (docs 09 §2). Names under
# 5 characters can never be Press sites anyway. Anything starting with "test-" is reserved too.
RESERVED_SUBDOMAINS = {
	"www",
	"api",
	"app",
	"mail",
	"core",
	"core-test",
	"admin",
	"status",
	"support",
	"billing",
	"growthx",
	"growthx-core",
	"web-origin",
	"autodiscover",
	"bounce",
	"partner",
	"partners",
	"portal",
	"login",
	"signup",
	"account",
	"staging",
	"email",
	"docs",
	"help",
	"press",
}
TEST_PREFIX = "test-"
MAX_BUILDING = 2  # Press's agent runs 2 jobs at a time; more creations only queue there
MAX_TEST_SITES = 5  # live test sites allowed at once (DevOps)


class StepPending(Exception):
	"""The step is waiting on Press. Leave it running; the scheduler resumes the job."""


# ---------------------------------------------------------------- creating a tenant


def make_subdomain(display_name: str) -> str:
	"""A unique Press name: 5-32 of a-z, 0-9 and -. The Core test site makes only test-<name>
	sites; production never does."""
	base = re.sub(r"[^a-z0-9]+", "-", (display_name or "").lower()).strip("-") or "my-site"
	if _test_sites():
		base = base if base.startswith(TEST_PREFIX) else TEST_PREFIX + base
	elif base.startswith(TEST_PREFIX):
		base = base.replace("-", "", 1)
	base = base[:28].strip("-")
	if base in RESERVED_SUBDOMAINS or len(base) < 5:
		base = f"{base}-site"
	candidate, n = base, 1
	while frappe.db.exists("GX Tenant", {"subdomain": candidate}):
		n += 1
		candidate = f"{base}-{n}"
	return candidate


def site_subdomain(chosen: str) -> str:
	"""The Press name for a subdomain the customer chose: test-<name> on the Core test site."""
	return TEST_PREFIX + chosen if _test_sites() else chosen


def subdomain_problem(chosen: str) -> str | None:
	"""Why the customer can't have this subdomain ("invalid", "reserved" or "taken"), or None.
	Checked within Core only: Press allows 10 name checks a minute (auth.signup asks Press)."""
	if not chosen or not SUBDOMAIN.match(chosen):
		return "invalid"
	if chosen in RESERVED_SUBDOMAINS or chosen.startswith(TEST_PREFIX):
		return "reserved"
	if not SUBDOMAIN.match(site_subdomain(chosen)):
		return "invalid"  # too long for test-<name> on the Core test site
	if frappe.db.exists("GX Tenant", {"subdomain": site_subdomain(chosen)}):
		return "taken"
	return None


def longest_subdomain() -> int:
	return 32 - (len(TEST_PREFIX) if _test_sites() else 0)


def suggest_subdomain(chosen: str) -> str | None:
	"""A free name close to one that is taken or reserved, e.g. noor-solar-2."""
	if chosen.startswith(TEST_PREFIX):
		return None
	for n in range(2, 22):
		suffix = f"-{n}"
		candidate = f"{chosen[: longest_subdomain() - len(suffix)].strip('-')}{suffix}"
		if not subdomain_problem(candidate):
			return candidate
	return None


def press_name_taken(chosen: str) -> bool:
	"""Ask Press whether a chosen name is still free (auth.signup only: Press allows 10 checks a
	minute). If Press can't answer, sign-up goes ahead; create_site refuses a name that isn't ours."""
	client = get_client()
	if not hasattr(client, "exists"):
		return False  # the fake and local clients have no shared namespace
	try:
		return client.exists(site_subdomain(chosen))
	except PressError:
		frappe.log_error(title=f"GrowthX: Press couldn't check the name {chosen}")
		return False


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
	site = client.find_site(tenant.subdomain)
	if site and site.status in ("broken", "archived"):
		# An earlier attempt's broken site, being archived: Press frees the name only when
		# the archive finishes, so wait until the name is free before creating it again.
		_keep_waiting(step, f"Press to archive the old {site.id}")
	if not site:
		_check_capacity(tenant)
		site = client.create_site(tenant.subdomain, product.press_release_group, site_apps(tenant))
	tenant.press_site_id = site.id
	tenant.site_url = client.site_url(tenant.subdomain)
	# Save right away: this is what makes the job idempotent.
	tenant.save(ignore_permissions=True)
	frappe.db.commit()


def _check_capacity(tenant):
	"""Wait while MAX_BUILDING sites are building; on the Core test site, refuse a 6th live site."""
	others = frappe.get_all(
		"GX Tenant",
		filters={"press_site_id": ("is", "set"), "status": ("!=", "archived"), "name": ("!=", tenant.name)},
		fields=["name", "status", "subdomain"],
	)
	live_tests = [row for row in others if row.subdomain.startswith(TEST_PREFIX)]
	if _test_sites() and len(live_tests) >= MAX_TEST_SITES:
		raise PressError(
			f"{len(live_tests)} test sites are live; archive some first (at most {MAX_TEST_SITES})"
		)
	provisioning = [row.name for row in others if row.status == "provisioning"]
	building = provisioning and frappe.db.count(
		"GX Provisioning Step",
		{"parent": ("in", provisioning), "step_key": "wait_for_site", "state": ("!=", "done")},
	)
	if building and building >= MAX_BUILDING:
		raise StepPending


def _keep_waiting(step, what: str):
	if step.started_at and now_datetime() - step.started_at > WAIT_TIMEOUT:
		raise PressError(f"Timed out after {WAIT_TIMEOUT} waiting for {what}")
	raise StepPending


def _wait_for_site(tenant, step, client: PressClient):
	site = client.get_site(tenant.press_site_id)
	if site.status == "active":
		return
	if site.status == "broken":
		# Archive it now; an admin retry then creates it again under the same name.
		client.archive_site(site.id)
		tenant.press_site_id = None
		for row in tenant.steps:
			if row.step_key == "create_site":
				row.state = "pending"
		tenant.save(ignore_permissions=True)
		frappe.db.commit()  # _fail rolls back; the archive has already been asked for
		raise PressError(f"Press reports {site.id} as broken. It is being archived; Retry creates it again.")
	if site.status in ("inactive", "archived"):
		raise PressError(f"Press reports {site.id} as {site.status} while it was being created")
	_keep_waiting(step, f"Press to create {site.id}")


def _install_apps(tenant, step, client: PressClient):
	missing = [app for app in site_apps(tenant) if app != "frappe"]
	missing = sorted(set(missing) - set(client.get_site(tenant.press_site_id).apps))
	if missing:
		raise PressError(f"Apps not installed on {tenant.press_site_id}: {', '.join(missing)}")


def _configure(tenant, step, client: PressClient):
	configure(tenant, client)


def configure(tenant, client: PressClient, reset_owner_password: bool = False):
	"""Set up the tenant's site, and keep the API key of the GrowthX service user it creates."""
	result = client.configure_site(tenant.press_site_id, tenant_settings(tenant, reset_owner_password))
	if result and result.get("apiKey"):
		tenant.service_api_key = result["apiKey"]
		tenant.service_api_secret = result["apiSecret"]
		tenant.save(ignore_permissions=True)


def tenant_settings(tenant, reset_owner_password: bool = False) -> dict:
	"""What a tenant site needs: company defaults, its owner, and single sign-on.

	Generates a new tenant key every time (the old one stops working), so the caller must pass
	these settings to the site straight away."""
	from growthx_core.api.v1._core import web_url

	account = frappe.db.get_value("GX Account", tenant.account, ["account_name", "user"], as_dict=True)
	owner = frappe.db.get_value("User", account.user, ["email", "full_name"], as_dict=True)
	country = countries.for_tenant(tenant)
	return {
		"company_name": account.account_name,
		"country": country.country,
		"currency": country.currency,
		"timezone": country.timezone,
		"fiscal_year_start": country.fiscal_year_start,
		"chart_of_accounts": tenant.get("chart_of_accounts") or country.default_chart,
		"owner_email": owner.email,
		"owner_name": owner.full_name or account.account_name,
		"reset_owner_password": reset_owner_password,
		"core_url": site_login.core_url(),
		"web_url": web_url(),  # "Sign in with GrowthX" on the site's login page goes there
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
			"GX Provisioning Step",
			{"parent": name, "step_key": ("in", ["create_site", "wait_for_site"]), "state": "running"},
		):
			enqueue(name)


def archive_failed(tenant_name: str) -> bool:
	"""Admin: archive a site whose creation failed (e.g. a test sign-up), so it stops counting in
	health.provisioning. Returns False unless the tenant is `failed`; ready sites are never archived here."""
	if frappe.db.get_value("GX Tenant", tenant_name, "status") != "failed":
		return False
	archive(tenant_name)
	return True


def archive(tenant_name: str, client: PressClient | None = None):
	"""Delete the tenant's site on Press (it is backed up first) and mark the tenant archived."""
	client = client or get_client()
	tenant = frappe.get_doc("GX Tenant", tenant_name, for_update=True)
	if tenant.press_site_id:
		client.archive_site(tenant.press_site_id)
	tenant.status = "archived"
	tenant.status_message = None
	tenant.save(ignore_permissions=True)
	frappe.db.commit()


def _test_sites() -> bool:
	return bool(cint((frappe.conf.get("growthx_press") or {}).get("test_sites")))


def _ready_email(tenant, login_email: str, google: bool) -> str:
	"""Welcome email when the site is ready: how to get in, never a password."""
	from growthx_core.api.v1._core import web_url

	esc = frappe.utils.escape_html
	login_url, forgot_url = web_url("/login"), web_url("/forgot-password")
	if google:
		how = (
			"<b>Continue with Google</b>, using this Google account. You can also set a password: "
			f'<a href="{esc(forgot_url)}">Forgot password</a> sends you a link.'
		)
	else:
		how = (
			"your <b>email and the password</b> you chose at sign-up. "
			f'Forgot it? <a href="{esc(forgot_url)}">Reset your password</a>.'
		)
	lines = [
		f"<p>Welcome to GrowthX! Your site <b>{esc(tenant.display_name or tenant.name)}</b> is ready.</p>"
	]
	if tenant.site_url:
		lines.append(f'<p>Site address: <a href="{esc(tenant.site_url)}">{esc(tenant.site_url)}</a></p>')
	lines += [
		f"<p>Your login email: <b>{esc(login_email)}</b></p>",
		f"<p>How you sign in: {how}</p>",
		"<p><b>To open your site:</b></p><ol>"
		f'<li>Sign in at <a href="{esc(login_url)}">{esc(login_url)}</a>.</li>'
		"<li>Click <b>Open app</b>: it signs you in to your site.</li></ol>",
		# Customer sites can't send email yet, so no "Login with Email Link" (launch checklist item 19:
		# point to "Sign in with GrowthX" on the site once it exists).
		"<p>There's no separate password for your site: always open it from GrowthX.</p>",
	]
	return "".join(lines)


def _send_ready_email(tenant):
	from growthx_core import accounts

	account = frappe.db.get_value("GX Account", tenant.account, ["contact_email", "user"], as_dict=True)
	if not account or not account.contact_email:
		return
	login_email = frappe.db.get_value("User", account.user, "email") if account.user else None
	try:
		frappe.sendmail(
			recipients=[account.contact_email],
			subject="Welcome to GrowthX: your site is ready",
			message=_ready_email(
				tenant, login_email or account.contact_email, accounts.signs_in_with_google(account.user)
			),
		)
	except Exception:
		frappe.log_error(title=f"GrowthX: could not send site-ready email for {tenant.name}")
