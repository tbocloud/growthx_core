"""ERPNext Direct onboarding: admin.add_direct_site, admin.send_payment_link, and what a direct customer
can and can't do afterwards (pay the assigned plan only, plan change, termination)."""

import json
import re
import unittest
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, nowdate

from growthx_core import accounts, direct
from growthx_core.api.v1 import _core, admin, admin_direct, auth, billing, customer
from growthx_core.billing import billing_details, payu
from growthx_core.provisioning import press, runner

PRODUCT = "erpnext-direct-test"
OTHER_PRODUCT = "test-direct-other"
PLAN = "erpnext-direct-test-month"
CHEAP = "erpnext-direct-test-cheap"
LIFETIME = "erpnext-direct-test-lifetime"
OTHER_PLAN = "test-direct-other-month"
IC = "india_compliance" in frappe.get_installed_apps()
SITE = "acme-erp.example.com"
KERALA = {
	"addressLine1": "1 Test Street",
	"addressLine2": None,
	"city": "Kochi",
	"state": "Kerala",
	"pincode": "682001",
}


def _call(fn, data=None, auth_patched=True, token=True):
	frappe.local.form_dict = frappe._dict(data or {})
	frappe.local.request = None
	frappe.local.request_ip = "127.0.0.1"
	frappe.local.gx_token = "token" if (token and not auth_patched) else None
	patches = [
		patch.object(_core, "_idempotency_key", return_value=None),
		patch.object(_core, "_check_rate_limit"),
		patch.object(frappe.db, "rollback"),  # an error response would roll back this test's data
	]
	if auth_patched:
		patches.append(patch.object(_core, "_require_auth"))
	for p in patches:
		p.start()
	try:
		response = fn()
	finally:
		for p in reversed(patches):
			p.stop()
	return response.status_code, json.loads(response.get_data())


def _plan(plan_id, product, name, interval="month", price=1000, **kwargs):
	frappe.get_doc(
		{
			"doctype": "GX Plan",
			"plan_id": plan_id,
			"product": product,
			"plan_name": name,
			"billing_interval": interval,
			"price": price,
			"gst_included": 0,
			"gst_rate": 18,
			"is_active": 1,
			**kwargs,
		}
	).insert()


