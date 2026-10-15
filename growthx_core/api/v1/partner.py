import re
from datetime import timedelta

import frappe
from frappe.query_builder.functions import Sum
from frappe.utils import add_months, cint, flt, get_datetime, getdate, now_datetime, nowdate

from growthx_core.api.v1._core import (
	ApiError,
	RateLimit,
	body,
	current_user,
	endpoint,
	get_choice,
	get_str,
	iso,
	money,
	monthly,
	net_of_gst,
	not_found,
	ok,
	page_meta,
	page_params,
	price,
	query,
	validation_error,
)
from growthx_core.partners.referrals import referral_url


def current_partner():
	partner = frappe.db.get_value(
		"GX Partner Profile",
		{"user": current_user()},
		["name", "referral_code", "sales_partner", "status", "user"],
		as_dict=True,
	)
	if not partner:
		raise ApiError(403, "forbidden", "No partner profile is linked to this user.")
	return partner


@endpoint(methods=("GET",), roles=("partner",))
def summary():
	return _summary(current_partner())


def _summary(partner) -> dict:
	accounts = frappe.get_all("GX Account", {"referred_by": partner.name}, ["name", "status"])
	active = [a.name for a in accounts if a.status == "active"]

	rate = (
		flt(frappe.db.get_value("Sales Partner", partner.sales_partner, "commission_rate"))
		if partner.sales_partner
		else 0
	)
	# Commission rules (base, recurring or not) are pending D8: for now, the monthly rate x the
	# active subscriptions' monthly price excluding GST.
	recurring = 0.0
	if active and rate:
		for sub in frappe.get_all(
			"GX Subscription",
			{"account": ("in", active), "status": "active"},
			["price", "gst_included", "gst_rate", "billing_interval"],
		):
			recurring += (
				monthly(net_of_gst(sub.price, sub.gst_included, sub.gst_rate), sub.billing_interval)
				* rate
				/ 100
			)

	# No payouts exist yet (GX Payout is Phase 3), so all commission on paid invoices is pending.
	pending = 0.0
	if partner.sales_partner:
		si = frappe.qb.DocType("Sales Invoice")
		pending = flt(
			frappe.qb.from_(si)
			.select(Sum(si.total_commission))
			.where((si.sales_partner == partner.sales_partner) & (si.docstatus == 1) & (si.status == "Paid"))
			.run()[0][0]
		)

	return {
		"referralCode": partner.referral_code,
		"referralUrl": referral_url(partner.referral_code),
		"activeCustomers": len(active),
		"referredSignups": len(accounts),
		"openLeads": None,
		"monthlyRecurringCommission": money(recurring),
		"pendingPayout": money(pending),
	}


@endpoint(methods=("GET",), roles=("partner",))
def commission_history():
	partner = current_partner()
	first_month = getdate(add_months(nowdate(), -11)).replace(day=1)
	months = [add_months(first_month, i).strftime("%Y-%m") for i in range(12)]
	totals = dict.fromkeys(months, 0.0)

	if partner.sales_partner:
		rows = frappe.db.sql(
			"""
			select date_format(posting_date, '%%Y-%%m') as month, sum(total_commission) as amount
			from `tabSales Invoice`
			where sales_partner = %s and docstatus = 1 and posting_date >= %s
			group by month
			""",
			(partner.sales_partner, first_month),
			as_dict=True,
		)
		for row in rows:
			if row.month in totals:
				totals[row.month] = flt(row.amount)

	return {"items": [{"month": m, "amount": money(totals[m])} for m in months]}


@endpoint(methods=("GET",), auth=False, rate_limit=RateLimit(60, 60))
def referrer():
	"""Public: who a referral code belongs to, for the website's welcome page ("Chidi invited
	you"). Only active partners; anything else is 404 so codes can't be probed for status.
	Returns only what the partner shares by handing out their link."""
	code = (get_str(query(), "code", required=True, max_length=40, label="Code") or "").upper()
	partner = frappe.db.get_value(
		"GX Partner Profile",
		{"referral_code": code, "status": "active"},
		["referral_code", "partner_name", "company"],
		as_dict=True,
	)
	if not partner:
		raise not_found()
	return {"code": partner.referral_code, "name": partner.partner_name, "company": partner.company or None}


