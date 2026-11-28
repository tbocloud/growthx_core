"""Admin portal tenants: add a plan (admin.create_plan), bill a site on another plan
(admin.assign_plan), terminate a site (admin.terminate_tenant), and the list's origin/plan fields."""

import json
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_months, add_years, getdate, nowdate

from growthx_core.api.v1 import _core, admin
from growthx_core.provisioning import press, runner
from growthx_core.tests import test_locks

PRODUCT = "test-control"
OTHER_PRODUCT = "test-control-other"
PLAN = "test-control-month"
YEARLY = "test-control-year"
LIFETIME = "test-control-lifetime"
OTHER_PLAN = "test-control-other-plan"
EMAIL = "tenant-control@example.com"


def _call(fn, data=None):
	frappe.local.form_dict = frappe._dict(data or {})
	frappe.local.request = None
	frappe.local.request_ip = "127.0.0.1"
	with (
		patch.object(_core, "_idempotency_key", return_value=None),
		patch.object(_core, "_require_auth"),  # the caller is an admin
		patch.object(frappe.db, "rollback"),  # an error response would roll back this test's data
	):
		response = fn()
	if response.status_code >= 500:
		print(
			"ERROR LOG:",
			frappe.get_all("Error Log", fields=["method", "error"], order_by="creation desc", limit=1),
		)
	return response.status_code, json.loads(response.get_data())


def _plan(plan_id, product, name, interval="month", price=999, **kwargs):
	if frappe.db.exists("GX Plan", plan_id):
		return
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
			"trial_days": 7,
			"is_active": 1,
			**kwargs,
		}
	).insert()


