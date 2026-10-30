"""Referral link clicks: the website's /r/CODE page reports each visit, server to server.

The website skips link previews and bots before calling. Core counts at most one click per code
and visitor IP every 30 minutes, keeps only a hash of the IP, and answers the same way whether or
not the code exists, so the endpoint can't be used to find valid codes.
"""

import frappe
from frappe.utils import sha256_hash

from growthx_core.api.v1._core import ApiError, RateLimit, body, client_ip, endpoint, get_str, has_web_key

DEDUP_SECONDS = 30 * 60


@endpoint(methods=("POST",), auth=False, rate_limit=RateLimit(600, 60))
def record_click():
	if not has_web_key():
		raise ApiError(403, "forbidden", "This endpoint is for the GrowthX website's server only.")
	data = body()
	code = (get_str(data, "code", required=True, max_length=40, label="Code") or "").upper()
	landing_path = get_str(data, "landingPath", max_length=200, label="Landing path")

	partner = frappe.db.get_value("GX Partner Profile", {"referral_code": code, "status": "active"})
	if not partner:
		return 202, {"counted": False}
	visitor = sha256_hash(f"{client_ip()}|{code}")
	key = f"growthx:referral_click:{visitor}"
	if frappe.cache.get_value(key):
		return 202, {"counted": False}
	frappe.cache.set_value(key, 1, expires_in_sec=DEDUP_SECONDS)
	frappe.get_doc(
		{
			"doctype": "GX Referral Click",
			"partner": partner,
			"referral_code": code,
			"visitor_hash": visitor,
			"landing_path": landing_path,
		}
	).insert(ignore_permissions=True)
	return 202, {"counted": True}
