"""'Talk to an expert' bookings from the website (public, no login).

Opening hours, call length, working days, holidays, experts and the video-link template are in
GX Appointment Settings. Each start time can be booked once per expert (once in total when no
experts are listed). Times are stored in the site's timezone and returned as ISO 8601 UTC.

Workflow: a booking is confirmed at once (slots come from the experts' real availability). The
visitor gets a private manage link to cancel or reschedule; the team works the booking in
admin.appointments / admin.update_appointment (reschedule, reassign, cancel, completed, no-show,
internal note). Every change is audit-logged and emailed; reminders go out 24 h and 1 h before.
"""

import hashlib
import re
import secrets
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import frappe
from frappe.utils import cint, get_system_timezone, get_time, now_datetime

from growthx_core.api.v1._core import (
	ApiError,
	RateLimit,
	audit,
	body,
	client_ip,
	endpoint,
	get_email,
	get_str,
	iso,
	not_found,
	query,
	validation_error,
	web_url,
)

MAX_DAYS = 60
DEFAULT_DAYS = 28
PHONE_RE = re.compile(r"^(\+91)?[6-9]\d{9}$")
SLOT_TAKEN = "That time was just booked. Please choose another."
STATUS = {"Confirmed": "confirmed", "Completed": "completed", "Cancelled": "cancelled", "No Show": "no_show"}


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
	token = secrets.token_urlsafe(24)
	# One booking at a time, so two visitors can't take the last place in a slot.
	with _booking_lock():
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
				"manage_token_hash": _hash(token),
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()  # visible to the next booking before the lock is released

	if schedule.meeting_url_template:
		appointment.db_set("meeting_url", schedule.meeting_url_template.replace("{id}", appointment.name))
	_record_in_crm(appointment)
	audit(
		"appointment.booked",
		"GX Appointment",
		appointment.name,
		f"By the visitor for {iso(appointment.start)}",
	)
	_notify(appointment, "booked", token=token)

	return 201, {
		"id": appointment.name,
		"status": "confirmed",
		"start": iso(appointment.start),
		"end": iso(appointment.end),
	}


@endpoint(methods=("GET",), auth=False, rate_limit=RateLimit(30, 60))
def manage():
	"""The visitor's own booking, from the private link in their emails."""
	return _public_view(_by_token(query()))


@endpoint(methods=("POST",), auth=False, rate_limit=RateLimit(10, 60 * 60))
def cancel():
	data = body()
	appointment = _by_token(data)
	change(
		appointment, "cancel", by="Visitor", reason=get_str(data, "reason", max_length=500, label="Reason")
	)
	return _public_view(appointment)


@endpoint(methods=("POST",), auth=False, rate_limit=RateLimit(10, 60 * 60))
def reschedule():
	data = body()
	appointment = _by_token(data)
	start = _parse_start(get_str(data, "start", required=True, max_length=40, label="Start"))
	change(appointment, "reschedule", by="Visitor", start=start)
	return _public_view(appointment)


def _by_token(data: dict):
	token = get_str(data, "token", required=True, max_length=100, label="Token")
	name = frappe.db.get_value("GX Appointment", {"manage_token_hash": _hash(token)})
	if not name:
		raise not_found()
	return frappe.get_doc("GX Appointment", name)


def _public_view(appointment) -> dict:
	"""What the visitor may see about their own booking."""
	changeable = appointment.status == "Confirmed" and appointment.start > now_datetime()
	return {
		"id": appointment.name,
		"status": STATUS[appointment.status],
		"start": iso(appointment.start),
		"end": iso(appointment.end),
		"name": appointment.full_name,
		"meetingUrl": appointment.meeting_url or None,
		"canChange": changeable,
	}


# ---------------------------------------------------------------- changes (admin and visitor)

ACTIONS = ("reschedule", "reassign", "cancel", "complete", "no_show", "note")


