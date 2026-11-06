"""Site settings GrowthX Core needs, applied on install and after every migrate.

Email: Frappe adds "Sent via ERPNext" (a link to frappe.io/erpnext) under every email. It's a
white-label leak, and the outside link makes spam filters treat GrowthX mail as phishing (DevOps,
28 Oct 2026). Customer sites get the same from growthx_theme.
"""

import frappe

from growthx_core import website_content
from growthx_core.setup import defaults

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
	# A Customer Group and Territory for sign-ups and partners (ERPNext's wizard may never have run).
	defaults.ensure_all()
	_set_system_setting("disable_standard_email_footer", 1)
	# Our own footer, unless someone has set one (System Settings → Email Footer Address).
	if not frappe.db.get_single_value("System Settings", "email_footer_address"):
		_set_system_setting("email_footer_address", EMAIL_FOOTER)
	_enable_two_factor()
	# SEO settings rows for the six marketing sites (website content).
	website_content.ensure_sites()


def _enable_two_factor():
	"""Two-factor login (authenticator app) for roles with "Two Factor Authentication" ticked, which
	is only GrowthX Content Editor (role fixture): desk users who aren't GrowthX admins (DevOps,
	5 Nov 2026). Users without such a role sign in as before. Re-applied on every migrate."""
	_set_system_setting("enable_two_factor_auth", 1)
	_set_system_setting("two_factor_method", "OTP App")
	if not frappe.db.get_single_value("System Settings", "otp_issuer_name"):
		_set_system_setting("otp_issuer_name", "TBO GrowthX Core")


def _set_system_setting(field: str, value):
	"""Email reads these from the site's defaults, which only saving System Settings updates."""
	frappe.db.set_single_value("System Settings", field, value)
	frappe.db.set_default(field, value)