class TestAdminTenantControl(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		for slug, name in ((PRODUCT, "Control"), (OTHER_PRODUCT, "Control Other")):
			if not frappe.db.exists("GX Product", slug):
				frappe.get_doc({"doctype": "GX Product", "slug": slug, "product_name": name}).insert()
		_plan(PLAN, PRODUCT, "Monthly")
		_plan(YEARLY, PRODUCT, "Yearly", interval="year", price=9999)
		_plan(LIFETIME, PRODUCT, "Lifetime", interval="one_time", price=150000, gst_included=1)
		_plan(OTHER_PLAN, OTHER_PRODUCT, "Monthly")
		self._billing_prerequisites()

	def _billing_prerequisites(self):
		"""What `seed.plan_items` needs to make a plan's Item (a fresh test bench has neither)."""
		if not frappe.db.exists("Item Group", "All Item Groups"):
			frappe.get_doc(
				{"doctype": "Item Group", "item_group_name": "All Item Groups", "is_group": 1}
			).insert()
		if not frappe.db.exists("Item Group", "Services"):
			frappe.get_doc(
				{
					"doctype": "Item Group",
					"item_group_name": "Services",
					"parent_item_group": "All Item Groups",
					"is_group": 0,
				}
			).insert()
		if not frappe.db.exists("UOM", "Nos"):
			frappe.get_doc({"doctype": "UOM", "uom_name": "Nos"}).insert()

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()

	# ------------------------------------------------------------ fixtures

	def _account(self):
		email = f"control-{frappe.generate_hash(length=6)}@example.com"
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": "Control",
				"user_type": "Website User",
				"send_welcome_email": 0,
			}
		).insert()
		return frappe.get_doc(
			{
				"doctype": "GX Account",
				"account_name": "Control Co",
				"user": user.name,
				"contact_email": email,
				"status": "active",
			}
		).insert()

	def _tenant(
		self, origin="provisioned", plan=PLAN, status="ready", press_site_id="tenant-control.tbogrowthx.com"
	):
		account = self._account()
		plan_doc = frappe.get_doc("GX Plan", plan)
		sub = frappe.get_doc(
			{
				"doctype": "GX Subscription",
				"account": account.name,
				"plan": plan_doc.name,
				"product": plan_doc.product,
				"status": "active",
				"tenant": None,
				"billing_interval": plan_doc.billing_interval,
				"price": plan_doc.price,
				"gst_included": plan_doc.gst_included,
				"gst_rate": plan_doc.gst_rate,
				"current_period_end": add_months(nowdate(), 1),
			}
		).insert()
		tenant = frappe.get_doc(
			{
				"doctype": "GX Tenant",
				"account": account.name,
				"product": plan_doc.product,
				"origin": origin,
				"status": status,
				"display_name": "Control Co",
				"subdomain": f"control-{frappe.generate_hash(length=6)}",
				"site_url": f"https://{press_site_id}" if press_site_id else None,
				"press_site_id": press_site_id,
				"subscription": sub.name,
			}
		).insert()
		frappe.db.set_value("GX Subscription", sub.name, "tenant", tenant.name)
		return tenant.name

	# ------------------------------------------------------------ create_plan

	def test_a_new_plan_goes_on_sale_with_a_billing_item(self):
		status, body = _call(
			admin.create_plan,
			{
				"product": PRODUCT,
				"planName": "Growth",
				"interval": "month",
				"price": 149900,
				"trialDays": 14,
				"gstRate": 18,
				"features": "1 company\nUnlimited invoices",
			},
		)
		self.assertEqual(status, 201)
		self.assertEqual(body["id"], "test-control-growth")
		self.assertEqual(body["price"]["amount"], 149900)
		self.assertEqual(body["interval"], "month")
		self.assertEqual(body["trialDays"], 14)
		self.assertTrue(body["active"])
		self.assertEqual(body["features"], ["1 company", "Unlimited invoices"])
		self.assertEqual(body["item"], "GX-TEST-CONTROL-GROWTH")

		plan = frappe.get_doc("GX Plan", "test-control-growth")
		self.assertEqual(plan.price, 1499)
		self.assertTrue(plan.item)
		self.assertTrue(frappe.db.exists("Item", plan.item))
		self.assertTrue(
			frappe.db.exists("GX Audit Log", {"action": "plan.create", "reference_name": plan.name})
		)

	def test_the_plan_id_can_be_chosen_and_a_used_one_is_refused(self):
		status, body = _call(
			admin.create_plan,
			{
				"id": "growthx-growth",
				"product": PRODUCT,
				"planName": "Growth",
				"interval": "month",
				"price": 1000,
			},
		)
		self.assertEqual(status, 201)
		self.assertEqual(body["id"], "growthx-growth")
		status, body = _call(
			admin.create_plan,
			{
				"id": "growthx-growth",
				"product": PRODUCT,
				"planName": "Growth 2",
				"interval": "month",
				"price": 1000,
			},
		)
		self.assertEqual(status, 409)
		self.assertEqual(body["error"]["code"], "plan_exists")

	def test_a_plan_needs_a_known_app_and_a_price(self):
		status, body = _call(
			admin.create_plan,
			{"product": "no-such-app", "planName": "Growth", "interval": "month", "price": 1000},
		)
		self.assertEqual(status, 422)
		self.assertIn("product", body["error"]["fields"])
		status, body = _call(
			admin.create_plan, {"product": PRODUCT, "planName": "Growth", "interval": "month", "price": 0}
		)
		self.assertEqual(status, 422)
		self.assertIn("price", body["error"]["fields"])

	# ------------------------------------------------------------ assign_plan

	def test_assigning_a_plan_changes_the_subscription(self):
		tenant_id = self._tenant()
		status, body = _call(admin.assign_plan, {"id": tenant_id, "plan": YEARLY})
		self.assertEqual(status, 200)
		self.assertEqual(body["plan"], YEARLY)
		self.assertEqual(body["planName"], "Yearly")

		sub = frappe.get_doc("GX Subscription", frappe.db.get_value("GX Tenant", tenant_id, "subscription"))
		self.assertEqual(sub.plan, YEARLY)
		self.assertEqual(sub.billing_interval, "year")
		self.assertEqual(sub.price, 9999)
		self.assertEqual(getdate(sub.current_period_end), getdate(add_years(add_months(nowdate(), 1), 1)))
		self.assertTrue(
			frappe.db.exists("GX Audit Log", {"action": "tenant.assign_plan", "reference_name": sub.name})
		)

	def test_assign_plan_locks_the_account_in_a_fresh_transaction(self):
		tenant_id = self._tenant()
		sub = frappe.db.get_value("GX Tenant", tenant_id, "subscription")
		writes = frappe.db.transaction_writes
		with test_locks.lock_spy(admin) as calls:
			status, body = _call(admin.assign_plan, {"id": tenant_id, "plan": YEARLY})
		self.assertEqual(status, 200, body)
		# before any write
		self.assertEqual(calls, [(frappe.db.get_value("GX Subscription", sub, "account"), writes)])

	def test_assign_plan_checks_the_site_again_after_the_lock(self):
		"""The renewal job may archive the site while this request waits for the account lock."""
		tenant_id = self._tenant()
		real = admin.lock_account

		def archived_meanwhile(name):
			frappe.db.set_value("GX Tenant", tenant_id, "status", "archived")
			return real(name)

		with patch.object(admin, "lock_account", side_effect=archived_meanwhile):
			status, body = _call(admin.assign_plan, {"id": tenant_id, "plan": YEARLY})
		self.assertEqual((status, body["error"]["code"]), (409, "tenant_archived"), body)

	def test_a_lifetime_plan_has_no_period_end(self):
		tenant_id = self._tenant()
		status, _body = _call(admin.assign_plan, {"id": tenant_id, "plan": LIFETIME})
		self.assertEqual(status, 200)
		sub = frappe.get_doc("GX Subscription", frappe.db.get_value("GX Tenant", tenant_id, "subscription"))
		self.assertIsNone(sub.current_period_end)

	def test_a_plan_of_another_app_is_refused(self):
		tenant_id = self._tenant()
		status, body = _call(admin.assign_plan, {"id": tenant_id, "plan": OTHER_PLAN})
		self.assertEqual(status, 422)
		self.assertIn("plan", body["error"]["fields"])

	def test_a_terminated_site_cannot_change_plan(self):
		tenant_id = self._tenant(status="archived")
		status, body = _call(admin.assign_plan, {"id": tenant_id, "plan": YEARLY})
		self.assertEqual(status, 409)
		self.assertEqual(body["error"]["code"], "tenant_archived")

	def test_an_inactive_plan_is_refused(self):
		tenant_id = self._tenant()
		frappe.db.set_value("GX Plan", YEARLY, "is_active", 0)
		status, body = _call(admin.assign_plan, {"id": tenant_id, "plan": YEARLY})
		self.assertEqual(status, 422)
		self.assertIn("plan", body["error"]["fields"])

	# ------------------------------------------------------------ terminate_tenant

	def _archiving_client(self):
		client = press.FakePressClient()
		client.archived = []
		client.archive_site = client.archived.append
		return client

	def test_terminating_a_provisioned_site_archives_it_on_press_and_cancels_billing(self):
		tenant_id = self._tenant()
		client = self._archiving_client()
		with (
			patch.object(runner, "get_client", return_value=client),
			patch.object(frappe.db, "commit"),  # runner.archive commits: it would keep this test's records
		):
			status, body = _call(admin.terminate_tenant, {"id": tenant_id})
		self.assertEqual(status, 200)
		self.assertEqual(body["status"], "archived")
		self.assertEqual(client.archived, ["tenant-control.tbogrowthx.com"])
		sub = frappe.get_doc("GX Subscription", frappe.db.get_value("GX Tenant", tenant_id, "subscription"))
		self.assertEqual(sub.status, "canceled")
		self.assertTrue(
			frappe.db.exists("GX Audit Log", {"action": "tenant.terminate", "reference_name": tenant_id})
		)

	def test_terminating_an_imported_site_leaves_the_site_alone(self):
		tenant_id = self._tenant(origin="imported")
		client = self._archiving_client()
		with patch.object(runner, "get_client", return_value=client):
			status, body = _call(admin.terminate_tenant, {"id": tenant_id})
		self.assertEqual(status, 200)
		self.assertEqual(body["status"], "archived")
		self.assertEqual(client.archived, [])

	def test_a_terminated_site_is_refused_again(self):
		tenant_id = self._tenant(status="archived")
		frappe.db.set_value("GX Subscription", self._subscription(tenant_id), "status", "canceled")
		status, body = _call(admin.terminate_tenant, {"id": tenant_id})
		self.assertEqual(status, 409)
		self.assertEqual(body["error"]["code"], "already_archived")

	def test_terminating_an_archived_site_whose_billing_still_runs_stops_it(self):
		"""A terminate that stopped after runner.archive's commit (e.g. a lock wait timeout on the account
		again) left the site archived and its subscription live: the retry stops the billing, without asking
		Press again, instead of refusing it as already archived."""
		tenant_id = self._tenant(status="archived")
		client = self._archiving_client()
		with patch.object(runner, "get_client", return_value=client), patch.object(frappe.db, "commit"):
			status, body = _call(admin.terminate_tenant, {"id": tenant_id})
		self.assertEqual(status, 200, body)
		self.assertEqual(body["status"], "archived")
		self.assertEqual(client.archived, [])
		self.assertEqual(
			frappe.db.get_value("GX Subscription", self._subscription(tenant_id), "status"), "canceled"
		)
		self.assertEqual(self._account_status(tenant_id), "churned")
		details = frappe.db.get_value(
			"GX Audit Log", {"action": "tenant.terminate", "reference_name": tenant_id}, "details"
		)
		self.assertIn("billing stopped", details)

	def test_a_site_still_being_set_up_cannot_be_terminated(self):
		tenant_id = self._tenant(status="provisioning")
		status, body = _call(admin.terminate_tenant, {"id": tenant_id})
		self.assertEqual(status, 409)
		self.assertEqual(body["error"]["code"], "invalid_transition")

	def _terminate(self, tenant_id):
		"""terminate_tenant with Press archiving; runner.archive's commit is kept out of the test."""
		with (
			patch.object(runner, "get_client", return_value=self._archiving_client()),
			patch.object(frappe.db, "commit"),
		):
			return _call(admin.terminate_tenant, {"id": tenant_id})

	def _subscription(self, tenant_id):
		return frappe.db.get_value("GX Tenant", tenant_id, "subscription")

	def _second_site(self, tenant_id, press_site_id="tenant-control-2.tbogrowthx.com"):
		"""Another live site, on another plan, for the same customer."""
		account = frappe.db.get_value("GX Tenant", tenant_id, "account")
		plan = frappe.get_doc("GX Plan", OTHER_PLAN)
		sub = frappe.get_doc(
			{
				"doctype": "GX Subscription",
				"account": account,
				"plan": plan.name,
				"product": plan.product,
				"status": "active",
				"billing_interval": plan.billing_interval,
				"price": plan.price,
				"current_period_end": add_months(nowdate(), 1),
			}
		).insert()
		tenant = frappe.get_doc(
			{
				"doctype": "GX Tenant",
				"account": account,
				"product": plan.product,
				"origin": "provisioned",
				"status": "ready",
				"display_name": "Control Co",
				"subdomain": f"control-{frappe.generate_hash(length=6)}",
				"site_url": f"https://{press_site_id}",
				"press_site_id": press_site_id,
				"subscription": sub.name,
			}
		).insert()
		frappe.db.set_value("GX Subscription", sub.name, "tenant", tenant.name)
		return tenant.name

	def _account_status(self, tenant_id):
		return frappe.db.get_value(
			"GX Account", frappe.db.get_value("GX Tenant", tenant_id, "account"), "status"
		)

	def test_terminating_a_customers_only_site_churns_the_account(self):
		"""The cancel is a set_value, which skips the subscription's on_update: the account must still follow
		its subscriptions, as after the renewal job's archive (F6)."""
		for origin in ("provisioned", "imported"):
			with self.subTest(origin):
				tenant_id = self._tenant(origin=origin)
				self.assertEqual(self._account_status(tenant_id), "active")
				status, body = self._terminate(tenant_id)
				self.assertEqual(status, 200, body)
				self.assertEqual(self._account_status(tenant_id), "churned")

	def test_terminating_one_site_leaves_an_account_with_another_live_plan_as_it_is(self):
		tenant_id = self._tenant()
		account = frappe.db.get_value("GX Tenant", tenant_id, "account")
		plan = frappe.get_doc("GX Plan", OTHER_PLAN)
		frappe.get_doc(
			{
				"doctype": "GX Subscription",
				"account": account,
				"plan": plan.name,
				"product": plan.product,
				"status": "active",
				"billing_interval": plan.billing_interval,
				"price": plan.price,
				"current_period_end": add_months(nowdate(), 1),
			}
		).insert()
		status, body = self._terminate(tenant_id)
		self.assertEqual(status, 200, body)
		self.assertEqual(
			frappe.db.get_value(
				"GX Subscription", frappe.db.get_value("GX Tenant", tenant_id, "subscription"), "status"
			),
			"canceled",
		)
		self.assertEqual(self._account_status(tenant_id), "active")

	def test_terminating_a_site_of_a_suspended_customer_keeps_the_suspension(self):
		"""An admin's Suspend moves the account and its sites but leaves the subscriptions active. Terminating
		one site must not turn the account active from the other one's subscription: that site stays
		suspended, and Reactivate (only from suspended) must still bring it back."""
		first = self._tenant()
		second = self._second_site(first)
		account = frappe.db.get_value("GX Tenant", first, "account")
		with patch.object(runner, "get_client", return_value=self._archiving_client()):
			status, body = _call(
				admin.update_customer_status, {"id": account, "action": "suspend", "reason": "Chargeback"}
			)
		self.assertEqual(status, 200, body)
		self.assertEqual(frappe.db.get_value("GX Tenant", second, "status"), "suspended")
		status, body = self._terminate(first)
		self.assertEqual(status, 200, body)
		self.assertEqual(self._account_status(first), "suspended")
		with patch.object(runner, "get_client", return_value=self._archiving_client()):
			status, body = _call(admin.update_customer_status, {"id": account, "action": "reactivate"})
		self.assertEqual(status, 200, body)
		self.assertEqual(self._account_status(first), "active")
		self.assertEqual(frappe.db.get_value("GX Tenant", second, "status"), "ready")

	def test_terminating_the_last_site_of_a_suspended_customer_churns_it(self):
		tenant_id = self._tenant()
		account = frappe.db.get_value("GX Tenant", tenant_id, "account")
		with patch.object(runner, "get_client", return_value=self._archiving_client()):
			status, body = _call(
				admin.update_customer_status, {"id": account, "action": "suspend", "reason": "Chargeback"}
			)
		self.assertEqual(status, 200, body)
		status, body = self._terminate(tenant_id)
		self.assertEqual(status, 200, body)
		self.assertEqual(self._account_status(tenant_id), "churned")

	def test_terminate_locks_the_account_first_and_again_after_the_archive(self):
		"""Account first, the order payments and the renewal job lock in: before any write, in a fresh
		transaction, and again after runner.archive's commit (which releases it) before the subscription and
		the account are written."""
		tenant_id = self._tenant()
		account = frappe.db.get_value("GX Tenant", tenant_id, "account")
		events = []
		real_get_value, real_set_value = frappe.db.get_value, frappe.db.set_value

		def get_value(*args, **kwargs):
			if kwargs.get("for_update"):
				events.append(("lock", args[0]))
			return real_get_value(*args, **kwargs)

		def set_value(*args, **kwargs):
			events.append(("write", args[0]))
			return real_set_value(*args, **kwargs)

		writes = frappe.db.transaction_writes
		with (
			test_locks.lock_spy(admin) as calls,
			patch.object(runner, "get_client", return_value=self._archiving_client()),
			patch.object(frappe.db, "commit", side_effect=lambda *a, **k: events.append(("commit", None))),
			patch.object(frappe.db, "get_value", side_effect=get_value),
			patch.object(frappe.db, "set_value", side_effect=set_value),
		):
			status, body = _call(admin.terminate_tenant, {"id": tenant_id})
		self.assertEqual(status, 200, body)
		self.assertEqual(calls, [(account, writes)])  # before any write
		after_commit = events[events.index(("commit", None)) + 1 :]
		self.assertEqual(after_commit[:2], [("lock", "GX Account"), ("write", "GX Subscription")], events)

	def test_terminate_checks_the_site_again_after_the_lock(self):
		"""The renewal job may archive the site while this request waits for the account lock."""
		tenant_id = self._tenant()
		real = admin.lock_account

		def archived_meanwhile(name):
			# the job's archive_trial: the site archived and its subscription canceled
			frappe.db.set_value("GX Tenant", tenant_id, "status", "archived")
			frappe.db.set_value("GX Subscription", self._subscription(tenant_id), "status", "canceled")
			return real(name)

		client = self._archiving_client()
		with (
			patch.object(admin, "lock_account", side_effect=archived_meanwhile),
			patch.object(runner, "get_client", return_value=client),
		):
			status, body = _call(admin.terminate_tenant, {"id": tenant_id})
		self.assertEqual((status, body["error"]["code"]), (409, "already_archived"), body)
		self.assertEqual(client.archived, [])

	# ------------------------------------------------------------ the list

	def test_the_list_says_which_kind_of_site_it_is_and_its_plan(self):
		self._tenant()
		self._tenant(origin="imported", plan=OTHER_PLAN, press_site_id="imported-control.tbogrowthx.com")
		status, body = _call(admin.tenants, {"pageSize": 20})
		self.assertEqual(status, 200)
		origins = {item["origin"]: item for item in body["items"]}
		self.assertEqual(set(origins), {"provisioned", "imported"})
		self.assertEqual(origins["provisioned"]["plan"], PLAN)
		self.assertEqual(origins["provisioned"]["planName"], "Monthly")
		self.assertEqual(origins["imported"]["plan"], OTHER_PLAN)
