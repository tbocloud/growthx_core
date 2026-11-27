"""Date rules for renewals: which reminder is due, and when a subscription may be suspended.

Days are counted from a renewal date (the subscription's current period end, or the trial's end): -7 is a
week before it, 0 the day itself, 5 five days after. Standard library only, so these rules can be unit-tested
without Frappe. The hourly job calls them with the day's offset and the stages already sent.
"""

import re
from datetime import date, timedelta


def parse_days(text: str | None) -> list[int]:
	"""``"7, 3,1"`` → ``[1, 3, 7]``: positive whole numbers, sorted and unique; anything else is ignored."""
	days = set()
	for part in re.split(r"[\s,]+", text or ""):
		if re.fullmatch(r"\d+", part) and int(part) > 0:
			days.add(int(part))
	return sorted(days)


def paid_stages(
	reminder_days: list[int], warning_days: list[int], yearly_extra: list[int], yearly: bool
) -> list[int]:
	"""The offsets at which a paid subscription may be reminded: each reminder day before the renewal (negative),
	the renewal day itself (0), each final warning day after it, and for a yearly plan the yearly extra days
	before it. ``[7, 3, 1]``, ``[1, 3, 5]`` gives ``[-7, -3, -1, 0, 1, 3, 5]``."""
	stages = {-d for d in reminder_days} | {0} | set(warning_days)
	if yearly:
		stages |= {-d for d in yearly_extra}
	return sorted(stages)


def trial_stages(trial_reminder_days: list[int]) -> list[int]:
	"""The offsets for a trial: each reminder day before the trial ends, and the end day itself."""
	return sorted({-d for d in trial_reminder_days} | {0})


def stage_to_send(stages: list[int], offset: int, sent: set[int]) -> int | None:
	"""The latest stage at or before ``offset``, or None if there is none or it was already sent.

	Only the latest due stage is sent, so a customer never gets a backlog of reminders at once (for example
	when the feature is first switched on, or after the scheduler was down). Earlier stages count as skipped."""
	due = sorted(s for s in stages if s <= offset)
	if not due or due[-1] in sent:
		return None
	return due[-1]


def final_stage(warning_days: list[int]) -> int:
	"""The final warning's offset: the largest warning day."""
	return max(warning_days)


def suspension_day(
	day0: date,
	suspend_after: int,
	final_offset: int,
	final_warning_on: date | None,
	grace_until: date | None,
) -> date | None:
	"""The day a subscription may be suspended, or None while no final warning has gone out (a site is never
	suspended without one). Never before ``day0 + suspend_after``, never less than the notice period after the
	final warning was actually sent, and never before ``grace_until`` if that is set."""
	if final_warning_on is None:
		return None
	notice = final_warning_on + timedelta(days=max(suspend_after - final_offset, 1))
	return max(day0 + timedelta(days=suspend_after), notice, grace_until or date.min)


def parse_sent(text: str | None) -> set[int]:
	"""``"-7,-3,0"`` → ``{-7, -3, 0}``: the stages already sent, as stored on a renewal."""
	return {int(part) for part in re.findall(r"-?\d+", text or "")}


def format_sent(stages) -> str:
	"""``{3, -7, 0}`` → ``"-7,0,3"``: the inverse of parse_sent."""
	return ",".join(str(s) for s in sorted(set(stages)))