def change(appointment, action: str, by: str = "Team", start=None, expert=None, reason=None, note=None):
	"""Apply one workflow action. `by` is "Team" (admin) or "Visitor". Raises ApiError on a
	conflict; on success saves, audit-logs and emails the people affected."""
	if action not in ACTIONS:
		raise validation_error({"action": f"Must be one of: {', '.join(ACTIONS)}."})
	if action == "note":
		if not note:
			raise validation_error({"note": "Write a note."})
		stamp = f"{now_datetime():%d %b %Y %H:%M} · {frappe.session.user}"
		appointment.db_set(
			"admin_notes", "\n\n".join(filter(None, [appointment.admin_notes, f"[{stamp}] {note}"]))
		)
		audit("appointment.note", "GX Appointment", appointment.name, note)
		return appointment

	if appointment.status != "Confirmed":
		raise ApiError(
			409, "not_changeable", f"This call is already {STATUS[appointment.status].replace('_', ' ')}."
		)
	started = appointment.start <= now_datetime()

	if action in ("complete", "no_show"):
		if not started:
			raise ApiError(409, "not_started", "A call can be marked only after it has started.")
		appointment.db_set("status", "Completed" if action == "complete" else "No Show")
		audit(f"appointment.{action}", "GX Appointment", appointment.name, note)
		return appointment

	if started and by == "Visitor":
		raise ApiError(409, "not_changeable", "This call has already started. Please contact us.")

	if action == "cancel":
		appointment.db_set({"status": "Cancelled", "cancelled_by": by, "cancel_reason": reason})
		_update_event(appointment, cancelled=True)
		audit(
			"appointment.cancel",
			"GX Appointment",
			appointment.name,
			f"By {by.lower()}" + (f": {reason}" if reason else ""),
		)
		_notify(appointment, "cancelled", by=by)
		return appointment

	schedule = Schedule()
	if action == "reschedule":
		if start is None:
			raise validation_error({"start": "Choose a new time."})
		if start == appointment.start:
			return appointment
		with _booking_lock():
			if not schedule.is_offered(start):
				raise ApiError(409, "slot_taken", SLOT_TAKEN)
			# Keep the same expert when they're free at the new time.
			new_expert = schedule.pick_expert(start, exclude=appointment.name, prefer=appointment.expert)
			if new_expert is False:
				raise ApiError(409, "slot_taken", SLOT_TAKEN)
			previous = appointment.start
			appointment.db_set(
				{
					"start": start,
					"end": start + schedule.duration,
					"expert": new_expert or None,
					"reminder_24h_sent": 0,
					"reminder_1h_sent": 0,
				}
			)
			frappe.db.commit()
		_update_event(appointment)
		audit(
			"appointment.reschedule",
			"GX Appointment",
			appointment.name,
			f"By {by.lower()}: {iso(previous)} → {iso(start)}",
		)
		_notify(appointment, "rescheduled", by=by)
		return appointment

	# reassign
	if not expert or expert not in schedule.experts:
		raise validation_error({"expert": "Choose one of the experts in GX Appointment Settings."})
	if expert == appointment.expert:
		return appointment
	if expert in schedule.bookings([appointment.start], exclude=appointment.name).get(appointment.start, []):
		raise ApiError(409, "expert_busy", "That expert already has a call at this time.")
	previous = appointment.expert
	appointment.db_set("expert", expert)
	_update_event(appointment)
	audit("appointment.reassign", "GX Appointment", appointment.name, f"{previous or 'nobody'} → {expert}")
	_notify(appointment, "assigned")
	return appointment


def history(appointment) -> list[dict]:
	rows = frappe.get_all(
		"GX Audit Log",
		filters={"reference_doctype": "GX Appointment", "reference_name": appointment.name},
		fields=["creation", "actor", "action", "details"],
		order_by="creation asc",
	)
	return [
		{
			"at": iso(r.creation),
			"actor": r.actor,
			"action": r.action.removeprefix("appointment."),
			"details": r.details,
		}
		for r in rows
	]


# ---------------------------------------------------------------- reminders


def send_reminders():
	"""Scheduler (every 5 minutes): remind visitor and expert 24 h and 1 h before the call."""
	now = now_datetime()
	for field, window, kind in (
		("reminder_24h_sent", timedelta(hours=24), "reminder_24h"),
		("reminder_1h_sent", timedelta(hours=1), "reminder_1h"),
	):
		due = frappe.get_all(
			"GX Appointment",
			filters={"status": "Confirmed", field: 0, "start": ("between", [now, now + window])},
			pluck="name",
		)
		for name in due:
			appointment = frappe.get_doc("GX Appointment", name)
			# A call booked 3 hours ahead gets only the 1-hour reminder.
			if kind == "reminder_24h" and appointment.creation > appointment.start - timedelta(hours=24):
				appointment.db_set(field, 1)
				continue
			_notify(appointment, kind)
			appointment.db_set(field, 1)
	frappe.db.commit()


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

	def bookings(self, starts, exclude=None) -> dict:
		"""{start: [expert, ...]} for confirmed bookings at the given start times."""
		if not starts:
			return {}
		filters = {"status": "Confirmed", "start": ("in", list(starts))}
		if exclude:
			filters["name"] = ("!=", exclude)
		rows = frappe.get_all("GX Appointment", filters=filters, fields=["start", "expert"])
		booked = {}
		for row in rows:
			booked.setdefault(row.start, []).append(row.expert)
		return booked

	def free_slots(self, days: int) -> list:
		offered = list(self.offered_slots(days))
		booked = self.bookings(offered)
		return [start for start in offered if len(booked.get(start, [])) < self.capacity]

	def is_offered(self, start) -> bool:
		if start < now_datetime() + self.min_notice or start.date() > now_datetime().date() + timedelta(
			days=MAX_DAYS
		):
			return False
		return start in set(self._day_slots(start.date()))

	def pick_expert(self, start, exclude=None, prefer=None):
		"""The least busy expert free at `start` (`prefer` if they are free); None when no
		experts are listed; False when the slot is full. `exclude` ignores one booking (the one
		being rescheduled)."""
		taken = self.bookings([start], exclude=exclude).get(start, [])
		if not self.experts:
			return False if taken else None
		free = [e for e in self.experts if e not in taken]
		if not free:
			return False
		if prefer in free:
			return prefer
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


