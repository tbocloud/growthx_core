"""Partner app endpoints (Flutter): customers, dashboard, stats, earnings, notifications, devices,
profile and preferences. Two partners, so every test also proves a partner sees only their own data."""

import json
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, now_datetime

from growthx_core.api.v1 import _core, partner
from growthx_core.partners import notifications as notif

PREFIX = "gx-pa-test"
PRODUCT = f"{PREFIX}-product"
PLAN = f"{PREFIX}-plan"


def _user(email, role):
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": email.split("@")[0],
				"send_welcome_email": 0,
				"roles": [{"role": role}],
			}
		).insert(ignore_permissions=True)
	return email


def _partner(key):
	user = _user(f"{PREFIX}-{key}@example.com", "GrowthX Partner")
	return frappe.get_doc(
		{
			"doctype": "GX Partner Profile",
			"partner_name": f"Partner {key.upper()}",
			"user": user,
			"referral_code": f"GXPA-{key.upper()}-{frappe.generate_hash(length=4).upper()}",
			"status": "pending_approval",
		}
	).insert(ignore_permissions=True)


def _customer(partner_doc, key, status="trialing", trial_days=10, site_ready=True, joined_days_ago=0):
	user = _user(f"{PREFIX}-cust-{key}@example.com", "GrowthX Customer")
	account = frappe.get_doc(
		{
			"doctype": "GX Account",
			"account_name": f"Business {key}",
			"user": user,
			"contact_email": user,
			"phone": "+919800000000",
			"referred_by": partner_doc.name,
			"referred_at": add_days(now_datetime(), -joined_days_ago),
		}
	).insert(ignore_permissions=True)
	sub = frappe.get_doc(
		{
			"doctype": "GX Subscription",
			"account": account.name,
			"plan": PLAN,
			"status": status,
			"price": 1499,
			"trial_ends_at": add_days(now_datetime(), trial_days) if status == "trialing" else None,
			"current_period_end": add_days(now_datetime(), 30) if status != "trialing" else None,
		}
	).insert(ignore_permissions=True)
	frappe.get_doc(
		{
			"doctype": "GX Tenant",
			"account": account.name,
			"product": PRODUCT,
			"subscription": sub.name,
			"subdomain": f"{PREFIX}-{key}-{frappe.generate_hash(length=4)}",
			"status": "ready" if site_ready else "provisioning",
			"ready_at": now_datetime() if site_ready else None,
		}
	).insert(ignore_permissions=True)
	return account


