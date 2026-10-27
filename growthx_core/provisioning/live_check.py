"""Checks against the real Press (tbocloud.in), for the integration runs with DevOps.

    bench --site <core-site> execute growthx_core.provisioning.live_check.read_only
    bench --site <core-site> execute growthx_core.provisioning.live_check.create_and_archive
    bench --site <core-site> execute growthx_core.provisioning.live_check.create_and_archive --kwargs "{'runs': 5}"

Uses `growthx_press` when it is in press mode, otherwise `growthx_press_live` (same keys, see
RealPressClient), so a development site can run the checks without sending its sign-ups to Press.
On a development bench the key can come from the environment instead of a file:
GX_PRESS_API_KEY and GX_PRESS_API_SECRET (the other settings default to the GrowthX test values).

read_only: the key, the release group's apps, a known site, reserved names, the health check.
create_and_archive: creates test-<name> sites one at a time through the job's own steps
(create, wait, apps, configure, health check), times them, then archives each and waits until
Press frees the name. Only on a Core test site (`test_sites: 1`). Never more than one live site.
"""

import os
import time
from unittest.mock import patch

import frappe
from frappe.utils import now_datetime

from growthx_core.provisioning import runner
from growthx_core.provisioning.press import PressError, RealPressClient

SMOKE_SITE = "test-smoke.tbogrowthx.com"
RESERVED = ("support", "growthx-core", "core-test")
NEEDED_APPS = {"frappe", "erpnext", "growthx_theme", "solar"}
PRODUCT = "solar"
ACCOUNT_EMAIL = "gx-live-check@tbogrowthx.com"
POLL_SECONDS = 20


TEST_DEFAULTS = {
	"mode": "press",
	"url": "https://tbocloud.in",
	"team": "lgcrlmj0t5",
	"domain": "tbogrowthx.com",
	"plan": "GrowthX Demo",
	"test_sites": 1,
	"core_url": "https://core-test.tbogrowthx.com",
}


def _conf() -> dict:
	conf = frappe.conf.get("growthx_press") or {}
	if conf.get("mode") != "press":
		conf = {**TEST_DEFAULTS, **(frappe.conf.get("growthx_press_live") or {})}
		if os.environ.get("GX_PRESS_API_KEY"):
			conf["api_key"] = os.environ["GX_PRESS_API_KEY"]
			conf["api_secret"] = os.environ.get("GX_PRESS_API_SECRET")
	if not conf.get("api_key") or not conf.get("api_secret"):
		frappe.throw("Set growthx_press_live (or growthx_press in press mode) with api_key and api_secret")
	return conf


def _ok(label, passed, detail=""):
	print(f"  {'PASS' if passed else 'FAIL'}  {label}{f': {detail}' if detail else ''}")
	return passed


def read_only():
	conf = _conf()
	client = RealPressClient(conf)
	group = frappe.db.get_value("GX Product", PRODUCT, "press_release_group") or conf.get("group")
	print(f"Press {client.url}, team {client.team}, release group {group}")
	results = []

	account = client._call("press.api.account.get")
	teams = sorted({t.get("name") for t in account.get("teams") or [] if isinstance(t, dict)})
	results.append(_ok("key works", bool(account), f"teams {teams}"))

	apps = set(client.group_apps(group))
	results.append(_ok(f"{group} has the Solar apps", NEEDED_APPS <= apps, ", ".join(sorted(apps))))

	smoke = client.get_site(SMOKE_SITE)
	results.append(_ok(f"{SMOKE_SITE} is active", smoke.status == "active", smoke.status))
	results.append(_ok(f"{SMOKE_SITE} apps", NEEDED_APPS <= set(smoke.apps), ", ".join(sorted(smoke.apps))))

	for name in RESERVED:
		results.append(_ok(f"{name} is reserved", client.exists(name)))
	free = f"test-free-{frappe.generate_hash(length=6)}"
	results.append(_ok(f"{free} is free", not client.exists(free)))

	results.append(_ok("health check: live site", client.health_check(f"https://{SMOKE_SITE}")))
	results.append(
		_ok("health check: unknown site (307) fails", not client.health_check(client.site_url(free)))
	)
	print(f"{sum(results)}/{len(results)} passed")
	return all(results)