class DirectCase(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		for slug in (PRODUCT, OTHER_PRODUCT):
			frappe.get_doc(
				{
					"doctype": "GX Product",
					"slug": slug,
					"product_name": slug,
					"availability": "hidden" if slug == PRODUCT else "available",
				}
			).insert()
		_plan(PLAN, PRODUCT, "Direct Monthly", price=1000)
		_plan(CHEAP, PRODUCT, "Direct Cheap", price=1)
		_plan(LIFETIME, PRODUCT, "Direct Lifetime", interval="one_time", price=150000, gst_included=1)
		_plan(OTHER_PLAN, OTHER_PRODUCT, "Other Monthly")
		self.mail = patch.object(frappe, "sendmail")
		self.sendmail = self.mail.start()
		self.press = press.FakePressClient()
		self.press_patch = patch.object(runner, "get_client", return_value=self.press)
		self.press_patch.start()

	def tearDown(self):
		self.press_patch.stop()
		self.mail.stop()
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def _add(self, **extra):
		data = {
			"company": "Acme Traders",
			"contactName": "Asha Nair",
			"email": "asha@acme-traders.example.com",
			"phone": "9800000000",
			"site": SITE,
			"plan": PLAN,
			**extra,
		}
		return _call(admin.add_direct_site, data)

	def _tenant(self, body):
		return frappe.get_doc("GX Tenant", body["tenant"]["id"])

	def _as_customer(self, email):
		frappe.set_user(frappe.db.get_value("User", {"email": email}, "name"))


def _reset_key(sendmail) -> str:
	"""The set-password key in the last email sent."""
	html = sendmail.call_args.kwargs["message"]
	return re.search(r"reset-password\?key=([0-9a-f]+)", html).group(1)


class TestAddDirectSite(DirectCase):
	def test_only_admins_can_use_the_endpoints(self):
		self._add()  # a customer exists to be the caller
		self._as_customer("asha@acme-traders.example.com")
		for fn in (admin.add_direct_site, admin.send_payment_link):
			self.assertEqual(_call(fn, {"site": SITE}, auth_patched=False)[0], 403, fn.__name__)
		frappe.set_user("Guest")
		for fn in (admin.add_direct_site, admin.send_payment_link):
			self.assertEqual(_call(fn, {"site": SITE}, auth_patched=False, token=False)[0], 401, fn.__name__)

	def test_adds_a_new_customer_a_subscription_and_an_untouched_site(self):
		status, body = self._add(site="HTTPS://Acme-ERP.Example.com/login?x=1")
		self.assertEqual(status, 201, body)
		self.assertEqual((body["created"], body["accountCreated"], body["inviteSent"]), (True, True, True))
		self.assertEqual((body["pressCheck"], body["warnings"]), ("confirmed", ["plan_not_payable_online"]))

		tenant = self._tenant(body)
		self.assertEqual(
			(tenant.origin, tenant.status, tenant.site_url), ("imported", "ready", f"https://{SITE}")
		)
		self.assertEqual(tenant.product, PRODUCT)
		# No Press id: Core can never suspend, archive or configure this site.
		self.assertFalse(tenant.press_site_id)
		self.assertFalse(tenant.get("apps"))
		self.assertFalse(tenant.get("steps"))
		self.assertEqual(body["tenant"]["origin"], "imported")
		self.assertEqual(body["tenant"]["subscriptionStatus"], "past_due")

		sub = frappe.get_doc("GX Subscription", tenant.subscription)
		self.assertEqual((sub.status, sub.plan, sub.price, sub.tenant), ("past_due", PLAN, 1000, tenant.name))
		self.assertEqual(
			(sub.billing_interval, sub.trial_ends_at, sub.current_period_end), ("month", None, None)
		)
		account = frappe.get_doc("GX Account", tenant.account)
		self.assertEqual(
			(account.account_name, account.contact_email), ("Acme Traders", "asha@acme-traders.example.com")
		)
		self.assertTrue(account.customer)
		self.assertEqual(body["customer"]["id"], account.name)
		self.assertIn("GrowthX Customer", frappe.get_roles(account.user))
		self.assertFalse(accounts.has_password(account.user))
		log = frappe.get_last_doc("GX Audit Log", {"action": "tenant.add_direct_site"})
		self.assertIn(SITE, log.details)
		self.assertIn("first invoice due now", log.details)

	def test_the_invite_sets_a_password_once_and_only_for_that_email(self):
		_, body = self._add()
		self.assertEqual(self.sendmail.call_args.kwargs["recipients"], ["asha@acme-traders.example.com"])
		user = frappe.db.get_value("GX Account", body["customer"]["id"], "user")
		key = _reset_key(self.sendmail)
		strong = "Sturdy-Pass-2026-xk"
		status, result = _call(auth.reset_password, {"key": key, "newPassword": strong})
		self.assertEqual(status, 200, result)
		self.assertTrue(accounts.has_password(user))
		# single use
		self.assertEqual(_call(auth.reset_password, {"key": key, "newPassword": strong + "1"})[0], 400)

	def test_a_plan_without_gst_included_warns_that_online_payment_wont_work(self):
		status, body = self._add()
		self.assertEqual(status, 201, body)
		self.assertIn("plan_not_payable_online", body["warnings"])
		frappe.db.set_value("GX Plan", LIFETIME, "gst_included", 1)
		_, ok = self._add(site="inc.example.com", email="inc@example.com", phone="9833333333", plan=LIFETIME)
		self.assertNotIn("plan_not_payable_online", ok["warnings"])

	def test_the_invite_failing_does_not_fail_the_onboarding(self):
		self.sendmail.side_effect = Exception("no outgoing email account")
		status, body = self._add()
		self.assertEqual(status, 201, body)
		self.assertFalse(body["inviteSent"])
		self.assertIn("invite_not_sent", body["warnings"])

	def test_a_site_already_added_is_refused_for_another_customer(self):
		self._add()
		status, body = self._add(email="other@else.example.com", company="Else Co")
		self.assertEqual((status, body["error"]["code"]), (409, "site_exists"))
		self.assertIsNone(frappe.db.exists("User", "other@else.example.com"))
		# also by its custom domain
		frappe.db.set_value(
			"GX Tenant", {"site_url": f"https://{SITE}"}, "custom_domain", "erp.acme.example.com"
		)
		status, body = self._add(site="erp.acme.example.com", email="o2@else.example.com")
		self.assertEqual((status, body["error"]["code"]), (409, "site_exists"))

	def test_a_growthx_provisioned_site_is_refused_too(self):
		account = self._add()[1]["customer"]["id"]
		frappe.get_doc(
			{
				"doctype": "GX Tenant",
				"account": account,
				"product": OTHER_PRODUCT,
				"site_url": "https://theirs.tbogrowthx.com",
				"press_site_id": "theirs.tbogrowthx.com",
				"status": "ready",
				"subdomain": "theirs-site",
			}
		).insert(ignore_permissions=True, ignore_mandatory=True)
		status, body = self._add(site="theirs.tbogrowthx.com", email="o3@else.example.com")
		self.assertEqual((status, body["error"]["code"]), (409, "site_exists"))

	def test_sending_the_same_request_again_returns_the_same_tenant(self):
		_, first = self._add()
		status, second = self._add()
		self.assertEqual((status, second["created"]), (200, False))
		self.assertEqual(second["tenant"]["id"], first["tenant"]["id"])
		self.assertEqual(frappe.db.count("GX Tenant", {"site_url": f"https://{SITE}"}), 1)
		self.assertEqual(self.sendmail.call_count, 1)

	def test_a_terminated_site_can_be_added_again(self):
		_, first = self._add()
		_call(admin.terminate_tenant, {"id": first["tenant"]["id"]})
		status, body = self._add(email="new-owner@acme-traders.example.com", phone="9811111111")
		self.assertEqual(status, 201, body)
		self.assertNotEqual(body["tenant"]["id"], first["tenant"]["id"])

	def test_the_plan_must_be_an_active_direct_plan(self):
		status, body = self._add(plan=OTHER_PLAN)
		self.assertEqual((status, list(body["error"]["fields"])), (422, ["plan"]))
		self.assertIn("ERPNext Direct", body["error"]["fields"]["plan"])
		self.assertEqual(self._add(plan="nope")[0], 422)
		frappe.db.set_value("GX Plan", PLAN, "is_active", 0)
		self.assertEqual(self._add()[0], 422)
		self.assertFalse(frappe.db.exists("GX Account", {"contact_email": "asha@acme-traders.example.com"}))

	def test_input_is_validated(self):
		for bad in ("", "not a domain", "localhost", "a.local", "http://", "x" * 300 + ".com", 12):
			status, body = self._add(site=bad)
			self.assertEqual((status, list(body["error"]["fields"])), (422, ["site"]), bad)
		self.assertEqual(list(self._add(email="nope")[1]["error"]["fields"]), ["email"])
		self.assertEqual(self._add(company="")[0], 422)
		self.assertEqual(self._add(contactName="")[0], 422)
		self.assertEqual(self._add(billingStart="soon")[0], 422)
		self.assertEqual(self._add(billingStart=add_days(nowdate(), 400))[0], 422)
		self.assertEqual(self._add(confirmOnPress="yes")[0], 422)
		self.assertEqual(self._add(site=frappe.local.site)[0], 422)
		self.assertEqual(frappe.db.count("GX Tenant", {"product": PRODUCT}), 0)

	def test_an_existing_customer_is_reused_without_a_new_invite(self):
		user, _ = accounts.create_customer_account(
			email="asha@acme-traders.example.com",
			full_name="Asha Nair",
			company="Acme Traders",
			password="Long-Enough-Pass-91",
		)
		account = frappe.db.get_value("GX Account", {"user": user})
		status, body = self._add(company="A different name", contactName="Someone Else")
		self.assertEqual(status, 201, body)
		self.assertEqual((body["accountCreated"], body["inviteSent"]), (False, None))
		self.assertEqual(body["customer"]["id"], account)
		self.assertEqual(frappe.db.get_value("GX Account", account, "account_name"), "Acme Traders")
		self.assertEqual(frappe.db.count("GX Account", {"user": user}), 1)
		self.assertEqual(self._tenant(body).account, account)
		self.sendmail.assert_not_called()

		# one direct site per account and product
		status, body = self._add(site="second.acme.example.com")
		self.assertEqual((status, body["error"]["code"]), (409, "already_added"))

	def _existing_customer(self) -> str:
		user, _ = accounts.create_customer_account(
			email="asha@acme-traders.example.com",
			full_name="Asha Nair",
			company="Acme Traders",
			password="Long-Enough-Pass-91",
		)
		return frappe.db.get_value("GX Account", {"user": user})

	def test_an_existing_customer_is_locked_in_a_fresh_transaction(self):
		account = self._existing_customer()
		with patch.object(admin_direct, "lock_account", wraps=admin_direct.lock_account) as spy:
			status, body = self._add()
		self.assertEqual(status, 201, body)
		spy.assert_called_once_with(account)

	def test_a_suspended_customer_gets_no_new_site(self):
		"""Checked under the account lock, so a Suspend that this request waited for counts too."""
		account = self._existing_customer()
		frappe.db.set_value("GX Account", account, "status", "suspended")
		subscriptions = frappe.db.count("GX Subscription", {"account": account})
		status, body = self._add()
		self.assertEqual(
			(status, body["error"]["code"], body["error"]["message"]),
			(409, "customer_suspended", "This customer is suspended. Reactivate them first."),
		)
		self.assertFalse(frappe.db.exists("GX Tenant", {"account": account}))
		self.assertEqual(frappe.db.count("GX Subscription", {"account": account}), subscriptions)
		self.sendmail.assert_not_called()

	def test_an_email_of_another_kind_of_user_is_never_attached(self):
		frappe.get_doc(
			{"doctype": "User", "email": "boss@example.com", "first_name": "Boss", "send_welcome_email": 0}
		).insert(ignore_permissions=True).add_roles("GrowthX Admin")
		status, body = self._add(email="boss@example.com")
		self.assertEqual((status, body["error"]["code"]), (409, "email_in_use"))
		accounts.create_partner_account(
			email="partner@example.com", full_name="Pat Ner", password="Long-Enough-Pass-91"
		)
		self.assertEqual(self._add(email="partner@example.com")[1]["error"]["code"], "email_in_use")
		self.assertFalse(frappe.db.exists("GX Tenant", {"product": PRODUCT}))
		self.sendmail.assert_not_called()

	def test_a_closed_account_email_can_be_used_again(self):
		first = self._add()[1]
		user = frappe.db.get_value("GX Account", first["customer"]["id"], "user")
		_call(admin.terminate_tenant, {"id": first["tenant"]["id"]})
		frappe.db.set_value("GX Account", first["customer"]["id"], "status", "churned")
		frappe.db.set_value("User", user, "enabled", 0)
		status, body = self._add(site="reopened.acme.example.com")
		self.assertEqual(status, 201, body)
		self.assertEqual((body["accountCreated"], body["customer"]["id"]), (True, first["customer"]["id"]))
		self.assertTrue(frappe.db.get_value("User", user, "enabled"))

	def test_an_enabled_churned_account_is_refused(self):
		first = self._add()[1]
		frappe.db.set_value("GX Account", first["customer"]["id"], "status", "churned")
		status, body = self._add(site="again.acme.example.com")
		self.assertEqual((status, body["error"]["code"]), (409, "email_in_use"))

	def test_already_paid_offline_makes_the_subscription_active(self):
		status, body = self._add(paidOffline={"reference": "UTR-1", "amount": 118000, "paidOn": nowdate()})
		self.assertEqual(status, 201, body)
		sub = frappe.get_doc("GX Subscription", self._tenant(body).subscription)
		self.assertEqual((sub.status, sub.gateway, sub.last_payment_id), ("active", "manual", "UTR-1"))
		self.assertTrue(sub.current_period_end)
		self.assertEqual(frappe.db.get_value("GX Account", sub.account, "status"), "active")
		self.assertEqual(body["tenant"]["subscriptionStatus"], "active")
		self.assertTrue(
			frappe.db.exists(
				"GX Audit Log", {"action": "customer.record_payment", "reference_name": sub.account}
			)
		)
		log = frappe.get_last_doc("GX Audit Log", {"action": "tenant.add_direct_site"})
		self.assertIn("already paid offline", log.details)

	def test_a_lifetime_plan_paid_offline_has_no_end(self):
		status, body = self._add(
			plan=LIFETIME, paidOffline={"reference": "UTR-2", "amount": 150000, "paidOn": nowdate()}
		)
		self.assertEqual(status, 201, body)
		sub = frappe.get_doc("GX Subscription", self._tenant(body).subscription)
		self.assertEqual((sub.status, sub.current_period_end), ("active", None))

	def test_offline_payment_input_and_start_date_conflicts(self):
		paid = {"reference": "UTR-3", "amount": 1000, "paidOn": nowdate()}
		self.assertEqual(self._add(paidOffline={**paid, "amount": 0})[0], 422)
		self.assertEqual(self._add(paidOffline={**paid, "reference": ""})[0], 422)
		self.assertEqual(self._add(paidOffline={**paid, "paidOn": "2999-01-01"})[0], 422)
		self.assertEqual(self._add(paidOffline="yes")[0], 422)
		self.assertEqual(self._add(paidOffline=paid, billingStart=add_days(nowdate(), 5))[0], 422)

	def test_a_later_billing_start_waits_as_trialing(self):
		start = add_days(nowdate(), 20)
		status, body = self._add(billingStart=start)
		self.assertEqual(status, 201, body)
		sub = frappe.get_doc("GX Subscription", self._tenant(body).subscription)
		self.assertEqual(sub.status, "trialing")
		self.assertEqual(sub.trial_ends_at.date(), date.fromisoformat(start))
		log = frappe.get_last_doc("GX Audit Log", {"action": "tenant.add_direct_site"})
		self.assertIn(f"billing starts {start}", log.details)
		# a date that is today means now
		_, today = self._add(
			site="b.example.com", email="b@example.com", phone="9822222222", billingStart=nowdate()
		)
		self.assertEqual(
			frappe.db.get_value("GX Subscription", self._tenant(today).subscription, "status"), "past_due"
		)

	def test_press_must_have_the_site_when_it_can_say(self):
		self.press.site_names = {"other.example.com"}
		status, body = self._add()
		self.assertEqual((status, list(body["error"]["fields"])), (422, ["site"]))
		self.assertFalse(frappe.db.exists("GX Account", {"contact_email": "asha@acme-traders.example.com"}))
		# the admin may add it without the Press check (e.g. a custom domain)
		status, body = self._add(confirmOnPress=False)
		self.assertEqual((status, body["pressCheck"]), (201, "not_checked"))

	def test_press_being_unreachable_allows_it_with_a_warning(self):
		client = MagicMock()
		client.site_exists.side_effect = press.PressError("Press unreachable")
		with patch.object(runner, "get_client", return_value=client):
			status, body = self._add()
		self.assertEqual((status, body["pressCheck"]), (201, "unreachable"))
		self.assertIn("press_unreachable", body["warnings"])
		client.site_exists.assert_called_once_with(SITE)
		client.list_sites.assert_not_called()  # one exact lookup, never a listing

	def test_a_press_that_cannot_look_up_is_skipped(self):
		client = MagicMock()
		client.site_exists.side_effect = NotImplementedError
		with patch.object(runner, "get_client", return_value=client):
			self.assertEqual(self._add()[1]["pressCheck"], "skipped")

	def test_nothing_is_ever_provisioned_or_sent_to_press(self):
		client = MagicMock()
		client.site_exists.return_value = True
		with (
			patch.object(runner, "get_client", return_value=client),
			patch.object(runner, "enqueue") as enqueue,
		):
			_, body = self._add()
			_call(admin.terminate_tenant, {"id": body["tenant"]["id"]})
		enqueue.assert_not_called()
		for name in (
			"create_site",
			"archive_site",
			"suspend_site",
			"resume_site",
			"install_app",
			"configure_site",
		):
			getattr(client, name).assert_not_called()

	@unittest.skipUnless(IC, "billing details need India Compliance (CI installs it)")
	def test_the_billing_address_and_gstin_are_saved_for_the_tax_invoice(self):
		status, body = self._add(billingAddress=KERALA, gstin="32AABCK1234M1ZT")
		self.assertEqual(status, 201, body)
		details = billing_details.get(frappe.get_doc("GX Account", body["customer"]["id"]))
		self.assertEqual(
			(details["complete"], details["gstin"], details["address"]["city"]),
			(True, "32AABCK1234M1ZT", "Kochi"),
		)
		# a GSTIN without an address, or a bad one, is refused and nothing is left behind
		status, body = self._add(site="c.example.com", email="c@example.com", gstin="32AABCK1234M1ZT")
		self.assertEqual((status, list(body["error"]["fields"])), (422, ["billingAddress"]))
		bad = self._add(site="d.example.com", email="d@example.com", billingAddress=KERALA, gstin="bad")
		self.assertEqual(bad[0], 422)


class TestDirectBilling(DirectCase):
	"""After onboarding: what the customer and the admin can do with the subscription."""

	def setUp(self):
		super().setUp()
		_, self.body = self._add()
		self.tenant = self._tenant(self.body)
		self.account = self.tenant.account
		self.email = "asha@acme-traders.example.com"

	def _checkout(self, plan=PLAN, user_email=None):
		self._as_customer(user_email or self.email)
		try:
			return _call(billing.create_checkout, {"planId": plan})
		finally:
			frappe.set_user("Administrator")

	def test_the_customer_sees_the_site_and_opens_its_own_login(self):
		self._as_customer(self.email)
		status, sites = _call(customer.sites)
		self.assertEqual(
			(status, [(r["id"], r["origin"]) for r in sites["items"]]),
			(200, [(self.tenant.name, "imported")]),
		)
		status, opened = _call(customer.open_site, {"id": self.tenant.name})
		self.assertEqual((status, opened["url"]), (200, f"https://{SITE}/login"))
		status, subs = _call(customer.subscriptions)
		self.assertEqual(
			(subs["items"][0]["status"], subs["items"][0]["planName"]), ("past_due", "Direct Monthly")
		)

	def test_a_direct_customer_cannot_pay_another_plan_of_the_product(self):
		for plan in (CHEAP, LIFETIME):
			status, body = self._checkout(plan)
			self.assertEqual((status, body["error"]["code"]), (422, "invalid_plan"), plan)

	def test_the_assigned_plan_is_the_one_checked_out_after_a_plan_change(self):
		_call(admin.assign_plan, {"id": self.tenant.name, "plan": CHEAP})
		self.assertEqual(self._checkout(PLAN)[1]["error"]["code"], "invalid_plan")
		# the assigned plan passes the plan checks (it then needs PayU, which a bare test site lacks)
		_, body = self._checkout(CHEAP)
		self.assertNotEqual(body.get("error", {}).get("code"), "invalid_plan", body)

	def test_a_customer_can_only_pay_their_own_subscription(self):
		accounts.create_customer_account(
			email="stranger@example.com",
			full_name="Stranger",
			company="Stranger Co",
			password="Long-Enough-Pass-91",
		)
		status, body = self._checkout(PLAN, "stranger@example.com")
		self.assertEqual((status, list(body["error"]["fields"])), (422, ["planId"]))
		# the stranger's request left the direct customer's subscription as it was
		self.assertEqual(
			frappe.db.get_value("GX Subscription", self.tenant.subscription, "status"), "past_due"
		)

	def test_billing_that_was_terminated_cannot_be_paid(self):
		_call(admin.terminate_tenant, {"id": self.tenant.name})
		status, body = self._checkout()
		self.assertEqual((status, body["error"]["code"]), (409, "invalid_transition"))

	def test_assign_plan_moves_the_unpaid_subscription_without_a_period(self):
		status, body = _call(admin.assign_plan, {"id": self.tenant.name, "plan": LIFETIME})
		self.assertEqual((status, body["plan"], body["subscriptionStatus"]), (200, LIFETIME, "past_due"))
		sub = frappe.get_doc("GX Subscription", self.tenant.subscription)
		self.assertEqual(
			(sub.plan, sub.price, sub.billing_interval, sub.current_period_end),
			(LIFETIME, 150000, "one_time", None),
		)
		self.assertEqual(_call(admin.assign_plan, {"id": self.tenant.name, "plan": CHEAP})[0], 200)
		self.assertIsNone(
			frappe.db.get_value("GX Subscription", self.tenant.subscription, "current_period_end")
		)
		# another app's plan is still refused
		self.assertEqual(_call(admin.assign_plan, {"id": self.tenant.name, "plan": OTHER_PLAN})[0], 422)

	def test_terminating_stops_billing_and_leaves_the_site_alone(self):
		client = MagicMock()
		with patch.object(runner, "get_client", return_value=client):
			status, body = _call(admin.terminate_tenant, {"id": self.tenant.name})
			self.assertEqual(
				(status, body["status"], body["subscriptionStatus"]), (200, "archived", "canceled")
			)
			again = _call(admin.terminate_tenant, {"id": self.tenant.name})[1]
			self.assertEqual(again["error"]["code"], "already_archived")
		client.archive_site.assert_not_called()
		log = frappe.get_last_doc("GX Audit Log", {"action": "tenant.terminate"})
		self.assertIn("the site itself is untouched", log.details)

	def test_even_with_a_press_id_an_imported_site_is_never_archived_or_suspended(self):
		from growthx_core.billing import payments

		frappe.db.set_value("GX Tenant", self.tenant.name, "press_site_id", SITE)
		client = MagicMock()
		with patch.object(runner, "get_client", return_value=client), patch.object(frappe.db, "commit"):
			payments.set_customer_status(
				frappe.get_doc("GX Account", self.account), "suspended", "ready", "suspended", "suspend_site"
			)
			runner.archive(self.tenant.name)
		client.suspend_site.assert_not_called()
		client.archive_site.assert_not_called()

	def test_a_customer_deleting_their_account_does_not_delete_the_site(self):
		client = MagicMock()
		frappe.db.set_value("GX Tenant", self.tenant.name, "press_site_id", SITE)
		self._as_customer(self.email)
		with (
			patch.object(runner, "get_client", return_value=client),
			patch.object(customer.tokens, "revoke_all"),
			patch.object(frappe.db, "commit"),  # runner.archive commits
		):
			status, body = _call(customer.delete_account, {"confirm": self.email})
		frappe.set_user("Administrator")
		self.assertEqual(status, 200, body)
		client.archive_site.assert_not_called()

	def test_send_payment_link_emails_the_billing_page(self):
		self.sendmail.reset_mock()
		status, body = _call(admin.send_payment_link, {"id": self.tenant.name})
		self.assertEqual((status, body), (200, {"sent": True, "to": self.email}))
		kwargs = self.sendmail.call_args.kwargs
		self.assertEqual(kwargs["recipients"], [self.email])
		self.assertIn("/customer/billing", kwargs["message"])
		self.assertIn("reset-password?key=", kwargs["message"])  # no password yet
		self.assertTrue(
			frappe.db.exists(
				"GX Audit Log", {"action": "tenant.send_payment_link", "reference_name": self.tenant.name}
			)
		)

		accounts.mark_has_password(frappe.db.get_value("GX Account", self.account, "user"))
		_call(admin.send_payment_link, {"id": self.tenant.name})
		self.assertNotIn("reset-password", self.sendmail.call_args.kwargs["message"])

	def test_send_payment_link_refusals(self):
		self.assertEqual(_call(admin.send_payment_link, {"id": "nope"})[0], 404)
		self.sendmail.side_effect = Exception("smtp down")
		status, body = _call(admin.send_payment_link, {"id": self.tenant.name})
		self.assertEqual((status, body["error"]["code"]), (502, "email_failed"))
		self.sendmail.side_effect = None

		def code():
			return _call(admin.send_payment_link, {"id": self.tenant.name})[1]["error"]["code"]

		frappe.db.set_value("GX Subscription", self.tenant.subscription, "status", "canceled")
		self.assertEqual(code(), "no_subscription")
		frappe.db.set_value(
			"GX Subscription", self.tenant.subscription, {"status": "active", "billing_interval": "one_time"}
		)
		self.assertEqual(code(), "nothing_due")
		frappe.db.set_value("GX Tenant", self.tenant.name, "status", "archived")
		self.assertEqual(code(), "tenant_archived")


class TestDirectHelpers(unittest.TestCase):
	def test_direct_products(self):
		self.assertTrue(direct.is_direct_product("erpnext-direct"))
		self.assertTrue(direct.is_direct_product("erpnext-direct-enterprise"))
		for other in ("erpnext-directory", "laundry", "", None):
			self.assertFalse(direct.is_direct_product(other))


if IC:
	from growthx_core.tests import test_checkout_payu as pay_helpers

	ITEM = "GX-TEST-DIRECT-ITEM"

	class TestDirectPaysWithPayU(DirectCase):
		"""The whole route: the admin adds the site, the customer pays the invoice on PayU, the plan is active."""

		@classmethod
		def setUpClass(cls):
			super().setUpClass()
			frappe.set_user("Administrator")
			pay_helpers.ensure_erpnext_company()
			pay_helpers.ensure_gst_company()

		def setUp(self):
			super().setUp()
			frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": ITEM,
					"item_name": "Direct Test Monthly",
					"item_group": "Services",
					"stock_uom": "Nos",
					"is_stock_item": 0,
					**pay_helpers.item_gst(),
				}
			).insert()
			# Online invoices need GST-inclusive prices
			frappe.db.set_value("GX Plan", PLAN, {"item": ITEM, "gst_included": 1})
			self.patches = [
				patch.dict(frappe.local.conf, {"growthx_company": pay_helpers.COMPANY}),
				patch.object(payu, "gateway_account", return_value="PayU-test - INR - GXCT"),
				patch.object(payu, "start_attempt", return_value=pay_helpers.FORM),
			]
			for p in self.patches:
				p.start()

		def tearDown(self):
			for p in reversed(self.patches):
				p.stop()
			super().tearDown()

		def test_add_the_site_then_the_customer_pays_and_the_plan_is_active(self):
			status, body = self._add(billingAddress=KERALA)
			self.assertEqual(status, 201, body)
			tenant = self._tenant(body)
			self._as_customer("asha@acme-traders.example.com")
			status, checkout = _call(billing.create_checkout, {"planId": PLAN})
			frappe.set_user("Administrator")
			self.assertEqual(status, 201, checkout)
			self.assertEqual(checkout["amount"]["amount"], 100000)  # 1,000 incl. GST, in paise
			self.assertEqual(checkout["redirect"]["url"], pay_helpers.FORM["url"])

			invoice = frappe.db.get_value("GX Checkout", checkout["reference"], "sales_invoice")
			from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

			pe = get_payment_entry("Sales Invoice", invoice)
			pe.reference_no = "PAYU-DIRECT"
			pe.reference_date = nowdate()
			pe.insert()
			pe.submit()

			sub = frappe.get_doc("GX Subscription", tenant.subscription)
			self.assertEqual((sub.status, sub.gateway), ("active", "payu"))
			self.assertEqual(frappe.db.get_value("GX Checkout", checkout["reference"], "status"), "paid")
			self.assertEqual(frappe.db.get_value("GX Account", tenant.account, "status"), "active")
			self.assertIsNone(frappe.db.get_value("GX Tenant", tenant.name, "press_site_id"))
