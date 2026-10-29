from datetime import timedelta

import frappe
from frappe.utils import get_datetime, now_datetime

import growthx_core
from growthx_core.api.v1._core import RateLimit, endpoint, iso

STUCK_AFTER = timedelta(minutes=45)
# resume_pending runs every minute; a few missed runs means the scheduler isn't running.
SCHEDULER_LATE_AFTER = timedelta(minutes=5)
RESUME_PENDING = "growthx_core.provisioning.runner.resume_pending"
JOB_PREFIX = "gx-provision::"  # runner.enqueue
FAILED_JOBS_WINDOW = timedelta(hours=24)


@endpoint(methods=("GET",), auth=False)
def ping():
	"""Liveness check for web, mobile and monitoring. Returns no tenant or user data."""
	return {"status": "ok", "version": growthx_core.__version__}


@endpoint(methods=("GET",), auth=False, rate_limit=RateLimit(60, 60))
def provisioning():
	"""For DevOps' uptime alert: 200 when site creation is healthy, 503 with `problems` when not.
	Counts only; no tenant or user data."""
	now = now_datetime()
	sites = {"failed": frappe.db.count("GX Tenant", {"status": "failed"}), "stuck": _stuck(now)}
	jobs = _jobs(now)
	last_run = frappe.db.get_value("Scheduled Job Type", {"method": RESUME_PENDING}, "last_execution")
	scheduler = {
		"active": not _scheduler_inactive(),
		"lastRun": iso(last_run) if last_run else None,
	}

	problems = []
	if sites["failed"]:
		problems.append("failed_sites")
	if sites["stuck"]:
		problems.append("stuck_sites")
	if jobs is None:
		problems.append("queue_unreachable")
	elif not jobs["workers"]:
		problems.append("no_workers")
	if not scheduler["active"] or not last_run or now - get_datetime(last_run) > SCHEDULER_LATE_AFTER:
		problems.append("scheduler_not_running")

	result = {
		"status": "alert" if problems else "ok",
		"problems": problems,
		"sites": sites,
		"jobs": jobs,
		"scheduler": scheduler,
		"checkedAt": iso(now),
	}
	return (503 if problems else 200), result


def _stuck(now) -> int:
	"""Sites not ready 45 minutes after their current attempt started (a retry starts a new one)."""
	cutoff = now - STUCK_AFTER
	count = 0
	for t in frappe.get_all(
		"GX Tenant",
		filters={"status": ("in", ["requested", "provisioning"])},
		fields=["name", "status", "attempts", "modified"],
	):
		if t.status == "requested":
			# Saved when requested or retried; the job changes it to provisioning within a minute.
			started = t.modified
		else:
			started = min(
				frappe.get_all(
					"GX Provisioning Step",
					filters={
						"parent": t.name,
						"parenttype": "GX Tenant",
						"attempt": t.attempts,
						"started_at": ("is", "set"),
					},
					pluck="started_at",
				),
				default=None,
			)
		if started and get_datetime(started) < cutoff:
			count += 1
	return count


def _jobs(now) -> dict | None:
	"""This site's provisioning jobs on the long queue, or None if Redis can't be reached."""
	from frappe.utils.background_jobs import get_queue, get_workers
	from rq.job import Job

	prefix = f"{frappe.local.site}||{JOB_PREFIX}"
	try:
		queue = get_queue("long")
		mine = lambda ids: [i for i in ids if i.startswith(prefix)]  # noqa: E731
		failed_ids = mine(queue.failed_job_registry.get_job_ids())
		since = now - FAILED_JOBS_WINDOW
		failed = [
			job
			for job in Job.fetch_many(failed_ids, connection=queue.connection)
			if job and job.ended_at and _local(job.ended_at) >= since
		]
		return {
			"queued": len(mine(queue.get_job_ids())),
			"running": len(mine(queue.started_job_registry.get_job_ids())),
			"failedLast24h": len(failed),
			"workers": len(get_workers(queue)),
		}
	except Exception:
		frappe.log_error(title="GrowthX health: job queue unreachable")
		return None


def _local(utc_dt):
	"""RQ times are naive UTC; Frappe's are naive in the system timezone."""
	from frappe.utils import convert_utc_to_system_timezone

	return convert_utc_to_system_timezone(utc_dt).replace(tzinfo=None)


def _scheduler_inactive() -> bool:
	from frappe.utils.scheduler import is_scheduler_inactive

	return is_scheduler_inactive(verbose=False)
