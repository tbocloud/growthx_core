"""Client for Frappe Press (owned by DevOps).

`growthx_press` in site_config selects the client:
    {"mode": "fake"}                         # default: local development and tests
    {"mode": "fake", "fail_step": "configure"}  # make the fake fail a step, to test retries
    {"mode": "press", "url": "...", "token": "..."}  # real Press, once DevOps delivers the API

The real Press calls are not written yet: the endpoints, payloads and auth are open questions
for DevOps (Arun). Until then `mode: press` fails at the first step with a clear admin error.
"""

from dataclasses import dataclass, field

import frappe


class PressError(Exception):
	"""Press refused or failed. The message is admin-only detail."""


@dataclass
class PressSite:
	id: str
	status: str  # creating | active | broken
	apps: list[str] = field(default_factory=list)


class PressClient:
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


def get_client() -> PressClient:
	conf = frappe.conf.get("growthx_press") or {}
	if conf.get("mode") == "press":
		return RealPressClient(conf.get("url"), conf.get("token"))
	return FakePressClient(fail_step=conf.get("fail_step"))


def tenant_domain() -> str:
	return frappe.conf.get("growthx_tenant_domain") or "growthx.local"