class TestPartnerApp(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls._cleanup()
		frappe.get_doc(
			{"doctype": "GX Product", "slug": PRODUCT, "product_name": "PA Test", "availability": "available"}
		).insert(ignore_permissions=True)
		frappe.get_doc(
			{
				"doctype": "GX Plan",
				"plan_id": PLAN,
				"product": PRODUCT,
				"plan_name": "Growth",
				"billing_interval": "month",
				"price": 1499,
			}
		).insert(ignore_permissions=True)
		cls.a = _partner("a")
		cls.b = _partner("b")
		cls.trial_soon = _customer(cls.a, "trial-soon", trial_days=3)
		cls.trial_later = _customer(cls.a, "trial-later", trial_days=12, site_ready=False)
		cls.paying = _customer(cls.a, "paying", status="active", joined_days_ago=40)
		cls.other = _customer(cls.b, "other")
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		cls._cleanup()
		super().tearDownClass()

	@classmethod
	def _cleanup(cls):
		accounts = frappe.get_all("GX Account", {"contact_email": ("like", f"{PREFIX}%")}, pluck="name")
		if accounts:
			frappe.db.delete("GX Tenant", {"account": ("in", accounts)})
			frappe.db.delete("GX Subscription", {"account": ("in", accounts)})
			frappe.db.delete("GX Account", {"name": ("in", accounts)})
		users = frappe.get_all("User", {"name": ("like", f"{PREFIX}%")}, pluck="name")
		if users:
			frappe.db.delete("GX Notification", {"user": ("in", users)})
			frappe.db.delete("GX Device", {"user": ("in", users)})
			frappe.db.delete("GX Partner Profile", {"user": ("in", users)})
		for user in users:
			frappe.delete_doc("User", user, force=True, ignore_permissions=True)
		frappe.db.delete("GX Plan", {"name": PLAN})
		frappe.db.delete("GX Product", {"name": PRODUCT})
		frappe.db.commit()

	def setUp(self):
		p = patch.object(_core, "_check_rate_limit")
		p.start()
		self.addCleanup(p.stop)
		frappe.local.request_ip = "127.0.0.1"
		self.as_user(self.a.user)

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.delete("GX Notification", {"user": ("like", f"{PREFIX}%")})
		frappe.db.delete("GX Device", {"user": ("like", f"{PREFIX}%")})
		frappe.db.commit()

	def as_user(self, user):
		frappe.set_user(user)
		frappe.local.gx_token = "test-token"

	def call(self, fn, data=None):
		frappe.local.form_dict = frappe._dict(data or {})
		response = getattr(partner, fn)()
		return response.status_code, json.loads(response.get_data())

	# ------------------------------------------------------------ access

	def test_only_partners_can_call(self):
		self.as_user(self.paying.user)  # a customer
		for fn in ("customers", "dashboard", "notifications", "profile"):
			status, body = self.call(fn)
			self.assertEqual((status, body["error"]["code"]), (403, "forbidden"), fn)

	# ------------------------------------------------------------ customers

	def test_customers_are_only_the_partners_own(self):
		status, body = self.call("customers")
		self.assertEqual(status, 200)
		ids = {c["id"] for c in body["items"]}
		self.assertEqual(ids, {self.trial_soon.name, self.trial_later.name, self.paying.name})
		self.assertEqual(body["total"], 3)
		first = body["items"][0]
		self.assertEqual(first["plan"]["price"], {"amount": 149900, "currency": "INR", "gstIncluded": False})
		self.assertNotIn("_sub", first)

	def test_customer_filters_search_sort_and_paging(self):
		self.assertEqual(
			{c["id"] for c in self.call("customers", {"status": "active"})[1]["items"]}, {self.paying.name}
		)
		soon = self.call("customers", {"segment": "trials_ending_soon"})[1]["items"]
		self.assertEqual([c["id"] for c in soon], [self.trial_soon.name])
		self.assertEqual(self.call("customers", {"q": "trial-later"})[1]["total"], 1)
		page = self.call("customers", {"pageSize": 2, "page": 2, "sort": "businessName"})[1]
		self.assertEqual((page["total"], len(page["items"])), (3, 1))
		self.assertEqual(self.call("customers", {"sort": "bogus"})[0], 422)

	def test_customer_detail_and_ownership(self):
		status, body = self.call("customer", {"id": self.trial_soon.name})
		self.assertEqual(status, 200)
		self.assertTrue(body["siteActive"])
		self.assertEqual(body["timeline"][0]["event"], "signed_up")
		self.assertIn("site_ready", [e["event"] for e in body["timeline"]])
		self.assertEqual(body["commissions"], [])
		self.assertEqual(self.call("customer", {"id": self.other.name})[0], 404)

	def test_referral_stats(self):
		body = self.call("referral_stats")[1]
		self.assertEqual((body["signups"], body["conversions"], body["clicks"]), (2, 0, None))
		wide = self.call("referral_stats", {"from": "2000-01-01", "to": "2100-01-01"})[1]
		self.assertEqual((wide["signups"], wide["conversions"], wide["conversionRate"]), (3, 1, 0.3333))
		self.assertEqual(self.call("referral_stats", {"from": "2026-12-01", "to": "2026-01-01"})[0], 422)

	def test_dashboard_and_earnings(self):
		status, body = self.call("dashboard", {"recentCustomers": 2})
		self.assertEqual(status, 200)
		self.assertEqual(body["referredSignups"], 3)
		self.assertEqual(body["trialsEndingNext7Days"], 1)
		self.assertEqual(len(body["recentCustomers"]), 2)
		self.assertEqual(body["earnings"]["thisMonth"], {"amount": 0, "currency": "INR"})
		self.assertEqual(self.call("earnings")[1]["approved"], {"amount": 0, "currency": "INR"})

	# ------------------------------------------------------------ notifications

	def _notify(self, user, category="customer", title="New customer", **kw):
		name = notif.notify(user, category, title, "body", **kw)
		frappe.db.commit()
		return name

	def test_notifications_list_read_and_dismiss(self):
		n1 = self._notify(self.a.user, deep_link_type="customer", deep_link_id=self.paying.name)
		n2 = self._notify(self.a.user, "account", "Welcome")
		self._notify(self.b.user)

		body = self.call("notifications")[1]
		self.assertEqual((body["total"], body["unreadCount"]), (2, 2))
		self.assertEqual({n["id"] for n in body["items"]}, {n1, n2})
		self.assertEqual(
			next(n for n in body["items"] if n["id"] == n1)["deepLink"],
			{"type": "customer", "id": self.paying.name},
		)
		self.assertEqual(self.call("notifications", {"category": "account"})[1]["total"], 1)

		self.assertEqual(self.call("mark_notification_read", {"id": n1}), (200, {"ok": True}))
		self.assertEqual(self.call("notifications", {"unreadOnly": "true"})[1]["total"], 1)
		self.assertEqual(self.call("mark_all_notifications_read")[1], {"ok": True, "updated": 1})
		self.assertEqual(self.call("dashboard")[1]["unreadNotifications"], 0)
		self.assertEqual(self.call("dismiss_notification", {"id": n2})[0], 200)
		self.assertEqual(self.call("notifications")[1]["total"], 1)

	def test_cannot_touch_another_partners_notification(self):
		other = self._notify(self.b.user)
		self.assertEqual(self.call("mark_notification_read", {"id": other})[0], 404)
		self.assertEqual(self.call("dismiss_notification", {"id": other})[0], 404)

	def test_preferences_and_dedupe_are_respected(self):
		frappe.db.set_value("GX Partner Profile", self.a.name, "notify_new_customers", 0)
		try:
			self.assertIsNone(notif.notify(self.a.user, "customer", "x"))
			self.assertTrue(notif.notify(self.a.user, "payout", "always sent"))  # can't be turned off
		finally:
			frappe.db.set_value("GX Partner Profile", self.a.name, "notify_new_customers", 1)
		self.assertTrue(notif.notify(self.a.user, "customer", "x", dedupe_key="once"))
		self.assertIsNone(notif.notify(self.a.user, "customer", "x", dedupe_key="once"))

	def test_new_referred_customer_notifies_the_partner(self):
		notif.on_customer_referred(self.trial_later)
		rows = frappe.get_all("GX Notification", {"user": self.a.user}, ["category", "deep_link_id"])
		self.assertEqual(rows, [{"category": "customer", "deep_link_id": self.trial_later.name}])

	def test_trial_reminder_for_trials_ending_in_3_days(self):
		notif.send_trial_reminders()
		notif.send_trial_reminders()  # running twice doesn't duplicate
		rows = frappe.get_all(
			"GX Notification", {"user": self.a.user, "category": "trial"}, pluck="deep_link_id"
		)
		self.assertEqual(rows, [self.trial_soon.name])

	# ------------------------------------------------------------ devices and push

	def test_register_and_unregister_device(self):
		self.assertEqual(
			self.call("register_device", {"token": "fcm-1", "platform": "android", "appVersion": "1.0"})[0],
			200,
		)
		self.assertEqual(self.call("register_device", {"token": "fcm-1", "platform": "android"})[0], 200)
		self.assertEqual(frappe.db.count("GX Device", {"user": self.a.user}), 1)
		self.as_user(self.b.user)  # same phone, other partner logs in: the token moves
		self.call("register_device", {"token": "fcm-1", "platform": "android"})
		self.assertEqual(
			frappe.db.get_value("GX Device", {"token_hash": notif.token_hash("fcm-1")}, "user"), self.b.user
		)
		self.assertEqual(self.call("register_device", {"token": "x", "platform": "windows"})[0], 422)
		self.call("unregister_device", {"token": "fcm-1"})
		self.assertEqual(frappe.db.count("GX Device", {"user": self.b.user}), 0)

	def test_push_is_generic_and_skipped_without_firebase(self):
		self.call("register_device", {"token": "fcm-2", "platform": "android"})
		frappe.db.commit()
		name = self._notify(self.a.user, title="Business paying signed up")
		with patch("requests.post") as post:
			notif.send_push(name)  # no growthx_fcm configured
			post.assert_not_called()

		conf = {"growthx_fcm": {"project_id": "p", "service_account": {"x": 1}}}
		with (
			patch.dict(frappe.local.conf, conf),
			patch.object(notif, "_fcm_access_token", return_value="at"),
			patch("requests.post", return_value=MagicMock(status_code=200)) as post,
		):
			notif.send_push(name)
		message = post.call_args.kwargs["json"]["message"]
		self.assertEqual(message["token"], "fcm-2")
		self.assertNotIn(
			"Business", json.dumps(message["notification"])
		)  # no customer names on the lock screen
		self.assertEqual(message["data"]["notificationId"], name)

	def test_uninstalled_app_token_is_removed(self):
		self.call("register_device", {"token": "fcm-3", "platform": "ios"})
		frappe.db.commit()
		name = self._notify(self.a.user)
		conf = {"growthx_fcm": {"project_id": "p", "service_account": {"x": 1}}}
		with (
			patch.dict(frappe.local.conf, conf),
			patch.object(notif, "_fcm_access_token", return_value="at"),
			patch("requests.post", return_value=MagicMock(status_code=404, text="UNREGISTERED")),
		):
			notif.send_push(name)
		self.assertEqual(frappe.db.count("GX Device", {"user": self.a.user}), 0)

	# ------------------------------------------------------------ profile and preferences

	def test_profile_get_and_update(self):
		body = self.call("profile")[1]
		self.assertEqual(
			(body["email"], body["status"], body["payoutMethod"]), (self.a.user, "pending_approval", None)
		)

		status, body = self.call(
			"update_profile", {"name": "Anil Kumar", "phone": "+919812345678", "city": "Kochi"}
		)
		self.assertEqual((status, body["name"], body["city"]), (200, "Anil Kumar", "Kochi"))
		self.assertEqual(frappe.db.get_value("User", self.a.user, "first_name"), "Anil")

		self.assertEqual(self.call("update_profile", {"referralCode": "MINE"})[0], 422)
		self.assertEqual(self.call("update_profile", {"phone": "call me"})[0], 422)
		self.assertEqual(self.call("update_profile", {})[0], 422)

	def test_notification_preferences(self):
		body = self.call("notification_preferences")[1]
		self.assertEqual(
			body, {"commissions": True, "newCustomers": True, "trialReminders": True, "productUpdates": False}
		)
		status, body = self.call(
			"update_notification_preferences", {"productUpdates": True, "trialReminders": False}
		)
		self.assertEqual((status, body["productUpdates"], body["trialReminders"]), (200, True, False))
		self.assertEqual(self.call("update_notification_preferences", {"productUpdates": "yes"})[0], 422)
		self.assertEqual(self.call("update_notification_preferences", {"payouts": False})[0], 422)
		frappe.db.set_value(
			"GX Partner Profile", self.a.name, {"notify_product_updates": 0, "notify_trial_reminders": 1}
		)