# ---------------------------------------------------------------- partner app (Flutter)
# Customers, dashboard, referral stats, earnings, notifications, devices, profile and preferences.
# Shapes: openapi.yaml. Leads, campaigns, commissions, payouts and payout method are still proposed
# (they wait on business decisions, see docs/api-decisions.md).

CUSTOMER_STATUS_FROM_ACCOUNT = {
	"trial": "trialing",
	"active": "active",
	"suspended": "suspended",
	"churned": "canceled",
}
TRIAL_SOON_DAYS = 7


def _partner_customers(partner) -> list[dict]:
	"""Every customer attributed to `partner`, newest first, as PartnerCustomer dicts.

	Each dict also carries "_sub" (the latest subscription row) for detail views. Partners have a
	few hundred customers at most, so filtering and paging happen in Python."""
	accounts = frappe.get_all(
		"GX Account",
		filters={"referred_by": partner.name},
		fields=[
			"name",
			"account_name",
			"user",
			"contact_email",
			"phone",
			"status",
			"referred_at",
			"creation",
		],
	)
	if not accounts:
		return []
	names = [a.name for a in accounts]
	subs = {}
	for sub in frappe.get_all(
		"GX Subscription",
		filters={"account": ("in", names)},
		fields=[
			"name",
			"account",
			"plan",
			"product",
			"status",
			"price",
			"gst_included",
			"billing_interval",
			"trial_ends_at",
			"current_period_end",
			"creation",
		],
		order_by="creation asc",
	):
		subs[sub.account] = sub  # latest wins
	tenants = {}
	for t in frappe.get_all(
		"GX Tenant", filters={"account": ("in", names)}, fields=["account", "status"], order_by="creation asc"
	):
		tenants[t.account] = t.status
	users = dict(
		frappe.get_all(
			"User",
			filters={"name": ("in", [a.user for a in accounts])},
			fields=["name", "full_name"],
			as_list=True,
		)
	)
	plan_names = dict(frappe.get_all("GX Plan", fields=["name", "plan_name"], as_list=True))

	rows = []
	for a in accounts:
		sub = subs.get(a.name)
		joined = a.referred_at or a.creation
		rows.append(
			{
				"id": a.name,
				"businessName": a.account_name,
				"contactName": users.get(a.user) or None,
				"phone": a.phone or None,
				"email": a.contact_email or None,
				"city": None,
				"productSlug": sub.product if sub else None,
				"plan": {
					"id": sub.plan,
					"name": plan_names.get(sub.plan) or sub.plan,
					"interval": sub.billing_interval,
					"price": price(sub.price, sub.gst_included),
				}
				if sub
				else None,
				"subscriptionStatus": sub.status
				if sub
				else CUSTOMER_STATUS_FROM_ACCOUNT.get(a.status, "trialing"),
				"siteActive": tenants.get(a.name) == "ready",
				"joinedAt": iso(joined),
				"trialEndsAt": iso(sub.trial_ends_at) if sub and sub.status == "trialing" else None,
				"renewsAt": iso(sub.current_period_end) if sub and sub.status != "trialing" else None,
				"source": {"type": "referral_link", "campaignId": None},
				"_joined": get_datetime(joined),
				"_trial_ends": get_datetime(sub.trial_ends_at)
				if sub and sub.status == "trialing" and sub.trial_ends_at
				else None,
				"_sub": sub,
				"_tenant_status": tenants.get(a.name),
			}
		)
	rows.sort(key=lambda r: r["_joined"], reverse=True)
	return rows


def _public(row: dict) -> dict:
	return {k: v for k, v in row.items() if not k.startswith("_")}


def _trial_ending_soon(row, now) -> bool:
	return bool(row["_trial_ends"]) and now <= row["_trial_ends"] <= now + timedelta(days=TRIAL_SOON_DAYS)


def _joined_this_month(row, now) -> bool:
	return (row["_joined"].year, row["_joined"].month) == (now.year, now.month)


