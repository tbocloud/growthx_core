import frappe

from growthx_core.api.v1._core import RateLimit, body, client_ip, endpoint, get_email, get_str, ok


@endpoint(methods=("POST",), auth=False, rate_limit=RateLimit(5, 60 * 60))
def submit():
	data = body()
	frappe.get_doc(
		{
			"doctype": "GX Contact Request",
			"full_name": get_str(data, "name", required=True, label="Name"),
			"email": get_email(data),
			"company": get_str(data, "company", label="Company"),
			"message": get_str(data, "message", required=True, max_length=5000, label="Message"),
			"ip_address": client_ip(),
		}
	).insert(ignore_permissions=True)
	return ok(202)
