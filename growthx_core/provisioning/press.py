"""Client for Frappe Press (owned by DevOps).

`growthx_press` in site_config selects the client:
    {"mode": "fake"}                         # default: local development and tests
    {"mode": "fake", "fail_step": "configure"}  # make the fake fail a step, to test retries
    {"mode": "press", ...}                   # tbocloud.in Press, see RealPressClient
    {"mode": "local", ...}                   # real sites on this bench, see LocalPressClient
"""

import json
import os
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import frappe
import requests
from frappe.utils import cint, get_bench_path


class PressError(Exception):
	"""Press refused or failed. The message is admin-only detail."""


class PressNotFound(PressError):
	"""Press has no such site, or it isn't the GrowthX team's (Press answers the same for both)."""


@dataclass
class PressSite:
	id: str
	status: str  # creating | active | broken | inactive | archived
	apps: list[str] = field(default_factory=list)


class PressClient:
	def site_url(self, subdomain: str) -> str:
		return f"https://{subdomain}.{tenant_domain()}"

	def create_site(self, subdomain: str, release_group: str | None, apps: list[str]) -> PressSite:
		raise NotImplementedError

	def get_site(self, site_id: str) -> PressSite:
		raise NotImplementedError

	def find_site(self, subdomain: str) -> PressSite | None:
		raise NotImplementedError

	def archive_site(self, site_id: str) -> None:
		"""Delete a site (Press backs it up first). Press frees the name only when this finishes."""
		raise NotImplementedError

	def install_app(self, site_id: str, app: str) -> None:
		"""Install `app` on an existing site (used to backfill growthx_theme)."""
		raise NotImplementedError

	def suspend_site(self, site_id: str) -> None:
		"""Take a site offline (an admin suspended the customer). Its data is kept."""
		raise NotImplementedError

	def resume_site(self, site_id: str) -> None:
		"""Bring a suspended site back online."""
		raise NotImplementedError

	def configure_site(self, site_id: str, settings: dict) -> dict | None:
		"""Set up a new site. Safe to run again on a configured site.

		`settings`: company_name, country, currency, timezone, plus for single sign-on:
		- owner_email, owner_name: the customer's user. Created with a random password nobody
		  knows (they sign in through GrowthX, or with an email link); `reset_owner_password`
		  replaces the password of an existing owner the same way.
		- core_url, tenant_key: stored on the site for growthx_theme's sso.login.
		Also enables "Login with Email Link" as the fallback way in.
		Returns the GrowthX service user's {"apiKey", "apiSecret"} when the site has one.
		The vertical's own set-up (solar) is being agreed with Nahala."""
		raise NotImplementedError

	def health_check(self, site_url: str) -> bool:
		raise NotImplementedError


class FakePressClient(PressClient):
	"""Behaves like Press, instantly. Used until the real Press API is available."""

	def __init__(self, fail_step: str | None = None):
		self.fail_step = fail_step

	def _maybe_fail(self, step):
		if self.fail_step == step:
			raise PressError(f"Fake Press failure in step {step} (growthx_press.fail_step)")

	def create_site(self, subdomain, release_group, apps):
		self._maybe_fail("create_site")
		return PressSite(id=f"fake-{subdomain}", status="active", apps=list(apps))

	def get_site(self, site_id):
		self._maybe_fail("wait_for_site")
		# Reports every app a product or tenant lists (plus the always-installed ones) as installed.
		from growthx_core.provisioning.apps import BASE_APPS, THEME_APP, all_listed_apps

		return PressSite(
			id=site_id, status="active", apps=[*BASE_APPS, THEME_APP, *sorted(all_listed_apps())]
		)

	def find_site(self, subdomain):
		return None

	def archive_site(self, site_id):
		pass

	def install_app(self, site_id, app):
		pass

	def suspend_site(self, site_id):
		pass

	def resume_site(self, site_id):
		pass

	def configure_site(self, site_id, settings):
		self._maybe_fail("configure")

	def health_check(self, site_url):
		self._maybe_fail("health_check")
		return True