def _hash(token: str) -> str:
	return hashlib.sha256(token.encode()).hexdigest()


def _booking_lock():
	return frappe.cache.lock(frappe.cache.make_key("gx:appointments:book"), timeout=30, blocking_timeout=15)


def _update_event(appointment, cancelled=False):
	"""Keep the ERPNext Event in step. A failure here never fails the change."""
	if not appointment.event:
		return
	try:
		values = (
			{"status": "Cancelled"}
			if cancelled
			else {"starts_on": appointment.start, "ends_on": appointment.end}
		)
		frappe.db.set_value("Event", appointment.event, values)
	except Exception:
		frappe.log_error(title=f"GrowthX: could not update the CRM event for {appointment.name}")


def _ics(appointment, cancelled=False) -> str:
	def utc(value):
		return iso(value).replace("-", "").replace(":", "")

	def text(value):
		return (
			(value or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")
		)

	# Calendar apps replace the event when the UID matches and SEQUENCE is higher.
	sequence = int(now_datetime().timestamp()) - 1_700_000_000
	lines = [
		"BEGIN:VCALENDAR",
		"VERSION:2.0",
		"PRODID:-//GrowthX//Appointments//EN",
		f"METHOD:{'CANCEL' if cancelled else 'PUBLISH'}",
		"BEGIN:VEVENT",
		f"UID:{appointment.name}@growthx",
		f"SEQUENCE:{sequence}",
		f"DTSTAMP:{utc(now_datetime())}",
		f"DTSTART:{utc(appointment.start)}",
		f"DTEND:{utc(appointment.end)}",
		f"SUMMARY:{text('GrowthX: talk to an expert')}",
		f"STATUS:{'CANCELLED' if cancelled else 'CONFIRMED'}",
		f"DESCRIPTION:{text('Join: ' + appointment.meeting_url if appointment.meeting_url else 'We will email you the video link before the call.')}",
	]
	if appointment.meeting_url:
		lines.append(f"LOCATION:{text(appointment.meeting_url)}")
	lines += ["END:VEVENT", "END:VCALENDAR"]
	return "\r\n".join(lines) + "\r\n"


SUBJECTS = {
	"booked": "Your GrowthX call is booked",
	"rescheduled": "Your GrowthX call has a new time",
	"cancelled": "Your GrowthX call is cancelled",
	"assigned": "Your GrowthX call",
	"reminder_24h": "Reminder: your GrowthX call is tomorrow",
	"reminder_1h": "Starting soon: your GrowthX call",
}


def _notify(appointment, kind: str, token: str | None = None, by: str = "Team"):
	"""Email the visitor (copy to the expert) about a booking change, with a calendar update.
	Never fails the request. The manage link is only in the first email: the token is not stored."""
	when = appointment.start.strftime("%A %d %B %Y, %I:%M %p") + f" ({get_system_timezone()})"
	name = frappe.utils.escape_html(appointment.full_name)
	join = (
		f'<p><a href="{appointment.meeting_url}">Join the video call</a></p>'
		if appointment.meeting_url
		else "<p>We'll email you the video-call link before the call.</p>"
	)
	manage = (
		f'<p>Need to change it? <a href="{web_url("/appointments/manage?token=" + token)}">Reschedule or cancel</a>.</p>'
		if token
		else ""
	)
	bodies = {
		"booked": f"<p>Your call with a GrowthX expert is booked for <b>{when}</b>.</p>{join}{manage}<p>The calendar invite is attached.</p>",
		"rescheduled": f"<p>Your GrowthX call is now on <b>{when}</b>.</p>{join}<p>The updated invite is attached.</p>",
		"cancelled": (
			"<p>Your GrowthX call has been cancelled as you asked.</p>"
			if by == "Visitor"
			else "<p>We're sorry — we had to cancel your GrowthX call. We'll be in touch to find a new time.</p>"
		)
		+ f'<p>You can <a href="{web_url("/talk-to-an-expert")}">book another time</a> whenever you like.</p>',
		"assigned": f"<p>Your call on <b>{when}</b> is now with {frappe.utils.escape_html(appointment.expert or 'a GrowthX expert')}.</p>{join}",
		"reminder_24h": f"<p>A reminder that your GrowthX call is tomorrow, <b>{when}</b>.</p>{join}",
		"reminder_1h": f"<p>Your GrowthX call starts at <b>{when}</b>.</p>{join}",
	}
	cancelled = kind == "cancelled"
	try:
		frappe.sendmail(
			recipients=[appointment.email],
			cc=[appointment.expert] if appointment.expert else None,
			subject=SUBJECTS[kind],
			message=f"<p>Hi {name},</p>{bodies[kind]}",
			attachments=None
			if kind.startswith("reminder")
			else [{"fname": "growthx-call.ics", "fcontent": _ics(appointment, cancelled=cancelled)}],
		)
	except Exception:
		frappe.log_error(title=f"GrowthX: could not send the {kind} email for {appointment.name}")
