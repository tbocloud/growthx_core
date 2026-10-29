"""health.provisioning: the monitoring endpoint DevOps' uptime alert calls."""

import json
from datetime import timedelta
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import now_datetime

from growthx_core.api.v1 import health

HEALTHY_JOBS = {"queued": 0, "running": 0, "failedLast24h": 0, "workers": 1}


def _call():
	frappe.local.form_dict = frappe._dict()
	frappe.local.request = None
	frappe.local.request_ip = "127.0.0.1"
	response = health.provisioning()
	return response.status_code, json.loads(response.get_data())


class TestProvisioningHealth(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		# Only this test's tenants count (rolled back in tearDown).
		frappe.db.delete("GX Tenant", {"status": ("in", ["requested", "provisioning", "failed"])})
		self._last_run(minutes=1)
		self.jobs = patch.object(health, "_jobs", return_value=dict(HEALTHY_JOBS))
		self.jobs.start()

	def tearDown(self):
		self.jobs.stop()
		frappe.db.rollback()

	def _last_run(self, minutes):
		name = frappe.db.get_value("Scheduled Job Type", {"method": health.RESUME_PENDING})
		self.assertTrue(name, "resume_pending isn't a scheduled job")
		frappe.db.set_value(
			"Scheduled Job Type", name, "last_execution", now_datetime() - timedelta(minutes=minutes)
		)

	def _tenant(self, status, attempts=1, steps=(), modified_minutes_ago=0):
		tenant = frappe.get_doc(
			{
				"doctype": "GX Tenant",
				"status": status,
				"attempts": attempts,
				"subdomain": frappe.generate_hash(length=10),
			}
		)
		for attempt, minutes in steps:
			tenant.append(
				"steps",
				{
					"step_key": "create_site",
					"state": "running",
					"attempt": attempt,
					"started_at": now_datetime() - timedelta(minutes=minutes),
				},
			)
		tenant.insert(ignore_mandatory=True)
		if modified_minutes_ago:
			frappe.db.set_value(
				"GX Tenant",
				tenant.name,
				"modified",
				now_datetime() - timedelta(minutes=modified_minutes_ago),
				update_modified=False,
			)
		return tenant

	def test_healthy(self):
		with patch.object(health, "_scheduler_inactive", return_value=False):
			status, body = _call()
		self.assertEqual(status, 200, body)
		self.assertEqual((body["status"], body["problems"]), ("ok", []))
		self.assertEqual(body["sites"], {"failed": 0, "stuck": 0})
		self.assertEqual(body["jobs"], HEALTHY_JOBS)

	def test_failed_site(self):
		self._tenant("failed")
		with patch.object(health, "_scheduler_inactive", return_value=False):
			status, body = _call()
		self.assertEqual((status, body["problems"], body["sites"]["failed"]), (503, ["failed_sites"], 1))

	def test_stuck_is_counted_from_the_current_attempt(self):
		self._tenant("provisioning", attempts=1, steps=[(1, 50)])  # stuck
		self._tenant("provisioning", attempts=1, steps=[(1, 10)])  # still within 45 minutes
		self._tenant("provisioning", attempts=2, steps=[(1, 120), (2, 5)])  # retried 5 minutes ago
		self._tenant("requested", modified_minutes_ago=60)  # never picked up
		self._tenant("requested")
		with patch.object(health, "_scheduler_inactive", return_value=False):
			status, body = _call()
		self.assertEqual((status, body["sites"]["stuck"], body["problems"]), (503, 2, ["stuck_sites"]))

	def test_no_workers_or_queue_down(self):
		for jobs, problem in (({**HEALTHY_JOBS, "workers": 0}, "no_workers"), (None, "queue_unreachable")):
			with (
				patch.object(health, "_jobs", return_value=jobs),
				patch.object(health, "_scheduler_inactive", return_value=False),
			):
				status, body = _call()
			self.assertEqual((status, body["problems"]), (503, [problem]))

	def test_scheduler_not_running(self):
		self._last_run(minutes=10)
		with patch.object(health, "_scheduler_inactive", return_value=False):
			status, body = _call()
		self.assertEqual((status, body["problems"]), (503, ["scheduler_not_running"]))

		self._last_run(minutes=1)
		with patch.object(health, "_scheduler_inactive", return_value=True):
			status, body = _call()
		self.assertEqual((status, body["problems"]), (503, ["scheduler_not_running"]))

	def test_jobs_reads_the_long_queue(self):
		self.jobs.stop()
		try:
			jobs = health._jobs(now_datetime())
		finally:
			self.jobs.start()
		self.assertEqual(set(jobs), set(HEALTHY_JOBS))