SUBDOMAIN = re.compile(r"^[a-z0-9][a-z0-9-]{3,30}[a-z0-9]$")  # Press: 5-32 characters
TEST_PREFIX = "test-"
PRESS_STATUS = {
	"Pending": "creating",
	"Installing": "creating",
	"Updating": "creating",  # busy for a moment (deploys, restores): keep waiting
	"Recovering": "creating",
	"Active": "active",
	"Broken": "broken",
	"Inactive": "inactive",
	"Suspended": "inactive",
	"Archived": "archived",
}


class RealPressClient(PressClient):
	"""TBO's self-hosted Press, tbocloud.in (DevOps guide: docs/press-api-for-core.md, Arun).

	    {"mode": "press",
	     "url": "https://tbocloud.in", "team": "lgcrlmj0t5",
	     "api_key": "...", "api_secret": "...",      # this Core site's own Press user; never in Git
	     "domain": "tbogrowthx.com", "plan": "GrowthX Demo",
	     "test_sites": 1}     # the Core test site: may create only test-<name> sites. Production
	                          # Core leaves it out and refuses test- names.

	The key reaches everything the GrowthX team owns, including Core's own site, so every call
	that changes a site first checks that the site belongs to a GX Tenant (`_own`).
	"""

	def __init__(self, conf: dict):
		self.url = (conf.get("url") or "https://tbocloud.in").rstrip("/")
		self.team = conf.get("team")
		self.domain = conf.get("domain") or "tbogrowthx.com"
		self.plan = conf.get("plan") or "GrowthX Demo"
		self.test_sites = cint(conf.get("test_sites"))
		self.session = requests.Session()
		self.session.headers["Authorization"] = f"token {conf.get('api_key')}:{conf.get('api_secret')}"
		if self.team:
			self.session.headers["X-Press-Team"] = self.team

	# ---- Press API

	def _call(self, method: str, get: bool = False, timeout: int = 30, **params):
		url = f"{self.url}/api/method/{method}"
		try:
			if get:
				r = self.session.get(url, params=params, timeout=timeout, allow_redirects=False)
			else:
				r = self.session.post(url, json=params, timeout=timeout, allow_redirects=False)
		except requests.RequestException as e:
			raise PressError(f"{method}: Press unreachable ({type(e).__name__})") from None
		if r.status_code == 200:
			return r.json().get("message")
		detail = _error_text(r)
		if r.status_code == 403 or "DoesNotExistError" in detail:
			raise PressNotFound(f"{method}: {detail}")
		raise PressError(f"{method}: HTTP {r.status_code} {detail}")

	def site_name(self, subdomain: str) -> str:
		return f"{subdomain}.{self.domain}"

	def site_url(self, subdomain):
		return f"https://{self.site_name(subdomain)}"

	def exists(self, subdomain: str) -> bool:
		"""True if the name is taken or reserved. Press allows 10 calls a minute."""
		return bool(self._call("press.api.site.exists", get=True, subdomain=subdomain, domain=self.domain))

	def group_apps(self, group: str) -> list[str]:
		return sorted({_app_name(row) for row in self._call("press.api.bench.apps", name=group) or []})

	# ---- safety

	def check_subdomain(self, subdomain: str):
		if not SUBDOMAIN.match(subdomain or ""):
			raise PressError(f"{subdomain!r} isn't a valid Press name (5-32 of a-z, 0-9 and -)")
		if self.test_sites and not subdomain.startswith(TEST_PREFIX):
			raise PressError(f"This is the Core test site: it may only create {TEST_PREFIX}<name> sites")
		if not self.test_sites and subdomain.startswith(TEST_PREFIX):
			raise PressError(f"{TEST_PREFIX}<name> sites are for the Core test site only")

	def _own(self, site_id: str):
		"""Refuse any site that isn't a GX Tenant's, such as Core's own site."""
		if not frappe.db.exists("GX Tenant", {"press_site_id": site_id}):
			raise PressError(f"Refusing to act on {site_id}: it isn't a GrowthX tenant site")

	# ---- PressClient

	def create_site(self, subdomain, release_group, apps):
		self.check_subdomain(subdomain)
		if not frappe.db.exists("GX Tenant", {"subdomain": subdomain}):
			raise PressError(f"Refusing to create {subdomain}: no GX Tenant has this subdomain")
		if not release_group:
			raise PressError("The product has no Press release group (GX Product → Press Release Group)")
		# Press would accept the request and then fail the site; refuse it here instead.
		missing = sorted(set(apps) - set(self.group_apps(release_group)))
		if missing:
			raise PressError(f"Not in Press release group {release_group}: {', '.join(missing)}")
		result = self._call(
			"press.api.site.new",
			site={
				"name": subdomain,
				"domain": self.domain,
				"group": release_group,
				"apps": list(apps),
				"plan": self.plan,
				"cluster": "Default",
			},
		)
		return PressSite(id=result["site"], status="creating", apps=[])

	def get_site(self, site_id):
		site = self._call("press.api.site.get", name=site_id)
		status = PRESS_STATUS.get(site.get("status"), "creating")
		apps = []
		if status == "active":
			apps = [_app_name(row) for row in self._call("press.api.site.installed_apps", name=site_id) or []]
		return PressSite(id=site_id, status=status, apps=apps)

	def find_site(self, subdomain):
		if not self.exists(subdomain):
			return None
		try:
			return self.get_site(self.site_name(subdomain))
		except PressNotFound:
			raise PressError(f"{subdomain} is taken on Press by a site that isn't GrowthX's") from None

	def archive_site(self, site_id):
		self._own(site_id)
		self._call("press.api.site.archive", name=site_id, force=1)

	def install_app(self, site_id, app):
		self._own(site_id)
		self._call("press.api.site.install_app", name=site_id, app=app)

	def suspend_site(self, site_id):
		self._own(site_id)
		self._call("press.api.site.deactivate", name=site_id)

	def resume_site(self, site_id):
		self._own(site_id)
		self._call("press.api.site.activate", name=site_id)

	def configure_site(self, site_id, settings):
		"""Log in as Administrator through Press, then run growthx_theme's set-up on the site.

		The tenant key goes to the site directly over HTTPS, never through Press's update_config,
		whose values can end up in Press-side logs."""
		self._own(site_id)
		login = self._call("press.api.site.login", name=site_id, reason="GrowthX provisioning")
		try:
			r = requests.post(
				f"https://{site_id}/api/method/growthx_theme.setup.configure",
				json={"settings": settings},
				cookies={"sid": login["sid"]},
				timeout=300,  # the setup wizard takes a minute or two
				allow_redirects=False,
			)
		except requests.RequestException as e:
			raise PressError(f"Configuring {site_id}: site unreachable ({type(e).__name__})") from None
		if r.status_code != 200:
			raise PressError(f"Configuring {site_id}: HTTP {r.status_code} {_error_text(r)}")
		return r.json().get("message")

	def health_check(self, site_url):
		# Only a direct 200 from the tenant counts: an unknown name redirects (307) to tbocloud.in.
		try:
			r = requests.get(f"{site_url}/api/method/ping", timeout=15, allow_redirects=False)
		except requests.RequestException:
			return False
		return r.status_code == 200 and r.json().get("message") == "pong"