def create_and_archive(runs: int = 1, keep: bool = False):
	conf = _conf()
	client = RealPressClient(conf)
	if not client.test_sites:
		frappe.throw("Only on a Core test site (test_sites: 1)")
	core_url = conf.get("core_url") or "https://core-test.tbogrowthx.com"
	timings = []
	for i in range(1, int(runs) + 1):
		print(f"Run {i}/{runs}")
		with patch.dict(frappe.local.conf, {"growthx_press": conf, "growthx_core_url": core_url}):
			tenant = _new_tenant()
			try:
				timings.append(_provision(tenant, client))
			except PressError as e:
				print(f"  FAIL  {e}")
				break
			finally:
				if not keep:
					_archive(tenant, client)
	if timings:
		print("Minutes to a ready site: " + ", ".join(f"{t:.1f}" for t in timings))
	return timings


def _new_tenant():
	account = frappe.db.get_value("GX Account", {"contact_email": ACCOUNT_EMAIL}, "name")
	if not account:
		if not frappe.db.exists("User", ACCOUNT_EMAIL):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": ACCOUNT_EMAIL,
					"first_name": "Live Check",
					"send_welcome_email": 0,
				}
			).insert(ignore_permissions=True)
		account = (
			frappe.get_doc(
				{
					"doctype": "GX Account",
					"account_name": "GrowthX Live Check",
					"user": ACCOUNT_EMAIL,
					"contact_email": ACCOUNT_EMAIL,
				}
			)
			.insert(ignore_permissions=True)
			.name
		)
	tenant = frappe.get_doc(
		{
			"doctype": "GX Tenant",
			"account": account,
			"product": PRODUCT,
			"display_name": "Live Check",
			"subdomain": runner.make_subdomain(f"Live Check {frappe.generate_hash(length=4)}"),
			# Not "provisioning": this check runs the steps itself, and the scheduler's resume_pending
			# would otherwise start runner.run on the same tenant (a second site.new or configure).
			# runner.run ignores tenants in any other status.
			"status": "suspended",
			"status_message": "Live check: steps run by live_check, not the scheduler.",
		}
	).insert(ignore_permissions=True)
	runner.init_steps(tenant)
	tenant.save(ignore_permissions=True)
	frappe.db.commit()
	print(f"  tenant {tenant.name}, site {tenant.subdomain}")
	return tenant


def _provision(tenant, client) -> float:
	started = now_datetime()
	for step in tenant.steps:
		step.state = "running"
		step.started_at = now_datetime()
		while True:
			try:
				runner.STEP_HANDLERS[step.step_key](tenant, step, client)
				break
			except runner.StepPending:
				time.sleep(POLL_SECONDS)
			except Exception as e:
				# Keep the record on the tenant (desk: GX Tenant → steps), then stop this run.
				step.state, step.finished_at, step.error = "failed", now_datetime(), str(e)[:1000]
				tenant.save(ignore_permissions=True)
				frappe.db.commit()
				raise
		step.state, step.finished_at = "done", now_datetime()
		tenant.save(ignore_permissions=True)
		frappe.db.commit()
		minutes = (now_datetime() - started).total_seconds() / 60
		print(f"  {step.step_key:<14} done at {minutes:.1f} min")
	tenant.status = "ready"
	tenant.save(ignore_permissions=True)
	frappe.db.commit()
	return (now_datetime() - started).total_seconds() / 60


def _archive(tenant, client):
	tenant.reload()
	if not tenant.press_site_id:
		return
	print(f"  archiving {tenant.press_site_id}")
	runner.archive(tenant.name, client)
	started = time.monotonic()
	while client.exists(tenant.subdomain):
		if time.monotonic() - started > 30 * 60:
			raise PressError(f"{tenant.subdomain} still taken 30 min after archiving")
		time.sleep(POLL_SECONDS)
	print(f"  name free again after {(time.monotonic() - started) / 60:.1f} min")
