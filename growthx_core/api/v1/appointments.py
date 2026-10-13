"""'Talk to an expert' bookings from the website (public, no login).

Opening hours, call length, working days, holidays, experts and the video-link template are in
GX Appointment Settings. Each start time can be booked once per expert (once in total when no
experts are listed). Times are stored in the site's timezone and returned as ISO 8601 UTC.
"""

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import frappe
from frappe.utils import cint, get_system_timezone, get_time, now_datetime

from growthx_core.api.v1._core import (
	ApiError,
	RateLimit,
	body,
	client_ip,
	endpoint,
	get_email,
	get_str,
	iso,
	query,
	validation_error,
)

MAX_DAYS = 60
DEFAULT_DAYS = 28
PHONE_RE = re.compile(r"^(\+91)?[6-9]\d{9}$")
SLOT_TAKEN = "That time was just booked. Please choose another."


# ---------------------------------------------------------------- endpoints


@endpoint(methods=("GET",), auth=False, rate_limit=RateLimit(60, 60))
def slots():
	raw_days = query().get("days")
	days = cint(raw_days) if raw_days not in (None, "") else DEFAULT_DAYS
	if not 1 <= days <= MAX_DAYS:
		raise validation_error({"days": f"days must be between 1 and {MAX_DAYS}."})

	schedule = Schedule()
	return {
		"timezone": get_system_timezone(),
		"durationMinutes": schedule.duration_minutes,
		"slots": [iso(start) for start in schedule.free_slots(days)],
	}


