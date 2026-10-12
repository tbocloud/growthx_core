import frappe

import growthx_core


@frappe.whitelist(allow_guest=True, methods=["GET"])
def ping():
	"""Liveness check for web, mobile and monitoring. Returns no tenant or user data."""
	return {"status": "ok", "version": growthx_core.__version__}
