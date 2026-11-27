# Copyright (c) 2026, TBO GrowthX and contributors
# For license information, please see license.txt

"""Renewal settings (growthx-infra docs/renewals-design.md 6.1).

Frappe applies a Single's defaults only while none of its fields is stored: once anything is (the
platform site list every 5 minutes, say), every field not stored yet reads 0 or blank. `fill_defaults`
stores the defaults for those fields (on install and by a patch), the readers fall back to the
defaults for a 0 (`get_int`), and `validate` refuses values that would make the job act too fast.
"""

import re

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint

from growthx_core.billing.renewal_schedule import parse_days

DOCTYPE = "GX Billing Settings"
DAY_LISTS = ("paid_reminder_days", "yearly_extra_reminder_days", "grace_warning_days", "trial_reminder_days")
# The Press-side job's platform site list (site_sync.report_platform_sites).
JOB_FIELDS = ("platform_sites", "platform_sites_at")
# Numbers that must be at least 1 (suspend_after_days has its own rule).
AT_LEAST_ONE = (
	"trial_archive_after_days",
	"trial_archive_warning_days",
	"max_suspensions_per_run",
	"max_archives_per_run",
	"sync_max_suspensions_per_run",
)


class GXBillingSettings(Document):
	def validate(self):
		# Written by the jobs, never by hand: a form opened before they wrote must not put its stale copy back.
		for field in JOB_FIELDS:
			self.set(field, frappe.db.get_single_value(DOCTYPE, field, cache=False))
		self.started_on = self.started_on or frappe.db.get_single_value(DOCTYPE, "started_on", cache=False)
		errors = []
		for field in DAY_LISTS:
			if not valid_days(self.get(field)):
				errors.append(
					_("{0}: enter whole days above 0, separated by commas (for example 7,3,1).").format(
						_(self.meta.get_label(field))
					)
				)
		for field in AT_LEAST_ONE:
			if cint(self.get(field)) < 1:
				errors.append(_("{0} must be at least 1.").format(_(self.meta.get_label(field))))
		final = max(parse_days(self.grace_warning_days) or [0])
		if cint(self.suspend_after_days) < final + 1:
			errors.append(
				_(
					"Suspend After Days must be at least {0}: a day after the final grace warning (day {1})."
				).format(final + 1, final)
			)
		if errors:
			frappe.throw("<br>".join(errors), title=_("Renewal settings"))


def valid_days(text) -> bool:
	"""``"7, 3,1"``: one or more whole days above 0, separated by commas or spaces, and nothing else."""
	parts = [p for p in re.split(r"[\s,]+", (text or "").strip()) if p]
	return bool(parts) and all(re.fullmatch(r"\d+", p) and int(p) > 0 for p in parts)


def default(field: str):
	"""The DocType's default for `field` (as stored in its JSON), or None."""
	df = frappe.get_meta(DOCTYPE).get_field(field)
	return df.default if df else None


def get_int(settings, field: str) -> int:
	"""A whole-number setting, or its default when it reads 0 (never stored, or stored before it had a value)."""
	return cint(settings.get(field)) or cint(default(field))


def int_setting(field: str) -> int:
	"""`get_int` for one setting, read on its own."""
	return cint(frappe.db.get_single_value(DOCTYPE, field, cache=False)) or cint(default(field))


def fill_defaults() -> list[str]:
	"""Store the DocType's default for every setting with none stored, a blank one, or a 0 for a number
	(which validate refuses). A checkbox already stored, on or off, is kept. Returns the fields filled."""
	stored = frappe.db.get_singles_dict(DOCTYPE)
	values = {}
	for df in frappe.get_meta(DOCTYPE).fields:
		if df.default in (None, ""):
			continue
		value = stored.get(df.fieldname)
		if value in (None, "") or (df.fieldtype == "Int" and not cint(value)):
			values[df.fieldname] = df.default
	if values:
		# Not as an edit: `modified` stays, so a form open in Desk can still be saved.
		frappe.db.set_single_value(DOCTYPE, values, update_modified=False)
	return sorted(values)
