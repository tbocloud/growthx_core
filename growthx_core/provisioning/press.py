"""Client for Frappe Press (owned by DevOps).

`growthx_press` in site_config selects the client:
    {"mode": "fake"}                         # default: local development and tests
    {"mode": "fake", "fail_step": "configure"}  # make the fake fail a step, to test retries
    {"mode": "press", "url": "...", "token": "..."}  # real Press, once DevOps delivers the API
    {"mode": "local", ...}                   # real sites on this bench, see LocalPressClient

The real Press calls are not written yet: the endpoints, payloads and auth are open questions
for DevOps (Arun). Until then `mode: press` fails at the first step with a clear admin error.
"""

import json
import os
import shlex
import subprocess
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import urlsplit

import frappe
import requests
from frappe.utils import cint, get_bench_path


class PressError(Exception):
	"""Press refused or failed. The message is admin-only detail."""


@dataclass
class PressSite:
	id: str
	status: str  # creating | active | broken
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

	def configure_site(self, site_id: str, settings: dict) -> None:
		"""Run the vertical's set-up on the new site (company, admin user, defaults).
		The method to call in solar_operations is being agreed with Nahala."""
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
		verticals = [app for app in frappe.get_all("GX Product", pluck="vertical_app") if app]
		return PressSite(id=site_id, status="active", apps=["frappe", "erpnext", "growthx_theme", *verticals])

	def find_site(self, subdomain):
		return None

	def configure_site(self, site_id, settings):
		self._maybe_fail("configure")

	def health_check(self, site_url):
		self._maybe_fail("health_check")
		return True


class RealPressClient(PressClient):
	def __init__(self, url: str | None, token: str | None):
		self.url = url
		self.token = token

	def _pending(self):
		raise PressError(
			"The Press API client is not implemented yet: waiting on DevOps for endpoints, payloads and a scoped token."
		)

	def create_site(self, subdomain, release_group, apps):
		self._pending()

	def get_site(self, site_id):
		self._pending()

	def find_site(self, subdomain):
		self._pending()

	def configure_site(self, site_id, settings):
		self._pending()

	def health_check(self, site_url):
		self._pending()


class LocalPressClient(PressClient):
	"""Creates real sites on this bench with `bench new-site`, so a tenant can be opened locally
	before the Press API exists. Development only: never use it on a server.

	    {"mode": "local",
	     "db_root_username": "root", "db_root_password": "",   # MariaDB admin for new-site
	     "admin_password": "admin",      # Administrator on each tenant
	     "user_password": "admin",       # the customer's user on each tenant
	     "domain": "localhost", "port": 8003,   # tenant URL: http://<subdomain>.localhost:8003
	     "allow_missing_apps": 1}        # report apps this bench lacks (growthx_theme, the
	                                     # vertical) as installed, so the job can finish

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
		self.user_password = conf.get("user_password") or self.admin_password
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
			raise PressError(f"`frappe {' '.join(args[:3])}` failed: {(result.stderr or result.stdout)[-800:]}")
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

		installed = list(json.loads(self._frappe("--site", site_id, "list-apps", "-f", "json")).get(site_id, []))
		if self.allow_missing_apps:
			verticals = [app for app in frappe.get_all("GX Product", pluck="vertical_app") if app]
			missing = [app for app in ["growthx_theme", *verticals] if app not in self._bench_apps()]
			installed += missing
		return PressSite(id=site_id, status="active", apps=installed)

	def find_site(self, subdomain):
		site = self._site_name(subdomain)
		if os.path.isdir(os.path.join(self.sites, site)) or os.path.exists(self._status_file(site)):
			return self.get_site(site)
		return None

	def configure_site(self, site_id, settings):
		"""Run ERPNext's setup wizard on the tenant and create the customer's user."""
		today = date.today()
		fy_start = date(today.year if today.month >= 4 else today.year - 1, 4, 1)
		args = {
			"language": "English",
			"country": settings.get("country") or "India",
			"currency": settings.get("currency") or "INR",
			"timezone": settings.get("timezone") or "Asia/Kolkata",
			"company_name": settings.get("company_name") or site_id,
			"chart_of_accounts": "Standard",
			"fy_start_date": fy_start.isoformat(),
			"fy_end_date": date(fy_start.year + 1, 3, 31).isoformat(),
		}
		code = f"""
import json, frappe
frappe.init(site={site_id!r}, sites_path={self.sites!r}); frappe.connect()
frappe.set_user("Administrator")
args, email = json.loads({json.dumps(args)!r}), {settings.get("admin_email")!r}
if not frappe.is_setup_complete():
	from frappe.desk.page.setup_wizard.setup_wizard import setup_complete
	result = setup_complete(args)
	if not frappe.is_setup_complete():
		raise SystemExit(f"setup wizard failed: {{result}}")
if email and not frappe.db.exists("User", email):
	user = frappe.get_doc({{"doctype": "User", "email": email, "first_name": args["company_name"],
		"send_welcome_email": 0}})
	user.append("roles", {{"role": "System Manager"}})
	user.insert(ignore_permissions=True)
	# Set directly: the password-strength policy would reject a simple local dev password.
	from frappe.utils.password import update_password
	update_password(email, {self.user_password!r})
frappe.db.commit()
frappe.destroy()
"""
		result = subprocess.run(
			[self.python, "-c", code], cwd=self.sites, capture_output=True, text=True, timeout=900
		)
		if result.returncode != 0:
			raise PressError(f"Configuring {site_id} failed: {(result.stderr or result.stdout)[-800:]}")

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
		return RealPressClient(conf.get("url"), conf.get("token"))
	if conf.get("mode") == "local":
		return LocalPressClient(conf)
	return FakePressClient(fail_step=conf.get("fail_step"))


def tenant_domain() -> str:
	return frappe.conf.get("growthx_tenant_domain") or "growthx.local"
