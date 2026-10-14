import json
from datetime import datetime
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from growthx_core.api.v1 import _core, admin, appointments, auth

EMAIL_DOMAIN = "gx-appt-test.example"
# A Monday, far from real bookings. Site time (Asia/Kolkata in this app).
NOW = datetime(2030, 1, 7, 9, 0)
SETTINGS = {
	"duration_minutes": 30,
	"day_start": "10:00:00",
	"day_end": "12:00:00",
	"working_days": "Mon,Tue,Wed,Thu,Fri,Sat,Sun",
	"min_notice_hours": 0,
	"holiday_list": None,
	"experts": "",
	"meeting_url_template": "https://meet.example.com/growthx-{id}",
}


def _json(response):
	return json.loads(response.get_data())


def _call(fn, data=None):
	frappe.local.form_dict = frappe._dict(data or {})
	frappe.local.request_ip = "127.0.0.1"
	return fn()


class AppointmentTestCase(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		settings = frappe.get_single("GX Appointment Settings")
		self._saved = {key: settings.get(key) for key in SETTINGS}
		self._set(SETTINGS)
		patches = [
			patch.object(appointments, "now_datetime", return_value=NOW),
			patch.object(_core, "_check_rate_limit"),
			# Reading the Idempotency-Key header needs a real HTTP request
			patch.object(_core, "_idempotency_key", return_value=None),
			patch.object(_core, "get_system_timezone", return_value="Asia/Kolkata"),
			patch.object(appointments, "get_system_timezone", return_value="Asia/Kolkata"),
			patch("frappe.sendmail"),
		]
		for p in patches:
			p.start()
			self.addCleanup(p.stop)

	def tearDown(self):
		for name, lead, event in frappe.get_all(
			"GX Appointment",
			filters={"email": ("like", f"%@{EMAIL_DOMAIN}")},
			fields=["name", "lead", "event"],
			as_list=True,
		):
			frappe.delete_doc("GX Appointment", name, force=True, ignore_permissions=True)
			if event:
				frappe.delete_doc("Event", event, force=True, ignore_permissions=True)
			if lead:
				frappe.delete_doc("Lead", lead, force=True, ignore_permissions=True)
		self._set(self._saved)
		frappe.db.commit()

	def _set(self, values):
		settings = frappe.get_single("GX Appointment Settings")
		settings.update(values)
		settings.save(ignore_permissions=True)
		frappe.db.commit()

	def _slots(self, days=1):
		response = _call(appointments.slots, {"days": days})
		self.assertEqual(response.status_code, 200)
		return _json(response)

	def _book(self, start, who="riya", **extra):
		data = {"start": start, "name": who.title(), "email": f"{who}@{EMAIL_DOMAIN}", "phone": "9876543210"}
		data.update(extra)
		return _call(appointments.book, data)


class TestAppointments(AppointmentTestCase):
	def test_slots_follow_opening_hours_in_utc(self):
		body = self._slots()
		self.assertEqual(body["timezone"], "Asia/Kolkata")
		self.assertEqual(body["durationMinutes"], 30)
		# 10:00-12:00 IST in 30-minute calls = 04:30-06:00 UTC
		self.assertEqual(
			body["slots"],
			["2030-01-07T04:30:00Z", "2030-01-07T05:00:00Z", "2030-01-07T05:30:00Z", "2030-01-07T06:00:00Z"],
		)

	def test_minimum_notice_hides_close_slots(self):
		self._set({"min_notice_hours": 2})  # now is 09:00, so 11:00 is the first
		self.assertEqual(self._slots()["slots"], ["2030-01-07T05:30:00Z", "2030-01-07T06:00:00Z"])

	def test_closed_days_have_no_slots(self):
		self._set({"working_days": "Tue,Wed"})
		self.assertEqual(self._slots()["slots"], [])

	def test_days_out_of_range_is_422(self):
		response = _call(appointments.slots, {"days": 61})
		self.assertEqual(response.status_code, 422)
		self.assertIn("days", _json(response)["error"]["fields"])

	def test_book_then_slot_taken(self):
		response = self._book("2030-01-07T05:00:00Z", app=None, notes="Two branches")
		self.assertEqual(response.status_code, 201)
		body = _json(response)
		self.assertEqual(body["status"], "confirmed")
		self.assertEqual((body["start"], body["end"]), ("2030-01-07T05:00:00Z", "2030-01-07T05:30:00Z"))

		doc = frappe.get_doc("GX Appointment", body["id"])
		self.assertEqual(doc.start, datetime(2030, 1, 7, 10, 30))
		self.assertEqual(doc.meeting_url, f"https://meet.example.com/growthx-{doc.name}")
		self.assertTrue(doc.lead and doc.event)

		again = self._book("2030-01-07T05:00:00Z", who="second")
		self.assertEqual(again.status_code, 409)
		self.assertEqual(_json(again)["error"]["code"], "slot_taken")
		self.assertNotIn("2030-01-07T05:00:00Z", self._slots()["slots"])

	def test_each_expert_takes_one_booking_per_slot(self):
		self._set({"experts": f"a@{EMAIL_DOMAIN}\nb@{EMAIL_DOMAIN}"})
		first = self._book("2030-01-07T04:30:00Z", who="one")
		second = self._book("2030-01-07T04:30:00Z", who="two")
		third = self._book("2030-01-07T04:30:00Z", who="three")
		self.assertEqual((first.status_code, second.status_code, third.status_code), (201, 201, 409))
		experts = {frappe.db.get_value("GX Appointment", _json(r)["id"], "expert") for r in (first, second)}
		self.assertEqual(experts, {f"a@{EMAIL_DOMAIN}", f"b@{EMAIL_DOMAIN}"})

	def test_time_not_on_the_grid_is_slot_taken(self):
		self.assertEqual(self._book("2030-01-07T04:45:00Z").status_code, 409)

	def test_validation(self):
		self.assertEqual(_json(self._book("tomorrow"))["error"]["fields"].keys(), {"start"})
		self.assertEqual(
			_json(self._book("2030-01-07T04:30:00", app=None))["error"]["fields"].keys(), {"start"}
		)
		bad_phone = self._book("2030-01-07T04:30:00Z", phone="12345")
		self.assertEqual(bad_phone.status_code, 422)
		self.assertIn("phone", _json(bad_phone)["error"]["fields"])
		self.assertEqual(self._book("2030-01-07T04:30:00Z", phone="+91 98765-43210").status_code, 201)


class TestAppointmentWorkflow(AppointmentTestCase):
	"""Visitor manage link and the admin workflow (reschedule, reassign, cancel, complete, no-show, note)."""

	def setUp(self):
		super().setUp()
		self._set({"experts": f"a@{EMAIL_DOMAIN}\nb@{EMAIL_DOMAIN}"})
		for p in (
			patch.object(_core, "_require_auth"),  # admin endpoints: the caller is an admin
			patch.object(
				appointments.secrets,
				"token_urlsafe",
				side_effect=lambda n: f"tok-{frappe.generate_hash(length=8)}",
			),
		):
			p.start()
			self.addCleanup(p.stop)

	def tearDown(self):
		frappe.db.delete(
			"GX Audit Log", {"reference_doctype": "GX Appointment", "reference_name": ("in", self._ids())}
		)
		super().tearDown()

	def _ids(self):
		return frappe.get_all("GX Appointment", {"email": ("like", f"%@{EMAIL_DOMAIN}")}, pluck="name") or [
			""
		]

	def _booked(self, start="2030-01-07T05:00:00Z", who="riya"):
		with patch.object(appointments.secrets, "token_urlsafe", return_value=f"tok-{who}"):
			response = self._book(start, who=who)
		self.assertEqual(response.status_code, 201)
		frappe.db.commit()  # a real request commits when it ends; a later error response would roll back
		return _json(response)["id"], f"tok-{who}"

	def _admin(self, data):
		return _call(admin.update_appointment, data)

	def test_visitor_sees_and_reschedules_with_the_private_link(self):
		appointment_id, token = self._booked()
		view = _json(_call(appointments.manage, {"token": token}))
		self.assertEqual((view["id"], view["status"], view["canChange"]), (appointment_id, "confirmed", True))
		self.assertEqual(_call(appointments.manage, {"token": "wrong"}).status_code, 404)

		moved = _call(appointments.reschedule, {"token": token, "start": "2030-01-07T05:30:00Z"})
		self.assertEqual(moved.status_code, 200)
		self.assertEqual(_json(moved)["start"], "2030-01-07T05:30:00Z")
		doc = frappe.get_doc("GX Appointment", appointment_id)
		self.assertEqual(doc.start, datetime(2030, 1, 7, 11, 0))
		self.assertEqual(frappe.db.get_value("Event", doc.event, "starts_on"), datetime(2030, 1, 7, 11, 0))

	def test_visitor_cancels_and_the_slot_frees_up(self):
		self._set({"experts": ""})
		appointment_id, token = self._booked()
		self.assertNotIn("2030-01-07T05:00:00Z", self._slots()["slots"])
		cancelled = _json(_call(appointments.cancel, {"token": token, "reason": "Found a time conflict"}))
		self.assertEqual((cancelled["status"], cancelled["canChange"]), ("cancelled", False))
		self.assertIn("2030-01-07T05:00:00Z", self._slots()["slots"])
		self.assertEqual(frappe.db.get_value("GX Appointment", appointment_id, "cancelled_by"), "Visitor")
		self.assertEqual(_call(appointments.cancel, {"token": token}).status_code, 409)

	def test_admin_lists_and_filters(self):
		self._booked(who="riya")
		self._booked("2030-01-07T05:30:00Z", who="sam")
		listed = _json(_call(admin.appointments, {"from": "2030-01-07", "to": "2030-01-07", "q": "sam"}))
		self.assertEqual([i["name"] for i in listed["items"]], ["Sam"])
		self.assertEqual(listed["items"][0]["status"], "confirmed")
		self.assertEqual(_call(admin.appointments, {"from": "not-a-date"}).status_code, 422)

	def test_admin_reschedule_keeps_the_expert_when_free(self):
		appointment_id, _ = self._booked()
		expert = frappe.db.get_value("GX Appointment", appointment_id, "expert")
		body = _json(
			self._admin({"id": appointment_id, "action": "reschedule", "start": "2030-01-07T06:00:00Z"})
		)
		self.assertEqual((body["start"], body["expert"]), ("2030-01-07T06:00:00Z", expert))
		self.assertEqual([h["action"] for h in body["history"]], ["booked", "reschedule"])

	def test_admin_reassign_refuses_a_busy_expert(self):
		first, _ = self._booked(who="one")
		second, _ = self._booked(who="two")  # same slot, the other expert
		busy = frappe.db.get_value("GX Appointment", second, "expert")
		response = self._admin({"id": first, "action": "reassign", "expert": busy})
		self.assertEqual(response.status_code, 409)
		self.assertEqual(_json(response)["error"]["code"], "expert_busy")
		self.assertEqual(
			self._admin({"id": first, "action": "reassign", "expert": "x@nowhere.example"}).status_code, 422
		)

	def test_completed_and_no_show_only_after_the_call_starts(self):
		appointment_id, _ = self._booked()
		early = self._admin({"id": appointment_id, "action": "complete"})
		self.assertEqual(_json(early)["error"]["code"], "not_started")
		with patch.object(appointments, "now_datetime", return_value=datetime(2030, 1, 7, 10, 45)):
			done = _json(self._admin({"id": appointment_id, "action": "no_show"}))
		self.assertEqual(done["status"], "no_show")
		self.assertEqual(self._admin({"id": appointment_id, "action": "cancel"}).status_code, 409)

	def test_internal_notes_are_appended_and_never_shown_to_the_visitor(self):
		appointment_id, token = self._booked()
		self._admin({"id": appointment_id, "action": "note", "note": "Wants a Solar demo"})
		body = _json(self._admin({"id": appointment_id, "action": "note", "note": "Two branches"}))
		self.assertIn("Wants a Solar demo", body["adminNotes"])
		self.assertIn("Two branches", body["adminNotes"])
		self.assertNotIn("adminNotes", _json(_call(appointments.manage, {"token": token})))

	def test_reminders_go_out_once(self):
		appointment_id, _ = self._booked()
		# Booked at 09:00 for 10:30 the same day: only the 1-hour reminder applies.
		frappe.db.set_value("GX Appointment", appointment_id, "creation", NOW, update_modified=False)
		with (
			patch.object(appointments, "now_datetime", return_value=datetime(2030, 1, 7, 9, 45)),
			patch.object(appointments, "_notify") as notify,
		):
			appointments.send_reminders()
			appointments.send_reminders()
		self.assertEqual([c.args[1] for c in notify.call_args_list], ["reminder_1h"])


class TestForgotPassword(IntegrationTestCase):
	def test_returns_202_when_email_cannot_be_sent(self):
		with (
			patch.object(_core, "_check_rate_limit"),
			patch.object(auth, "_growthx_user", return_value="Administrator"),
			patch("frappe.sendmail", side_effect=frappe.OutgoingEmailError("no outgoing account")),
		):
			response = _call(auth.forgot_password, {"email": f"someone@{EMAIL_DOMAIN}"})
		self.assertEqual(response.status_code, 202)
		self.assertEqual(_json(response), {"ok": True})
