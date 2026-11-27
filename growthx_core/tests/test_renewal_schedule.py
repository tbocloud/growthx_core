import unittest
from datetime import date

from growthx_core.billing.renewal_schedule import (
	final_stage,
	format_sent,
	paid_stages,
	parse_days,
	parse_sent,
	stage_to_send,
	suspension_day,
	trial_stages,
)

S = [-7, -3, -1, 0, 1, 3, 5]
D0 = date(2026, 12, 26)


class RenewalScheduleTest(unittest.TestCase):
	def test_stage_to_send_sends_only_the_latest_due_stage(self):
		self.assertEqual(stage_to_send(S, -7, set()), -7)
		self.assertIsNone(stage_to_send(S, -5, {-7}))
		self.assertEqual(stage_to_send(S, -2, {-7}), -3)
		self.assertEqual(stage_to_send(S, 9, set()), 5)
		self.assertIsNone(stage_to_send(S, -8, set()))

	def test_suspension_day_needs_a_final_warning(self):
		self.assertIsNone(suspension_day(D0, 7, 5, None, None))
		self.assertEqual(suspension_day(D0, 7, 5, date(2026, 12, 31), None), date(2027, 1, 2))
		# a late final warning gives 2 days' notice
		self.assertEqual(suspension_day(D0, 7, 5, date(2027, 1, 5), None), date(2027, 1, 7))
		self.assertEqual(suspension_day(D0, 7, 5, date(2026, 12, 31), date(2027, 1, 10)), date(2027, 1, 10))

	def test_paid_stages_yearly_adds_30(self):
		self.assertEqual(paid_stages([7, 3, 1], [1, 3, 5], [30], True)[0], -30)

	def test_paid_stages_monthly_has_no_yearly_stage(self):
		self.assertEqual(paid_stages([7, 3, 1], [1, 3, 5], [30], False), S)

	def test_trial_stages(self):
		self.assertEqual(trial_stages([3, 1]), [-3, -1, 0])

	def test_final_stage_is_the_largest_warning_day(self):
		self.assertEqual(final_stage([1, 3, 5]), 5)

	def test_parse_days_ignores_junk(self):
		self.assertEqual(parse_days("7, x,3,,1,-2"), [1, 3, 7])

	def test_parse_days_handles_none_and_empty(self):
		self.assertEqual(parse_days(None), [])
		self.assertEqual(parse_days(""), [])

	def test_parse_sent_and_format_sent_round_trip(self):
		self.assertEqual(parse_sent("-7,-3, 0"), {-7, -3, 0})
		self.assertEqual(format_sent([3, -7, 0, 3]), "-7,0,3")
		self.assertEqual(parse_sent(format_sent(S)), set(S))
		self.assertEqual(parse_sent(None), set())
