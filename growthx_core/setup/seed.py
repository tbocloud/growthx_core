"""Seed data for development and staging: products, plans, a demo admin, partner and customer.

    bench --site <site> execute growthx_core.setup.seed.run
    bench --site <site> execute growthx_core.setup.seed.run --kwargs "{'password': '...'}"

Safe to run more than once. Demo passwords are never stored in the repo: pass one, or a random
one is generated and printed. Prices, trial length and commission are PLACEHOLDERS until the
business decisions D6 (pricing/GST), D7 (trial) and D8 (commission) are made.
"""

import secrets

import frappe

from growthx_core import accounts
from growthx_core.provisioning import runner

PRODUCTS = [
	dict(
		slug="solar",
		product_name="GrowthX Solar",
		category="Solar installers & distributors",
		summary="Site surveys, projects, installation and service for solar businesses.",
		availability="available",
		vertical_app="solar_operations",
		press_release_group="solar",
		sort_order=1,
	),
	dict(
		slug="salon",
		product_name="GrowthX Salon",
		category="Salons & spas",
		summary="Bookings, stylists and client history in one calendar.",
		sort_order=2,
	),
	dict(
		slug="laundry",
		product_name="GrowthX Laundry",
		category="Laundry & dry cleaning",
		summary="Track every order from pickup to delivery.",
		sort_order=3,
	),
	dict(
		slug="tailoring",
		product_name="GrowthX Tailoring",
		category="Tailors & boutiques",
		summary="Measurements, orders and fittings, without the paper notebook.",
		sort_order=4,
	),
]

# Placeholder prices from the web team's contract (02 §2.0.3). Yearly = 10 x monthly.
TIERS = [
	(
		"starter",
		"Starter",
		699,
		"For small teams getting started.",
		["Up to 3 staff logins", "1 location", "Email support"],
		0,
	),
	(
		"growth",
		"Growth",
		1499,
		"For busy businesses ready to grow.",
		[
			"Up to 10 staff logins",
			"Up to 3 locations",
			"Customer reminders by SMS",
			"Priority support & onboarding",
		],
		1,
	),
	(
		"scale",
		"Scale",
		3999,
		"For multi-location businesses.",
		["Unlimited staff logins", "Unlimited locations", "Dedicated account manager"],
		0,
	),
]


def run(password: str | None = None):
	_products()
	_plans()
	password = password or secrets.token_urlsafe(12)
	created = _demo_users(password)
	frappe.db.commit()
	print("GrowthX seed data is in place.")
	if created:
		print(f"Demo users created (password for all: {password}):")
		for email in created:
			print(f"  {email}")
	else:
		print("Demo users already existed; their passwords were not changed.")


def _upsert(doctype, name, values):
	if frappe.db.exists(doctype, name):
		doc = frappe.get_doc(doctype, name)
		doc.update(values)
		doc.save(ignore_permissions=True)
	else:
		frappe.get_doc({"doctype": doctype, **values}).insert(ignore_permissions=True)


def _products():
	for p in PRODUCTS:
		_upsert("GX Product", p["slug"], p)


def _plans():
	for tier, label, monthly_price, description, features, highlighted in TIERS:
		for interval, price in (("month", monthly_price), ("year", monthly_price * 10)):
			plan_id = f"solar-{tier}-{interval}"
			_upsert(
				"GX Plan",
				plan_id,
				dict(
					plan_id=plan_id,
					product="solar",
					plan_name=label,
					billing_interval=interval,
					price=price,
					gst_included=0,
					gst_rate=18,
					trial_days=14,
					description=description,
					features="\n".join(features),
					highlighted=highlighted,
					is_active=1,
					sort_order=[t[0] for t in TIERS].index(tier),
				),
			)


def _demo_users(password: str) -> list[str]:
	created = []

	if not frappe.db.exists("User", "admin@growthx.test"):
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": "admin@growthx.test",
				"first_name": "GrowthX",
				"last_name": "Admin",
				"send_welcome_email": 0,
				"new_password": password,
				"roles": [{"role": "GrowthX Admin"}],
			}
		).insert(ignore_permissions=True)
		created.append(user.name)

	if not frappe.db.exists("User", "partner@growthx.test"):
		user = accounts.create_partner_account(
			email="partner@growthx.test", full_name="Chidi Okafor", password=password, company="Okafor Sales"
		)
		profile = frappe.get_doc("GX Partner Profile", {"user": user})
		profile.status = "active"  # approve the demo partner
		profile.save(ignore_permissions=True)
		frappe.db.set_value("Sales Partner", profile.sales_partner, "commission_rate", 10)  # placeholder (D8)
		created.append(user)

	if not frappe.db.exists("User", "customer@growthx.test"):
		partner_code = frappe.db.get_value(
			"GX Partner Profile", {"user": "partner@growthx.test"}, "referral_code"
		)
		plan = frappe.get_doc("GX Plan", "solar-growth-month")
		user, tenant = accounts.create_customer_account(
			email="customer@growthx.test",
			full_name="Amina Noor",
			company="Noor Solar",
			password=password,
			phone="+919876543210",
			plan=plan,
			referral_code=partner_code,
		)
		frappe.db.commit()
		# Provision now (fake Press) so the demo customer has a ready site without a worker.
		if tenant:
			runner.run(tenant)
		created.append(user)

	return created
