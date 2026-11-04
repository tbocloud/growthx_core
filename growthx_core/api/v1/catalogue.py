import frappe

from growthx_core.api.v1 import _serializers as s
from growthx_core.api.v1._core import endpoint, get_str, query, validation_error


@endpoint(methods=("GET",), auth=False)
def products():
	rows = frappe.get_all(
		"GX Product",
		fields=["name", "slug", "product_name", "category", "summary", "availability"],
		order_by="sort_order asc, product_name asc",
	)
	return {"items": [s.product(r) for r in rows]}


def _show_prices() -> bool:
	"""Prices go only to signed-in callers (a GrowthX bearer token), unless the site_config flag
	growthx_public_prices makes them public again. Management doesn't want them public."""
	return bool(frappe.conf.get("growthx_public_prices") or getattr(frappe.local, "gx_token", None))


@endpoint(methods=("GET",), auth=False)
def plans():
	"""Public. For guests `price` is null (see _show_prices)."""
	product = get_str(query(), "product")
	filters = {"is_active": 1}
	if product:
		if not frappe.db.exists("GX Product", product):
			raise validation_error({"product": "Unknown product."})
		filters["product"] = product
	rows = frappe.get_all(
		"GX Plan",
		filters=filters,
		fields=[
			"name",
			"product",
			"plan_name",
			"description",
			"price",
			"gst_included",
			"billing_interval",
			"trial_days",
			"features",
			"highlighted",
		],
		order_by="product asc, sort_order asc, price asc",
	)
	show_price = _show_prices()
	return {"items": [s.plan(r, show_price) for r in rows]}