@endpoint(methods=("GET",), roles=("partner",))
def customers():
	data = query()
	page, page_size = page_params(data)
	status = get_choice(data, "status", ("trialing", "active", "past_due", "suspended", "canceled"))
	segment = get_choice(data, "segment", ("trials_ending_soon", "joined_this_month"))
	sort = data.get("sort") or "-joinedAt"
	if sort.lstrip("-") not in ("joinedAt", "businessName"):
		raise validation_error({"sort": "Must be joinedAt or businessName (prefix - for descending)."})
	q = (get_str(data, "q", max_length=100) or "").lower()

	now = now_datetime()
	rows = _partner_customers(current_partner())
	if q:
		rows = [
			r
			for r in rows
			if q in " ".join(filter(None, [r["businessName"], r["contactName"], r["city"]])).lower()
		]
	if status:
		rows = [r for r in rows if r["subscriptionStatus"] == status]
	if segment == "trials_ending_soon":
		rows = [r for r in rows if _trial_ending_soon(r, now)]
	elif segment == "joined_this_month":
		rows = [r for r in rows if _joined_this_month(r, now)]

	key = (
		(lambda r: r["_joined"])
		if sort.lstrip("-") == "joinedAt"
		else (lambda r: (r["businessName"] or "").lower())
	)
	rows.sort(key=key, reverse=sort.startswith("-"))
	start = (page - 1) * page_size
	return page_meta(page, page_size, len(rows), [_public(r) for r in rows[start : start + page_size]])


@endpoint(methods=("GET",), roles=("partner",))
def customer():
	customer_id = get_str(query(), "id", required=True)
	row = next((r for r in _partner_customers(current_partner()) if r["id"] == customer_id), None)
	if not row:
		raise not_found()  # not this partner's customer

	timeline = [{"at": row["joinedAt"], "event": "signed_up", "description": None}]
	sub = row["_sub"]
	if sub:
		timeline.append({"at": iso(sub.creation), "event": "trial_started", "description": None})
	ready_at = frappe.db.get_value("GX Tenant", {"account": customer_id, "status": "ready"}, "ready_at")
	if ready_at:
		timeline.append({"at": iso(ready_at), "event": "site_ready", "description": None})
	timeline.sort(key=lambda e: e["at"] or "")
	# Commission lines arrive with the commission ledger (Phase 3, decision D8).
	return {**_public(row), "timeline": timeline, "commissions": []}


@endpoint(methods=("GET",), roles=("partner",))
def referral_stats():
	data = query()
	today = getdate(nowdate())
	try:
		start = getdate(data.get("from")) if data.get("from") else today.replace(day=1)
		end = getdate(data.get("to")) if data.get("to") else today
	except Exception:
		raise validation_error({"from": "Dates must be YYYY-MM-DD."})
	if start > end:
		raise validation_error({"from": "from must be on or before to."})

	rows = [r for r in _partner_customers(current_partner()) if start <= r["_joined"].date() <= end]
	signups = len(rows)
	conversions = sum(1 for r in rows if r["subscriptionStatus"] in ("active", "past_due"))
	return {
		"from": start.isoformat(),
		"to": end.isoformat(),
		"clicks": None,  # until the website records link visits
		"signups": signups,
		"conversions": conversions,
		"conversionRate": round(conversions / signups, 4) if signups else None,
	}


def _earnings(partner) -> dict:
	"""PLACEHOLDER until the commission rules are decided (D8) and GX Commission/GX Payout exist:
	commission comes from ERPNext Sales Invoices for the partner's Sales Partner; commission on
	paid invoices counts as pending; nothing is approved or paid out yet."""
	zero = money(0)
	if not partner.sales_partner:
		return {
			"thisMonth": zero,
			"lastMonth": zero,
			"pending": zero,
			"approved": zero,
			"paidLifetime": zero,
			"lifetimeTotal": zero,
			"nextPayoutDate": None,
			"tdsRate": None,
		}
	si = frappe.qb.DocType("Sales Invoice")

	def total(*conditions):
		where = (si.sales_partner == partner.sales_partner) & (si.docstatus == 1)
		for c in conditions:
			where = where & c
		return flt(frappe.qb.from_(si).select(Sum(si.total_commission)).where(where).run()[0][0])

	today = getdate(nowdate())
	this_month = today.replace(day=1)
	last_month = getdate(add_months(this_month, -1))
	return {
		"thisMonth": money(total(si.posting_date >= this_month)),
		"lastMonth": money(total(si.posting_date >= last_month, si.posting_date < this_month)),
		"pending": money(total(si.status == "Paid")),
		"approved": zero,
		"paidLifetime": zero,
		"lifetimeTotal": money(total()),
		"nextPayoutDate": None,
		"tdsRate": None,
	}