def _app_name(row) -> str:
	return row if isinstance(row, str) else (row.get("app") or row.get("name"))


def _error_text(response) -> str:
	"""Press's error message, short. Never the request (it carries the key)."""
	try:
		body = response.json()
	except ValueError:
		return response.text[:300]
	messages = []
	for raw in json.loads(body.get("_server_messages") or "[]"):
		try:
			messages.append(json.loads(raw).get("message") or raw)
		except (ValueError, AttributeError):
			messages.append(raw)
	text = " ".join(filter(None, [body.get("exc_type"), *messages])) or str(body.get("message") or "")
	return text[:300]


class LocalPressClient(PressClient):
	"""Creates real sites on this bench with `bench new-site`, so a tenant can be opened locally
	before the Press API exists. Development only: never use it on a server.

	    {"mode": "local",
	     "db_root_username": "root", "db_root_password": "",   # MariaDB admin for new-site
	     "admin_password": "admin",      # Administrator on each tenant (local development only)
	     "domain": "localhost", "port": 8003,   # tenant URL: http://<subdomain>.localhost:8003
	     "allow_missing_apps": 1}        # report the vertical app as installed when this bench
	                                     # lacks it, so the job can finish (never growthx_theme)

	Sites are named `<subdomain>.<domain>`. They need a web process that picks the site from the
	Host header; `bench serve` doesn't when `default_site` is set (README). Browsers resolve
	*.localhost to this machine.
	Site creation runs in a detached process (it takes a few minutes); `get_site` reads its
	exit status from logs/growthx-local-press/<site>.status.
	"""

	def __init__(self, conf: dict):
		self.bench = conf.get("bench_path") or get_bench_path()
		self.sites = os.path.join(self.bench, "sites")
		self.python = os.path.join(self.bench, "env", "bin", "python")
		self.state_dir = os.path.join(self.bench, "logs", "growthx-local-press")
		self.domain = conf.get("domain") or "localhost"
		self.port = cint(conf.get("port")) or 8001
		self.db_root_username = conf.get("db_root_username") or "root"
		self.db_root_password = conf.get("db_root_password") or ""
		self.admin_password = conf.get("admin_password") or "admin"
		self.allow_missing_apps = cint(conf.get("allow_missing_apps"))

	# ---- helpers

	def _site_name(self, subdomain: str) -> str:
		return f"{subdomain}.{self.domain}"

	def _status_file(self, site: str) -> str:
		return os.path.join(self.state_dir, f"{site}.status")

	def _bench_apps(self) -> list[str]:
		with open(os.path.join(self.sites, "apps.txt")) as f:
			return [line.strip() for line in f if line.strip()]

	def _frappe(self, *args: str, timeout: int = 600) -> str:
		"""Run a frappe CLI command for this bench and return stdout."""
		result = subprocess.run(
			[self.python, "-m", "frappe.utils.bench_helper", "frappe", *args],
			cwd=self.sites,
			capture_output=True,
			text=True,
			timeout=timeout,
		)
		if result.returncode != 0:
			raise PressError(
				f"`frappe {' '.join(args[:3])}` failed: {(result.stderr or result.stdout)[-800:]}"
			)
		return result.stdout

	# ---- PressClient

	def site_url(self, subdomain):
		return f"http://{self._site_name(subdomain)}:{self.port}"

	def create_site(self, subdomain, release_group, apps):
		site = self._site_name(subdomain)
		os.makedirs(self.state_dir, exist_ok=True)
		available = set(self._bench_apps())
		install = [app for app in apps if app != "frappe" and app in available]

		cmd = [self.python, "-m", "frappe.utils.bench_helper", "frappe", "new-site", site]
		cmd += ["--db-root-username", self.db_root_username, "--admin-password", self.admin_password]
		if self.db_root_password:
			cmd += ["--db-root-password", self.db_root_password]
		for app in install:
			cmd += ["--install-app", app]

		log = os.path.join(self.state_dir, f"{site}.log")
		status = self._status_file(site)
		# An empty root password makes new-site prompt for it; the piped newline answers.
		script = (
			f"cd {shlex.quote(self.sites)} && printf '\\n' | {shlex.join(cmd)} > {shlex.quote(log)} 2>&1; "
			f"echo $? > {shlex.quote(status)}"
		)
		subprocess.Popen(
			["/bin/bash", "-c", script],
			start_new_session=True,
			stdin=subprocess.DEVNULL,
			stdout=subprocess.DEVNULL,
			stderr=subprocess.DEVNULL,
		)
		return PressSite(id=site, status="creating", apps=["frappe", *install])

	def get_site(self, site_id):
		try:
			with open(self._status_file(site_id)) as f:
				code = f.read().strip()
		except FileNotFoundError:
			return PressSite(id=site_id, status="creating")
		if code != "0":
			return PressSite(id=site_id, status="broken")

		installed = list(
			json.loads(self._frappe("--site", site_id, "list-apps", "-f", "json")).get(site_id, [])
		)
		if self.allow_missing_apps:
			from growthx_core.provisioning.apps import THEME_APP, all_listed_apps

			verticals = [app for app in all_listed_apps() if app != THEME_APP]
			# Never growthx_theme: without it single sign-on can't work, so the job must fail.
			missing = [app for app in verticals if app not in self._bench_apps()]
			installed += missing
		return PressSite(id=site_id, status="active", apps=installed)

	def find_site(self, subdomain):
		site = self._site_name(subdomain)
		if os.path.isdir(os.path.join(self.sites, site)) or os.path.exists(self._status_file(site)):
			return self.get_site(site)
		return None

	def suspend_site(self, site_id):
		pass  # local sites stay up; the tenant's status is what blocks "Open my app"

	def resume_site(self, site_id):
		pass

	def install_app(self, site_id, app):
		if app not in self._bench_apps():
			raise PressError(f"{app} is not on this bench (bench get-app {app})")
		installed = json.loads(self._frappe("--site", site_id, "list-apps", "-f", "json")).get(site_id, [])
		if app not in installed:
			self._frappe("--site", site_id, "install-app", app)

	def configure_site(self, site_id, settings):
		"""Run growthx_theme's set-up on the site, the same code Core calls on Press sites."""
		public = {k: v for k, v in settings.items() if k != "tenant_key"}
		code = f"""
import json, os, frappe
frappe.init(site={site_id!r}, sites_path={self.sites!r}); frappe.connect()
frappe.set_user("Administrator")
from growthx_theme.setup import configure
settings = {{**json.loads({json.dumps(public)!r}), "tenant_key": os.environ.get("GX_TENANT_KEY")}}
print("GX_RESULT " + json.dumps(configure(settings)))
frappe.destroy()
"""
		# The tenant key goes through the environment, not the command line, so it can't be read from `ps`.
		env = {**os.environ, "GX_TENANT_KEY": settings.get("tenant_key") or ""}
		result = subprocess.run(
			[self.python, "-c", code], cwd=self.sites, capture_output=True, text=True, timeout=900, env=env
		)
		if result.returncode != 0:
			raise PressError(f"Configuring {site_id} failed: {(result.stderr or result.stdout)[-800:]}")
		for line in result.stdout.splitlines():
			if line.startswith("GX_RESULT "):
				return json.loads(line.removeprefix("GX_RESULT "))

	def health_check(self, site_url):
		# Log in as Administrator, connecting to this machine directly with the site in the Host
		# header. A ping alone isn't enough: a web process pinned to another site (see README)
		# answers "pong" for every host, but only the right site accepts this password.
		host = urlsplit(site_url).hostname
		try:
			r = requests.post(
				f"http://127.0.0.1:{self.port}/api/method/login",
				data={"usr": "Administrator", "pwd": self.admin_password},
				headers={"Host": host},
				timeout=15,
			)
		except requests.RequestException:
			return False
		return r.ok and r.json().get("message") == "Logged In"


def get_client() -> PressClient:
	conf = frappe.conf.get("growthx_press") or {}
	if conf.get("mode") == "press":
		return RealPressClient(conf)
	if conf.get("mode") == "local":
		return LocalPressClient(conf)
	return FakePressClient(fail_step=conf.get("fail_step"))


def tenant_domain() -> str:
	return frappe.conf.get("growthx_tenant_domain") or "growthx.local"
