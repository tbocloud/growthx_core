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


@endpoint(methods=("GET",), auth=False)
def plans():
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
	return {"items": [s.plan(r) for r in rows]}