@endpoint(methods=("GET",), roles=("partner",))
def earnings():
	return _earnings(current_partner())


@endpoint(methods=("GET",), roles=("partner",))
def dashboard():
	data = query()
	n_customers = min(max(cint(data.get("recentCustomers", 4)), 0), 10)
	partner = current_partner()
	now = now_datetime()
	rows = _partner_customers(partner)
	return {
		**_summary(partner),
		"earnings": _earnings(partner),
		"newCustomersThisMonth": sum(1 for r in rows if _joined_this_month(r, now)),
		"trialsEndingNext7Days": sum(1 for r in rows if _trial_ending_soon(r, now)),
		"unreadNotifications": _unread_count(partner.user),
		"recentCustomers": [_public(r) for r in rows[:n_customers]],
		"recentCommissions": [],  # the commission ledger is Phase 3 (D8)
	}


# ---------------------------------------------------------------- notifications


NOTIFICATION_CATEGORIES = ("commission", "payout", "customer", "trial", "lead", "account")


def _unread_count(user) -> int:
	return frappe.db.count("GX Notification", {"user": user, "read_at": ("is", "not set"), "dismissed": 0})


def _notification(n) -> dict:
	return {
		"id": n.name,
		"category": n.category,
		"title": n.title,
		"body": n.body or "",
		"createdAt": iso(n.creation),
		"readAt": iso(n.read_at),
		"deepLink": {"type": n.deep_link_type, "id": n.deep_link_id or None} if n.deep_link_type else None,
	}


def _own_notification(notification_id):
	if not notification_id or not frappe.db.exists(
		"GX Notification", {"name": notification_id, "user": current_user(), "dismissed": 0}
	):
		raise not_found()
	return notification_id


@endpoint(methods=("GET",), roles=("partner",))
def notifications():
	data = query()
	page, page_size = page_params(data)
	category = get_choice(data, "category", NOTIFICATION_CATEGORIES)
	unread_only = str(data.get("unreadOnly", "")).lower() in ("1", "true")
	filters = {"user": current_user(), "dismissed": 0}
	if category:
		filters["category"] = category
	if unread_only:
		filters["read_at"] = ("is", "not set")
	rows = frappe.get_all(
		"GX Notification",
		filters=filters,
		fields=["name", "category", "title", "body", "creation", "read_at", "deep_link_type", "deep_link_id"],
		order_by="creation desc",
		limit_start=(page - 1) * page_size,
		limit_page_length=page_size,
	)
	result = page_meta(
		page, page_size, frappe.db.count("GX Notification", filters), [_notification(n) for n in rows]
	)
	result["unreadCount"] = _unread_count(current_user())
	return result


@endpoint(methods=("POST",), roles=("partner",))
def mark_notification_read():
	name = _own_notification(get_str(body(), "id", required=True))
	if not frappe.db.get_value("GX Notification", name, "read_at"):
		frappe.db.set_value("GX Notification", name, "read_at", now_datetime(), update_modified=False)
	return ok()


@endpoint(methods=("POST",), roles=("partner",))
def mark_all_notifications_read():
	names = frappe.get_all(
		"GX Notification",
		{"user": current_user(), "read_at": ("is", "not set"), "dismissed": 0},
		pluck="name",
	)
	if names:
		frappe.db.set_value(
			"GX Notification", {"name": ("in", names)}, "read_at", now_datetime(), update_modified=False
		)
	return {"ok": True, "updated": len(names)}


@endpoint(methods=("POST",), roles=("partner",))
def dismiss_notification():
	name = _own_notification(get_str(body(), "id", required=True))
	frappe.db.set_value("GX Notification", name, "dismissed", 1, update_modified=False)
	return ok()


# ---------------------------------------------------------------- devices (push)


@endpoint(methods=("POST",), roles=("partner",), rate_limit=RateLimit(30, 60 * 60))
def register_device():
	from growthx_core.partners.notifications import token_hash

	data = body()
	token = get_str(data, "token", required=True, max_length=4096, label="Token")
	platform = get_choice(data, "platform", ("android", "ios"), required=True)
	app_version = get_str(data, "appVersion", max_length=40)
	existing = frappe.db.get_value("GX Device", {"token_hash": token_hash(token)}, "name")
	values = {
		"user": current_user(),
		"platform": platform,
		"app_version": app_version,
		"last_seen": now_datetime(),
	}
	if existing:
		# Same phone, possibly a different partner logged in now: the token moves to the current user.
		frappe.db.set_value("GX Device", existing, values)
	else:
		frappe.get_doc(
			{"doctype": "GX Device", "token": token, "token_hash": token_hash(token), **values}
		).insert(ignore_permissions=True)
	return ok()


