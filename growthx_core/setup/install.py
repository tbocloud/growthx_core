"""Site settings GrowthX Core needs, applied on install and after every migrate.

Email: Frappe adds "Sent via ERPNext" (a link to frappe.io/erpnext) under every email. It's a
white-label leak, and the outside link makes spam filters treat GrowthX mail as phishing (DevOps,
28 Oct 2026). Customer sites get the same from growthx_theme.
"""

import frappe

EMAIL_FOOTER = "TBO GrowthX · https://tbogrowthx.com"


def after_install():
	apply_settings()


def after_migrate():
	try:
		apply_settings()
	except Exception:
		# Never fail a deploy over this; the next migrate tries again.
		frappe.log_error(title="GrowthX Core settings could not be applied")


def apply_settings():
	_set_system_setting("disable_standard_email_footer", 1)
	# Our own footer, unless someone has set one (System Settings → Email Footer Address).
	if not frappe.db.get_single_value("System Settings", "email_footer_address"):
		_set_system_setting("email_footer_address", EMAIL_FOOTER)


def _set_system_setting(field: str, value):
	"""Email reads these from the site's defaults, which only saving System Settings updates."""
	frappe.db.set_single_value("System Settings", field, value)
	frappe.db.set_default(field, value)
