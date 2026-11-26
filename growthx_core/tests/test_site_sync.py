"""The desired-state list for the Press-side job, its reports, and platform-site matching."""

import json

import frappe
from frappe.utils import add_months, nowdate

from growthx_core.billing import renewals, site_sync
from growthx_core.tests import test_admin_direct as direct_tests
from growthx_core.tests.test_renewals import RenewalsCase


def _platform(*sites):
	return json.dumps(list(sites))


class TestSiteSync(RenewalsCase):
	def _direct(self, status="active"):
		_, sub, tenant = self._direct_customer(status, current_period_end=add_months(nowdate(), 1))
		return sub, tenant

	def _on_platform(self, tenant, *extra, domains=None):
		host = site_sync.host_of(tenant.site_url)
		site_sync.report_platform_sites(
			_platform(
				{"site": host, "group": "bench-1", "created_on": "2026-01-01", "domains": domains or []},
				*extra,
			)
		)
		tenant.reload()
		return host

	def _states(self):
		return {row["site"]: row for row in site_sync.get_site_states()["sites"]}

	def test_states_lists_only_matched_direct_sites(self):
		_, direct = self._direct()
		_, unmatched = self._direct()
		_, archived = self._direct()
		gx = self._gx_customer("active", current_period_end=add_months(nowdate(), 1))[2]
		host = site_sync.host_of(direct.site_url)
		old = site_sync.host_of(archived.site_url)
		site_sync.report_platform_sites(_platform({"site": host}, {"site": old}))
		frappe.db.set_value("GX Tenant", archived.name, "status", "archived")
		frappe.db.set_value("GX Tenant", gx.name, "platform_site", site_sync.host_of(gx.site_url))
		result = site_sync.get_site_states()
		self.assertEqual([r["site"] for r in result["sites"]], [host])
		self.assertEqual(result["max_suspensions_per_run"], 1)
		self.assertTrue(result["enabled"])
		unmatched.reload()
		self.assertFalse(unmatched.platform_site)

	def test_desired_state_follows_tenant_status(self):
		sub, tenant = self._direct("active")
		host = self._on_platform(tenant)
		row = self._states()[host]
		self.assertEqual(
			row,
			{
				"site": host,
				"desired_state": "Active",
				"billing_status": "active",
				"overdue_invoice": sub.name,
				"suspended_by_billing": 0,
			},
		)
		frappe.db.set_value("GX Subscription", sub.name, {"status": "past_due"})
		renewals.suspend_subscription(sub.name)
		row = self._states()[host]
		self.assertEqual((row["desired_state"], row["suspended_by_billing"]), ("Suspended", 1))
		# an admin's suspension is also Suspended, but not by billing
		frappe.db.set_value("GX Subscription", sub.name, "suspended_by", "admin")
		row = self._states()[host]
		self.assertEqual((row["desired_state"], row["suspended_by_billing"]), ("Suspended", 0))
		frappe.db.set_value("GX Tenant", tenant.name, "status", "ready")
		self.assertEqual(self._states()[host]["desired_state"], "Active")
		settings = frappe.get_single("GX Billing Settings")
		settings.suspend_sites = 0
		settings.sync_max_suspensions_per_run = 3
		settings.save()
		result = site_sync.get_site_states()
		self.assertEqual((result["enabled"], result["max_suspensions_per_run"]), (False, 3))

	def test_report_updates_tenant_sync_fields(self):
		_, tenant = self._direct("active")
		host = self._on_platform(tenant)
		_, gone = self._direct("active")
		frappe.db.set_value("GX Tenant", gone.name, "platform_site", "missing.example.com")
		out = site_sync.report_site_states(
			json.dumps(
				[
					{
						"site": host.upper(),
						"platform_status": "Suspended",
						"action": "suspended",
						"message": "unpaid",
					},
					{
						"site": "missing.example.com",
						"platform_status": "Not found",
						"action": "",
						"message": "",
					},
					{"site": "unknown.example.com", "platform_status": "Active", "action": ""},
					{"site": host, "platform_status": "Active", "action": "bogus"},
				]
			)
		)
		self.assertEqual(out, {"updated": 2})
		tenant.reload()
		gone.reload()
		self.assertEqual((tenant.sync_status, tenant.sync_message), ("suspended", "unpaid"))
		self.assertTrue(tenant.sync_checked_at)
		self.assertEqual(tenant.status, "ready")  # a report never changes the tenant's own status
		self.assertEqual(gone.sync_status, "Not found")

	def test_platform_sites_match_by_name_then_custom_domain(self):
		self.assertIsNone(site_sync.match_platform_site("a.example.com"))  # no list yet
		_, by_name = self._direct()
		_, by_domain = self._direct()
		_, unknown = self._direct()
		frappe.db.set_value("GX Tenant", by_domain.name, "site_url", "https://ERP.Customer.com:8443/app")
		stored = site_sync.report_platform_sites(
			_platform(
				{"site": site_sync.host_of(by_name.site_url).upper(), "domains": []},
				{"site": "hosted.tbo.example", "domains": ["erp.customer.com"]},
				# a site named like another's custom domain: the exact name wins
				{"site": "other.tbo.example", "domains": [site_sync.host_of(by_name.site_url)]},
			)
		)
		self.assertEqual(stored, {"stored": 3, "matched": 2})
		for tenant, expected in (
			(by_name, site_sync.host_of(by_name.site_url).upper()),
			(by_domain, "hosted.tbo.example"),
			(unknown, None),
		):
			tenant.reload()
			self.assertEqual(tenant.platform_site or None, expected)
		self.assertEqual(site_sync.match_platform_site("ERP.customer.com"), "hosted.tbo.example")
		self.assertIsNone(site_sync.match_platform_site("customer.com"))  # never partial
		self.assertIsNone(site_sync.match_platform_site("x.erp.customer.com"))

	def test_an_empty_list_is_ignored(self):
		_, tenant = self._direct()
		host = self._on_platform(tenant)
		out = site_sync.report_platform_sites("[]")
		self.assertEqual(out, {"stored": 0, "matched": 0, "ignored": True})
		self.assertEqual(site_sync.match_platform_site(host), host)
		tenant.reload()
		self.assertEqual(tenant.platform_site, host)
		self.assertIn(host, self._states())

	def test_a_shrunk_list_keeps_a_suspended_tenant_listed(self):
		sub, tenant = self._direct()
		host = self._on_platform(tenant)
		renewals.suspend_subscription(sub.name)
		_, other = self._direct()
		site_sync.report_platform_sites(_platform({"site": site_sync.host_of(other.site_url)}))
		tenant.reload()
		self.assertEqual((tenant.status, tenant.platform_site), ("suspended", host))
		self.assertFalse(tenant.sync_status)
		self.assertEqual(self._states()[host]["desired_state"], "Suspended")

	def test_a_shrunk_list_marks_a_ready_tenant_not_on_platform(self):
		_, tenant = self._direct()
		_, other = self._direct()
		host = self._on_platform(tenant)
		other_host = site_sync.host_of(other.site_url)
		site_sync.report_platform_sites(_platform({"site": other_host}))
		tenant.reload()
		self.assertEqual((tenant.platform_site, tenant.sync_status), (None, "Not on platform"))
		self.assertTrue(tenant.sync_message)
		self.assertNotIn(host, self._states())
		# it comes back with the list: the mark is lifted
		site_sync.report_platform_sites(_platform({"site": other_host}, {"site": host}))
		tenant.reload()
		self.assertEqual((tenant.platform_site, tenant.sync_status), (host, None))

	def test_role_required(self):
		self.assertEqual(
			frappe.get_all("Role", filters={"name": site_sync.SYNC_ROLE}, pluck="name"), [site_sync.SYNC_ROLE]
		)
		account, _, _ = self._direct_customer("active")
		calls = (
			lambda: site_sync.get_site_states(),
			lambda: site_sync.report_site_states("[]"),
			lambda: site_sync.report_platform_sites("[]"),
		)
		for user in ("Guest", account.user):
			frappe.set_user(user)
			for call in calls:
				with self.assertRaises(frappe.PermissionError):
					call()
		frappe.set_user("Administrator")
		self.assertEqual(site_sync.report_site_states("[]"), {"updated": 0})

	def test_role_holder_is_allowed(self):
		email = f"sync-{frappe.generate_hash(length=6)}@example.com"
		user = frappe.get_doc(
			{"doctype": "User", "email": email, "first_name": "Sync", "send_welcome_email": 0}
		).insert()
		user.add_roles(site_sync.SYNC_ROLE)
		frappe.set_user(user.name)
		self.assertIn("sites", site_sync.get_site_states())


class TestAddDirectSiteMatching(direct_tests.DirectCase):
	def test_add_direct_site_warns_when_not_on_platform_list(self):
		status, body = self._add()
		self.assertNotIn("not_on_platform_list", body["warnings"])  # no list known: nothing to compare with
		site_sync.report_platform_sites(json.dumps([{"site": "other.example.com", "domains": []}]))
		status, body = self._add(site="beta.example.com", email="beta@example.com", phone="9811111111")
		self.assertEqual(status, 201, body)
		self.assertIn("not_on_platform_list", body["warnings"])
		self.assertFalse(self._tenant(body).platform_site)
		site_sync.report_platform_sites(json.dumps([{"site": "gamma.example.com", "domains": []}]))
		status, body = self._add(site="gamma.example.com", email="gamma@example.com", phone="9822222222")
		self.assertNotIn("not_on_platform_list", body["warnings"])
		self.assertEqual(self._tenant(body).platform_site, "gamma.example.com")