@endpoint(methods=("POST",), roles=("partner",))
def unregister_device():
	from growthx_core.partners.notifications import token_hash

	token = get_str(body(), "token", required=True, max_length=4096, label="Token")
	name = frappe.db.get_value("GX Device", {"token_hash": token_hash(token), "user": current_user()}, "name")
	if name:
		frappe.delete_doc("GX Device", name, ignore_permissions=True, force=True)
	return ok()


# ---------------------------------------------------------------- profile and preferences


PREFERENCE_FIELDS = {
	"commissions": "notify_commissions",
	"newCustomers": "notify_new_customers",
	"trialReminders": "notify_trial_reminders",
	"productUpdates": "notify_product_updates",
}
PHONE_RE = re.compile(r"^\+?[0-9][0-9 \-]{6,18}$")


def _profile(partner_name: str) -> dict:
	p = frappe.get_doc("GX Partner Profile", partner_name)
	email = frappe.db.get_value("User", p.user, "email")
	return {
		"id": p.name,
		"name": p.partner_name,
		"email": email,
		"phone": p.phone or None,
		"city": p.city or None,
		"company": p.company or None,
		"gstin": p.gstin or None,
		"panMasked": None,  # PAN is collected with payout details (Phase 3)
		"tier": p.tier or None,
		"status": p.status,
		"referralCode": p.referral_code,
		"memberSince": getdate(p.creation).isoformat(),
		"payoutMethod": None,  # Phase 3
	}


@endpoint(methods=("GET",), roles=("partner",))
def profile():
	return _profile(current_partner().name)


@endpoint(methods=("POST",), roles=("partner",), rate_limit=RateLimit(30, 60 * 60))
def update_profile():
	data = body()
	allowed = {"name", "phone", "city", "company"}
	unknown = sorted(set(data) - allowed)
	if unknown:
		raise validation_error({unknown[0]: "This field can't be changed here."})
	if not data:
		raise validation_error({"name": "Send at least one field to change."})

	updates = {}
	if "name" in data:
		updates["partner_name"] = get_str(data, "name", required=True, label="Name")
	if "phone" in data:
		phone = get_str(data, "phone", max_length=20, label="Phone")
		if phone and not PHONE_RE.match(phone):
			raise validation_error({"phone": "Enter a valid phone number, e.g. +919876543210."})
		updates["phone"] = phone
	if "city" in data:
		updates["city"] = get_str(data, "city", label="City")
	if "company" in data:
		updates["company"] = get_str(data, "company", label="Company")

	partner = current_partner()
	doc = frappe.get_doc("GX Partner Profile", partner.name)
	doc.update(updates)
	doc.save(ignore_permissions=True)
	if "partner_name" in updates:
		first, _, last = updates["partner_name"].partition(" ")
		frappe.db.set_value("User", partner.user, {"first_name": first, "last_name": last or None})
	return _profile(partner.name)


def _preferences(partner_name) -> dict:
	values = frappe.db.get_value(
		"GX Partner Profile", partner_name, list(PREFERENCE_FIELDS.values()), as_dict=True
	)
	return {key: bool(cint(values[field])) for key, field in PREFERENCE_FIELDS.items()}


@endpoint(methods=("GET",), roles=("partner",))
def notification_preferences():
	return _preferences(current_partner().name)


@endpoint(methods=("POST",), roles=("partner",))
def update_notification_preferences():
	data = body()
	if not data:
		raise validation_error({"commissions": "Send at least one preference."})
	updates = {}
	for key, value in data.items():
		if key not in PREFERENCE_FIELDS:
			raise validation_error({key: "Unknown preference."})
		if not isinstance(value, bool):
			raise validation_error({key: "Must be true or false."})
		updates[PREFERENCE_FIELDS[key]] = int(value)
	partner = current_partner()
	frappe.db.set_value("GX Partner Profile", partner.name, updates)
	return _preferences(partner.name)
