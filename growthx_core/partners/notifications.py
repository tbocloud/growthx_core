"""Partner notifications: an in-app list (GX Notification) plus a push to the partner's devices.

    notify(user, category, title, body, ...)   create one (respects the partner's preferences)
    on_customer_referred(account)               "New customer" when a referred customer signs up
    send_trial_reminders()                      daily: trials of the partner's customers ending in 3 days

Push goes through Firebase Cloud Messaging (HTTP v1) when `growthx_fcm` is set in site_config:
    {"project_id": "...", "service_account": {...the service-account JSON...}}
Without it, notifications are only in-app. Push text is always generic: no amounts and no customer
names, because it can show on the lock screen. The in-app list has the detail.
"""

import hashlib

import frappe
from frappe.utils import add_days, getdate, now_datetime

FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
TRIAL_REMINDER_DAYS = 3

# Preference field on GX Partner Profile for each category. Categories not listed are always sent
# (payouts and account messages can't be turned off).
PREFERENCE = {
	"commission": "notify_commissions",
	"customer": "notify_new_customers",
	"trial": "notify_trial_reminders",
}

# Generic push text per category (shown on the lock screen).
PUSH_TEXT = {
	"commission": ("GrowthX", "You have a commission update."),
	"payout": ("GrowthX", "You have a payout update."),
	"customer": ("GrowthX", "You have a new customer."),
	"trial": ("GrowthX", "A customer's trial ends soon."),
	"lead": ("GrowthX", "You have a lead update."),
	"account": ("GrowthX", "You have an account update."),
}


def notify(
	user: str,
	category: str,
	title: str,
	body: str | None = None,
	deep_link_type: str | None = None,
	deep_link_id: str | None = None,
	dedupe_key: str | None = None,
) -> str | None:
	"""Create an in-app notification and queue a push. Returns its name, or None if skipped."""
	pref = PREFERENCE.get(category)
	if pref and not frappe.db.get_value("GX Partner Profile", {"user": user}, pref):
		return None
	if dedupe_key and frappe.db.exists("GX Notification", {"dedupe_key": dedupe_key}):
		return None
	doc = frappe.get_doc(
		{
			"doctype": "GX Notification",
			"user": user,
			"category": category,
			"title": title,
			"body": body,
			"deep_link_type": deep_link_type,
			"deep_link_id": deep_link_id,
			"dedupe_key": dedupe_key,
		}
	).insert(ignore_permissions=True)
	frappe.enqueue(
		"growthx_core.partners.notifications.send_push",
		queue="short",
		enqueue_after_commit=True,
		notification=doc.name,
	)
	return doc.name


# ---------------------------------------------------------------- events


def on_customer_referred(account):
	"""Called after a referred customer's account is created."""
	if not account.referred_by:
		return
	user = frappe.db.get_value("GX Partner Profile", account.referred_by, "user")
	if user:
		notify(
			user,
			"customer",
			"New customer",
			f"{account.account_name} signed up with your link.",
			deep_link_type="customer",
			deep_link_id=account.name,
			dedupe_key=f"customer-referred:{account.name}",
		)


def send_trial_reminders():
	"""Daily: tell partners when a referred customer's trial ends in TRIAL_REMINDER_DAYS days."""
	day = getdate(add_days(now_datetime(), TRIAL_REMINDER_DAYS))
	subs = frappe.get_all(
		"GX Subscription",
		filters={"status": "trialing", "trial_ends_at": ("between", [f"{day} 00:00:00", f"{day} 23:59:59"])},
		fields=["name", "account"],
	)
	for sub in subs:
		account = frappe.db.get_value(
			"GX Account", sub.account, ["name", "account_name", "referred_by"], as_dict=True
		)
		if not account or not account.referred_by:
			continue
		user = frappe.db.get_value("GX Partner Profile", account.referred_by, "user")
		if user:
			notify(
				user,
				"trial",
				"Trial ending soon",
				f"{account.account_name}'s trial ends in {TRIAL_REMINDER_DAYS} days.",
				deep_link_type="customer",
				deep_link_id=account.name,
				dedupe_key=f"trial-ending:{sub.name}:{day}",
			)
	frappe.db.commit()


# ---------------------------------------------------------------- devices and push


def token_hash(token: str) -> str:
	return hashlib.sha256(token.encode()).hexdigest()


def send_push(notification: str):
	"""Background job: push one notification to all of the user's devices (if FCM is configured)."""
	conf = frappe.conf.get("growthx_fcm") or {}
	if not conf.get("project_id") or not conf.get("service_account"):
		return  # in-app only until Firebase is set up
	doc = frappe.get_doc("GX Notification", notification)
	title, body = PUSH_TEXT.get(doc.category, PUSH_TEXT["account"])
	data = {"notificationId": doc.name, "category": doc.category}
	if doc.deep_link_type:
		data["deepLinkType"] = doc.deep_link_type
		data["deepLinkId"] = doc.deep_link_id or ""

	access_token = _fcm_access_token(conf["service_account"])
	url = f"https://fcm.googleapis.com/v1/projects/{conf['project_id']}/messages:send"
	for device in frappe.get_all("GX Device", {"user": doc.user}, ["name", "token"]):
		_send_one(url, access_token, device, title, body, data)
	frappe.db.commit()


def _send_one(url, access_token, device, title, body, data):
	import requests

	try:
		response = requests.post(
			url,
			headers={"Authorization": f"Bearer {access_token}"},
			json={
				"message": {
					"token": device.token,
					"notification": {"title": title, "body": body},
					"data": data,
				}
			},
			timeout=10,
		)
	except requests.RequestException as e:
		frappe.log_error(title="GrowthX push: FCM unreachable", message=type(e).__name__)
		return
	if response.status_code == 404 or (response.status_code == 400 and "UNREGISTERED" in response.text):
		frappe.delete_doc("GX Device", device.name, ignore_permissions=True, force=True)  # app uninstalled
	elif response.status_code >= 300:
		frappe.log_error(title="GrowthX push: FCM refused", message=f"HTTP {response.status_code}")


def _fcm_access_token(service_account: dict) -> str:
	from google.auth.transport.requests import Request
	from google.oauth2 import service_account as sa

	credentials = sa.Credentials.from_service_account_info(service_account, scopes=[FCM_SCOPE])
	credentials.refresh(Request())
	return credentials.token