@endpoint(methods=("POST",), auth=False, rate_limit=RateLimit(5, 60 * 60), idempotent=True)
def book():
	data = body()
	start = _parse_start(get_str(data, "start", required=True, max_length=40, label="Start"))
	full_name = get_str(data, "name", required=True, label="Name")
	email = get_email(data)
	phone = _phone(data)
	company = get_str(data, "company", label="Company")
	app = get_str(data, "app", max_length=40, label="App")
	if app and not frappe.db.exists("GX Product", app):
		raise validation_error({"app": "Unknown app."})
	notes = get_str(data, "notes", max_length=2000, label="Notes")

	schedule = Schedule()
	# One booking at a time, so two visitors can't take the last place in a slot.
	with frappe.cache.lock(frappe.cache.make_key("gx:appointments:book"), timeout=30, blocking_timeout=15):
		if not schedule.is_offered(start):
			raise ApiError(409, "slot_taken", SLOT_TAKEN)
		expert = schedule.pick_expert(start)
		if expert is False:
			raise ApiError(409, "slot_taken", SLOT_TAKEN)
		appointment = frappe.get_doc(
			{
				"doctype": "GX Appointment",
				"full_name": full_name,
				"email": email,
				"phone": phone,
				"company": company,
				"app": app,
				"notes": notes,
				"start": start,
				"end": start + schedule.duration,
				"expert": expert or None,
				"status": "Confirmed",
				"ip_address": client_ip(),
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()  # visible to the next booking before the lock is released

	if schedule.meeting_url_template:
		appointment.db_set("meeting_url", schedule.meeting_url_template.replace("{id}", appointment.name))
	_record_in_crm(appointment)
	_send_confirmation(appointment)

	return 201, {
		"id": appointment.name,
		"status": "confirmed",
		"start": iso(appointment.start),
		"end": iso(appointment.end),
	}


# ---------------------------------------------------------------- schedule


class Schedule:
	def __init__(self):
		s = frappe.get_single("GX Appointment Settings")
		self.duration_minutes = cint(s.duration_minutes) or 30
		self.duration = timedelta(minutes=self.duration_minutes)
		self.day_start = get_time(s.day_start or "10:00:00")
		self.day_end = get_time(s.day_end or "18:00:00")
		self.working_days = {d.strip()[:3].title() for d in (s.working_days or "").split(",") if d.strip()}
		self.min_notice = timedelta(hours=cint(s.min_notice_hours))
		self.experts = s.expert_list()
		self.meeting_url_template = (s.meeting_url_template or "").strip()
		self.holidays = (
			set(
				frappe.get_all(
					"Holiday",
					filters={"parenttype": "Holiday List", "parent": s.holiday_list},
					pluck="holiday_date",
				)
			)
			if s.holiday_list
			else set()
		)

	@property
	def capacity(self) -> int:
		return max(len(self.experts), 1)

	def _day_slots(self, day):
		if day.strftime("%a") not in self.working_days or day in self.holidays:
			return
		t = datetime.combine(day, self.day_start)
		close = datetime.combine(day, self.day_end)
		while t + self.duration <= close:
			yield t
			t += self.duration

	def offered_slots(self, days: int):
		earliest = now_datetime() + self.min_notice
		today = now_datetime().date()
		for i in range(days):
			for start in self._day_slots(today + timedelta(days=i)):
				if start >= earliest:
					yield start

	def _bookings(self, starts) -> dict:
		"""{start: [expert, ...]} for confirmed bookings at the given start times."""
		if not starts:
			return {}
		rows = frappe.get_all(
			"GX Appointment",
			filters={"status": "Confirmed", "start": ("in", list(starts))},
			fields=["start", "expert"],
		)
		booked = {}
		for row in rows:
			booked.setdefault(row.start, []).append(row.expert)
		return booked

	def free_slots(self, days: int) -> list:
		offered = list(self.offered_slots(days))
		booked = self._bookings(offered)
		return [start for start in offered if len(booked.get(start, [])) < self.capacity]

	def is_offered(self, start) -> bool:
		if start < now_datetime() + self.min_notice or start.date() > now_datetime().date() + timedelta(
			days=MAX_DAYS
		):
			return False
		return start in set(self._day_slots(start.date()))

	def pick_expert(self, start):
		"""The least busy expert free at `start`; None when no experts are listed;
		False when the slot is full."""
		taken = self._bookings([start]).get(start, [])
		if not self.experts:
			return False if taken else None
		free = [e for e in self.experts if e not in taken]
		if not free:
			return False
		upcoming = frappe.get_all(
			"GX Appointment",
			filters={"status": "Confirmed", "start": (">=", now_datetime()), "expert": ("in", free)},
			pluck="expert",
		)
		return min(free, key=lambda e: (upcoming.count(e), self.experts.index(e)))


# ---------------------------------------------------------------- input


def _parse_start(value: str) -> datetime:
	"""ISO 8601 with a timezone (e.g. 2026-10-14T04:30:00Z) -> naive site-timezone datetime."""
	try:
		parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
	except ValueError:
		parsed = None
	if parsed is None or parsed.tzinfo is None:
		raise validation_error({"start": "Use an ISO 8601 UTC time, e.g. 2026-10-14T04:30:00Z."})
	return parsed.astimezone(ZoneInfo(get_system_timezone())).replace(tzinfo=None, microsecond=0)


def _phone(data: dict) -> str:
	phone = get_str(data, "phone", required=True, max_length=20, label="Phone")
	phone = re.sub(r"[\s-]", "", phone)
	if not PHONE_RE.match(phone):
		raise validation_error({"phone": "Enter a 10-digit Indian mobile number."})
	return phone


# ---------------------------------------------------------------- side effects


def _record_in_crm(appointment):
	"""Lead + Event in ERPNext so sales sees the booking. A failure here never fails the booking."""
	try:
		frappe.db.savepoint("gx_appointment_crm")
		lead = frappe.db.get_value("Lead", {"email_id": appointment.email})
		if not lead:
			lead = (
				frappe.get_doc(
					{
						"doctype": "Lead",
						"first_name": appointment.full_name,
						"email_id": appointment.email,
						"mobile_no": appointment.phone,
						"company_name": appointment.company,
					}
				)
				.insert(ignore_permissions=True)
				.name
			)
		details = [
			f"Phone: {appointment.phone}",
			f"Company: {appointment.company or '-'}",
			f"App: {appointment.app or '-'}",
			f"Expert: {appointment.expert or '-'}",
			f"Meeting link: {appointment.meeting_url or '-'}",
			f"Notes: {appointment.notes or '-'}",
		]
		event = frappe.get_doc(
			{
				"doctype": "Event",
				"subject": f"GrowthX call: {appointment.full_name}"
				+ (f" ({appointment.company})" if appointment.company else ""),
				"starts_on": appointment.start,
				"ends_on": appointment.end,
				"event_type": "Private",
				"event_category": "Meeting",
				"description": "<br>".join(frappe.utils.escape_html(line) for line in details),
				"event_participants": [{"reference_doctype": "Lead", "reference_docname": lead}],
			}
		).insert(ignore_permissions=True)
		appointment.db_set({"lead": lead, "event": event.name})
	except Exception:
		frappe.db.rollback(save_point="gx_appointment_crm")
		frappe.log_error(title=f"GrowthX: could not record appointment {appointment.name} in CRM")


def _ics(appointment) -> str:
	def utc(value):
		return iso(value).replace("-", "").replace(":", "")

	def text(value):
		return (value or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")

	lines = [
		"BEGIN:VCALENDAR",
		"VERSION:2.0",
		"PRODID:-//GrowthX//Appointments//EN",
		"METHOD:PUBLISH",
		"BEGIN:VEVENT",
		f"UID:{appointment.name}@growthx",
		f"DTSTAMP:{utc(now_datetime())}",
		f"DTSTART:{utc(appointment.start)}",
		f"DTEND:{utc(appointment.end)}",
		f"SUMMARY:{text('GrowthX: talk to an expert')}",
		f"DESCRIPTION:{text('Join: ' + appointment.meeting_url if appointment.meeting_url else 'We will email you the video link before the call.')}",
	]
	if appointment.meeting_url:
		lines.append(f"LOCATION:{text(appointment.meeting_url)}")
	lines += ["END:VEVENT", "END:VCALENDAR"]
	return "\r\n".join(lines) + "\r\n"


def _send_confirmation(appointment):
	"""Email the visitor (copy to the expert) with a calendar invite. Never fails the booking."""
	# `start` is already in the site's timezone
	when = appointment.start.strftime("%A %d %B %Y, %I:%M %p") + f" ({get_system_timezone()})"
	link = (
		f'<p><a href="{appointment.meeting_url}">Join the video call</a></p>'
		if appointment.meeting_url
		else "<p>We'll email you the video-call link before the call.</p>"
	)
	try:
		frappe.sendmail(
			recipients=[appointment.email],
			cc=[appointment.expert] if appointment.expert else None,
			subject="Your GrowthX call is booked",
			message=(
				f"<p>Hi {frappe.utils.escape_html(appointment.full_name)},</p>"
				f"<p>Your call with a GrowthX expert is booked for <b>{when}</b>.</p>"
				f"{link}<p>The calendar invite is attached.</p>"
			),
			attachments=[{"fname": "growthx-call.ics", "fcontent": _ics(appointment)}],
		)
	except Exception:
		frappe.log_error(title=f"GrowthX: could not send booking confirmation for {appointment.name}")
