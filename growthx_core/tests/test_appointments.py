import json
from datetime import datetime
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from growthx_core.api.v1 import _core, appointments, auth

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


class TestAppointments(IntegrationTestCase):
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
		self.assertEqual(_json(self._book("2030-01-07T04:30:00", app=None))["error"]["fields"].keys(), {"start"})
		bad_phone = self._book("2030-01-07T04:30:00Z", phone="12345")
		self.assertEqual(bad_phone.status_code, 422)
		self.assertIn("phone", _json(bad_phone)["error"]["fields"])
		self.assertEqual(self._book("2030-01-07T04:30:00Z", phone="+91 98765-43210").status_code, 201)


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
