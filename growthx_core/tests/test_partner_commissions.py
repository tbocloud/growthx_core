"""Partner commissions and payouts: tier choice, one commission per payment, approval and the hold
period, void and refund, the payout run (minimum, TDS rounding, no double inclusion), marking a
payout paid, the partner's own numbers, and who may call what."""

import json
from datetime import date
from decimal import Decimal
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, nowdate

from growthx_core import accounts
from growthx_core.api.v1 import _core, admin, partner
from growthx_core.billing import payments
from growthx_core.partners import commissions as ops
from growthx_core.partners import program

PREFIX = "gx-comm-test"
PRODUCT = f"{PREFIX}-product"
PLAN = f"{PREFIX}-plan"
PASSWORD = "Long-Enough-Pass-91"
THIS_MONTH = nowdate()[:7]


def _call(module, fn, data=None, as_admin=True):
	frappe.local.form_dict = frappe._dict(data or {})
	frappe.local.request = None
	frappe.local.request_ip = "127.0.0.1"
	patches = [
		patch.object(_core, "_idempotency_key", return_value=None),
		patch.object(_core, "_check_rate_limit"),
		patch.object(frappe.db, "rollback"),  # an error response would roll back this test's data
	]
	if as_admin:
		patches.append(patch.object(_core, "_require_auth"))
	for p in patches:
		p.start()
	try:
		response = getattr(module, fn)()
	finally:
		for p in patches:
			p.stop()
	return response.status_code, json.loads(response.get_data())


class TestPartnerCommissions(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		frappe.get_doc(
			{"doctype": "GX Product", "slug": PRODUCT, "product_name": "Test", "availability": "available"}
		).insert()
		self.plan = frappe.get_doc(
			{
				"doctype": "GX Plan",
				"plan_id": PLAN,
				"product": PRODUCT,
				"plan_name": "Test Growth",
				"billing_interval": "month",
				"price": 1180,
				"gst_included": 1,
				"gst_rate": 18,
				"is_active": 1,
			}
		).insert()
		self.partner = self._partner("a")
		self.other = self._partner("b")
		self.customer = self._customer("one", self.partner)
		self.mail = patch("frappe.sendmail")
		self.sendmail = self.mail.start()

	def tearDown(self):
		self.mail.stop()
		frappe.set_user("Administrator")
		frappe.db.rollback()

	# ------------------------------------------------------------ fixtures

	def _partner(self, key):
		user = accounts.create_partner_account(
			email=f"{PREFIX}-{key}@example.com", full_name=f"Partner {key}", password=PASSWORD
		)
		name = frappe.db.get_value("GX Partner Profile", {"user": user})
		doc = frappe.get_doc("GX Partner Profile", name)
		doc.status = "active"
		doc.save()
		return name

	def _customer(self, key, partner_name):
		code = frappe.db.get_value("GX Partner Profile", partner_name, "referral_code")
		user, _ = accounts.create_customer_account(
			email=f"{PREFIX}-cust-{key}@example.com",
			full_name="Cust Omer",
			company=f"Customer {key}",
			password=PASSWORD,
			referral_code=code,
		)
		account = frappe.db.get_value("GX Account", {"user": user})
		frappe.get_doc(
			{
				"doctype": "GX Subscription",
				"account": account,
				"plan": PLAN,
				"product": PRODUCT,
				"status": "trialing",
				"price": 1180,
				"gst_included": 1,
				"gst_rate": 18,
			}
		).insert(ignore_permissions=True)
		return account

	def _pay(self, reference, account=None, gross=None, paid_on=None):
		account = frappe.get_doc("GX Account", account or self.customer)
		return ops.record_for_payment(
			account, self.plan, reference, paid_on or date.today(), gross_amount=gross, gateway="manual"
		)

	def _commission(self, amount, status="approved", partner_name=None, paid_on=None, key=None):
		doc = frappe.get_doc(
			{
				"doctype": "GX Commission",
				"partner": partner_name or self.partner,
				"account": self.customer,
				"status": status,
				"commission_type": "recurring",
				"paid_on": paid_on or add_days(nowdate(), -1),
				"invoice_amount": 1,
				"rate": 20,
				"amount": amount,
				"payment_key": key or frappe.generate_hash(length=20),
			}
		).insert(ignore_permissions=True)
		return doc.name

	def _row(self, name):
		return frappe.db.get_value("GX Commission", name, ["status", "payout", "amount"], as_dict=True)

	def _as_partner(self, partner_name):
		frappe.set_user(frappe.db.get_value("GX Partner Profile", partner_name, "user"))
		frappe.local.gx_token = "test-token"

	# ------------------------------------------------------------ pure rules

	def test_money_is_whole_paise_half_up(self):
		self.assertEqual(ops.to_paise("1234.565"), 123457)
		self.assertEqual(ops.net_of_gst_paise(118000, 18), 100000)
		self.assertEqual(ops.net_of_gst_paise(100, 18), 85)  # 84.745... rounds up
		self.assertEqual(ops.net_of_gst_paise(500, 0), 500)
		self.assertEqual(ops.percent_of(100005, 20), 20001)  # 20001.0
		self.assertEqual(ops.percent_of(12345, "2.5"), 309)  # 308.625 -> 309
		self.assertEqual(ops.percent_of(1, 50), 1)  # 0.5 -> 1, not banker's 0

	def test_tds_is_taken_once_per_payout_and_rounds_half_up(self):
		# 24.6914 -> 24.69; 12345 paise x 2 % = 246.9 -> 247 paise
		self.assertEqual(ops.payout_totals([123457], 2), (123457, 2469, 120988))
		self.assertEqual(ops.tds_paise(12345, 2), 247)
		# Per line would be 1+1 paise; on the total 0.04 -> 0: the payout total decides.
		self.assertEqual(ops.payout_totals([1, 1], 2), (2, 0, 2))
		self.assertEqual(ops.payout_totals([25000, 25000], 0), (50000, 0, 50000))

	def test_tier_is_chosen_by_active_clients(self):
		tiers = program.settings()["tiers"]
		got = {n: program.tier_for(n, tiers) for n in (0, 9, 10, 29, 30, 500)}
		self.assertEqual(
			got,
			{
				0: ("Seed", Decimal(20)),
				9: ("Seed", Decimal(20)),
				10: ("Sprout", Decimal(25)),
				29: ("Sprout", Decimal(25)),
				30: ("Canopy", Decimal(30)),
				500: ("Canopy", Decimal(30)),
			},
		)

	def test_next_payout_date(self):
		self.assertEqual(program.next_payout_date(date(2026, 11, 15), 15), date(2026, 11, 15))
		self.assertEqual(program.next_payout_date(date(2026, 11, 16), 15), date(2026, 12, 15))
		self.assertEqual(program.next_payout_date(date(2026, 12, 20), 15), date(2027, 1, 15))

	def test_settings_defaults_and_changes(self):
		s = program.settings()
		self.assertEqual(
			(s["minimum_payout"], s["payout_day"], s["tds_rate"], s["hold_days"]),
			(Decimal(1000), 15, Decimal(2), 0),
		)
		doc = frappe.get_single(program.SETTINGS)
		doc.tds_rate = 10
		doc.payout_day = 5
		doc.save()
		s = program.settings()
		self.assertEqual((s["tds_rate"], s["payout_day"]), (Decimal(10), 5))
		doc.payout_day = 31
		self.assertRaises(frappe.ValidationError, doc.save)

	def test_settings_tiers_can_be_changed(self):
		doc = frappe.get_single(program.SETTINGS)
		doc.set("tiers", [])
		doc.append("tiers", {"tier_name": "Base", "min_active_clients": 0, "rate": 10})
		doc.append("tiers", {"tier_name": "Top", "min_active_clients": 2, "rate": 40})
		doc.save()
		self.assertEqual(program.tier_for(2, program.settings()["tiers"]), ("Top", Decimal(40)))
		doc.tiers[0].min_active_clients = 1
		self.assertRaises(frappe.ValidationError, doc.save)  # must start at 0

	# ------------------------------------------------------------ creating commissions

	def test_a_payment_earns_the_partner_a_commission_once(self):
		name = self._pay("PAY-1")
		c = frappe.get_doc("GX Commission", name)
		# Rs 1,180 paid incl. 18 % GST -> Rs 1,000 commissionable, Seed 20 % -> Rs 200
		self.assertEqual(
			(c.partner, c.account, c.status, c.invoice_amount, c.rate, c.amount, c.tier),
			(self.partner, self.customer, "pending", 1000, 20, 200, "Seed"),
		)
		self.assertEqual((c.commission_type, c.gross_amount, c.plan), ("first_month", 1180, PLAN))
		self.assertEqual(self._pay("PAY-1"), name)  # the same payment again changes nothing
		self.assertEqual(frappe.db.count("GX Commission", {"account": self.customer}), 1)
		second = self._pay("PAY-2")
		self.assertNotEqual(second, name)
		self.assertEqual(frappe.db.get_value("GX Commission", second, "commission_type"), "recurring")

	def test_the_amount_paid_is_what_counts(self):
		# A discounted payment of Rs 590 incl. GST -> Rs 500 net -> Rs 100
		c = frappe.get_doc("GX Commission", self._pay("PAY-HALF", gross=590))
		self.assertEqual((c.invoice_amount, c.amount), (500, 100))

	def test_no_commission_without_a_partner(self):
		user, _ = accounts.create_customer_account(
			email=f"{PREFIX}-direct@example.com", full_name="Cust Omer", company="Direct", password=PASSWORD
		)
		account = frappe.db.get_value("GX Account", {"user": user})
		self.assertIsNone(self._pay("PAY-D", account=account))
		self.assertEqual(frappe.db.count("GX Commission", {"account": account}), 0)

	def test_rate_follows_the_tier_at_the_time_of_payment(self):
		for clients, rate in ((0, 20), (10, 25), (30, 30)):
			with patch.object(ops, "active_clients", return_value=clients):
				c = frappe.get_doc("GX Commission", self._pay(f"PAY-T{clients}"))
			self.assertEqual((c.rate, c.amount, c.active_clients), (rate, 1000 * rate / 100, clients))
		self.assertEqual(frappe.db.get_value("GX Partner Profile", self.partner, "tier"), "Canopy")

	def test_active_clients_counts_the_partners_active_accounts(self):
		self.assertEqual(ops.active_clients(self.partner), 0)
		frappe.db.set_value("GX Account", self.customer, "status", "active")
		self.assertEqual(ops.active_clients(self.partner), 1)
		self.assertEqual(ops.active_clients(self.other), 0)

	def test_apply_payment_records_the_commission(self):
		account = frappe.get_doc("GX Account", self.customer)
		payments.apply_payment(account, self.plan, "UTR-77", date.today(), "manual", amount=1180)
		c = frappe.get_all("GX Commission", {"account": self.customer}, ["amount", "gateway", "status"])
		self.assertEqual([(x.amount, x.gateway, x.status) for x in c], [(200, "manual", "pending")])
		# The customer counts as active at that moment: still Seed, but the account is active.
		self.assertEqual(frappe.db.get_value("GX Account", self.customer, "status"), "active")

	def test_record_payment_endpoint_creates_it_and_a_repeat_does_not_double_it(self):
		data = {
			"id": self.customer,
			"plan": PLAN,
			"amount": 118000,
			"reference": "UTR-ADMIN",
			"paidOn": nowdate(),
		}
		self.assertEqual(_call(admin, "record_payment", data)[0], 200)
		self.assertEqual(_call(admin, "record_payment", data)[0], 409)  # duplicate_payment
		rows = frappe.get_all("GX Commission", {"account": self.customer}, ["amount", "invoice_amount"])
		self.assertEqual([(r.invoice_amount, r.amount) for r in rows], [(1000, 200)])

	def test_a_failure_in_the_ledger_never_blocks_the_payment(self):
		account = frappe.get_doc("GX Account", self.customer)
		with patch.object(ops, "_record", side_effect=RuntimeError("boom")):
			payments.apply_payment(account, self.plan, "UTR-X", date.today(), "manual", amount=1180)
		self.assertEqual(
			frappe.db.get_value("GX Subscription", {"account": self.customer}, "status"), "active"
		)

	# ------------------------------------------------------------ approve and void

	def test_approve_and_hold_period(self):
		doc = frappe.get_single(program.SETTINGS)
		doc.hold_days = 5
		doc.save()
		held = self._pay("PAY-HOLD")
		self.assertEqual(
			str(frappe.db.get_value("GX Commission", held, "approvable_from")), add_days(nowdate(), 5)
		)
		status, body = _call(admin, "approve_commissions", {"ids": [held]})
		self.assertEqual(
			(status, body["updated"], body["skipped"]), (200, 0, [{"id": held, "reason": "on_hold"}])
		)
		frappe.db.set_value("GX Commission", held, "approvable_from", nowdate())
		status, body = _call(admin, "approve_commissions", {"ids": [held, "GXC-NOPE"]})
		self.assertEqual((body["updated"], body["skipped"]), (1, [{"id": "GXC-NOPE", "reason": "not_found"}]))
		self.assertEqual(self._row(held).status, "approved")
		self.assertEqual(
			frappe.db.get_value(
				"GX Audit Log", {"action": "commission.approve", "reference_name": held}, "name"
			)
			is not None,
			True,
		)
		# approving again does nothing
		_, body = _call(admin, "approve_commissions", {"ids": [held]})
		self.assertEqual(body["skipped"], [{"id": held, "reason": "already_approved"}])

	def test_approving_tells_the_partner(self):
		name = self._pay("PAY-N")
		_call(admin, "approve_commissions", {"ids": [name]})
		user = frappe.db.get_value("GX Partner Profile", self.partner, "user")
		titles = frappe.get_all("GX Notification", {"user": user, "category": "commission"}, pluck="title")
		self.assertIn("Commission approved", titles)
		self.assertIn("Commission earned", titles)

	def test_void_needs_a_reason_and_skips_paid(self):
		pending, paid = self._commission(100, "pending"), self._commission(100, "paid")
		self.assertEqual(_call(admin, "void_commissions", {"ids": [pending]})[0], 422)
		status, body = _call(admin, "void_commissions", {"ids": [pending, paid], "reason": "Refunded"})
		self.assertEqual(
			(status, body["ids"], body["skipped"]), (200, [pending], [{"id": paid, "reason": "already_paid"}])
		)
		self.assertEqual(self._row(pending).status, "void")
		self.assertEqual(self._row(paid).status, "paid")

	def test_bulk_ids_are_validated(self):
		for ids in (None, [], "x", [1], ["a"] * 0, [f"GXC-{i}" for i in range(201)]):
			self.assertEqual(_call(admin, "approve_commissions", {"ids": ids})[0], 422, ids)

	def test_refund_voids_the_commission_unless_already_paid(self):
		name = self._pay("PAY-REF")
		self.assertEqual(ops.void_for_payment(self.customer, "PAY-REF", "refund"), [name])
		self.assertEqual(self._row(name).status, "void")
		paid = self._pay("PAY-REF2")
		frappe.db.set_value("GX Commission", paid, "status", "paid")
		self.assertEqual(ops.void_for_payment(self.customer, "PAY-REF2", "refund"), [])
		self.assertEqual(self._row(paid).status, "paid")
		self.assertEqual(ops.void_for_payment(self.customer, "never-paid", "refund"), [])

	def test_a_voided_payment_is_not_commissioned_again(self):
		name = self._pay("PAY-V")
		ops.void([name], "refund")
		self.assertEqual(self._pay("PAY-V"), name)  # the same payment keeps its (void) row
		self.assertEqual(self._row(name).status, "void")

	# ------------------------------------------------------------ payouts

	def test_payout_run_respects_the_minimum_and_carries_the_rest(self):
		big, small = self._commission(1000), self._commission(999.99, partner_name=self.other)
		status, body = _call(admin, "create_payouts", {"period": THIS_MONTH})
		self.assertEqual(status, 200, body)
		self.assertEqual([p["partnerId"] for p in body["created"]], [self.partner])
		self.assertEqual(
			body["skipped"],
			[
				{
					"partner": self.other,
					"reason": "below_minimum",
					"amount": {"amount": 99999, "currency": "INR"},
				}
			],
		)
		self.assertTrue(self._row(big).payout)
		self.assertIsNone(self._row(small).payout)

	def test_payout_math_and_audit(self):
		self._commission(600.01)
		self._commission(634.56)  # 1234.57 gross
		status, body = _call(admin, "create_payouts", {"period": THIS_MONTH})
		p = body["created"][0]
		self.assertEqual(
			(p["status"], p["commissionCount"], p["gross"]["amount"], p["tds"]["amount"], p["net"]["amount"]),
			("draft", 2, 123457, 2469, 120988),
		)
		self.assertEqual(p["tdsRate"], 0.02)
		self.assertEqual(p["period"], THIS_MONTH)
		self.assertTrue(
			frappe.db.exists("GX Audit Log", {"action": "payout.create", "reference_name": p["id"]})
		)

	def test_running_the_payout_twice_never_includes_a_commission_twice(self):
		self._commission(1500)
		self.assertEqual(len(_call(admin, "create_payouts", {"period": THIS_MONTH})[1]["created"]), 1)
		again = _call(admin, "create_payouts", {"period": THIS_MONTH})[1]
		self.assertEqual((again["created"], again["skipped"]), ([], []))
		self.assertEqual(frappe.db.count("GX Payout", {"partner": self.partner}), 1)

	def test_only_approved_commissions_up_to_the_period_are_included(self):
		pending, void_ = self._commission(2000, "pending"), self._commission(2000, "void")
		late = self._commission(1000, paid_on=add_days(nowdate(), 40))
		ok = self._commission(1000, paid_on="2026-10-02")
		body = _call(admin, "create_payouts", {"period": "2026-10"})[1]
		self.assertEqual([p["commissionCount"] for p in body["created"]], [1])
		self.assertTrue(self._row(ok).payout)
		for name in (pending, void_, late):
			self.assertFalse(self._row(name).payout, name)

	def test_a_suspended_partner_gets_no_payout(self):
		self._commission(5000)
		frappe.db.set_value("GX Partner Profile", self.partner, "status", "suspended")
		body = _call(admin, "create_payouts", {"period": THIS_MONTH})[1]
		self.assertEqual((body["created"], body["skipped"][0]["reason"]), ([], "partner_not_active"))

	def test_payout_period_is_validated(self):
		for period in (None, "2026", "2026-13", "Nov 2026", "2999-01"):
			self.assertEqual(_call(admin, "create_payouts", {"period": period})[0], 422, period)

	def test_the_minimum_payout_is_a_setting(self):
		doc = frappe.get_single(program.SETTINGS)
		doc.minimum_payout = 100
		doc.save()
		self._commission(150)
		self.assertEqual(len(_call(admin, "create_payouts", {"period": THIS_MONTH})[1]["created"]), 1)

	def _draft(self, *amounts):
		for a in amounts:
			self._commission(a)
		return _call(admin, "create_payouts", {"period": THIS_MONTH})[1]["created"][0]["id"]

	def test_marking_a_payout_paid_pays_its_commissions_and_tells_the_partner(self):
		payout = self._draft(700, 800)
		data = {"id": payout, "reference": "UTR998877", "paidOn": nowdate(), "method": "bank_transfer"}
		status, body = _call(admin, "mark_payout_paid", data)
		self.assertEqual(
			(status, body["status"], body["reference"], body["method"]),
			(200, "paid", "UTR998877", "bank_transfer"),
		)
		self.assertEqual(
			{r.status for r in frappe.get_all("GX Commission", {"payout": payout}, ["status"])}, {"paid"}
		)
		doc = frappe.get_doc("GX Payout", payout)
		self.assertEqual((doc.status, str(doc.paid_on), doc.paid_by), ("paid", nowdate(), "Administrator"))
		user = frappe.db.get_value("GX Partner Profile", self.partner, "user")
		self.assertTrue(frappe.db.exists("GX Notification", {"user": user, "category": "payout"}))
		self.assertEqual(self.sendmail.call_count, 1)
		self.assertIn("1,470.00", self.sendmail.call_args.kwargs["message"])  # 1500 - 2 % TDS
		self.assertTrue(
			frappe.db.exists("GX Audit Log", {"action": "payout.mark_paid", "reference_name": payout})
		)

	def test_a_payout_cannot_be_paid_twice(self):
		payout = self._draft(1200)
		data = {"id": payout, "reference": "UTR1", "paidOn": nowdate(), "method": "upi"}
		self.assertEqual(_call(admin, "mark_payout_paid", data)[0], 200)
		status, body = _call(admin, "mark_payout_paid", {**data, "reference": "UTR2"})
		self.assertEqual((status, body["error"]["code"]), (409, "not_draft"))
		self.assertEqual(frappe.db.get_value("GX Payout", payout, "reference"), "UTR1")
		self.assertEqual(frappe.db.count("GX Notification", {"category": "payout"}), 1)

	def test_mark_paid_validates_and_404s(self):
		payout = self._draft(1200)
		ok = {"id": payout, "reference": "UTR1", "paidOn": nowdate(), "method": "upi"}
		for bad in (
			{"reference": ""},
			{"paidOn": "01/11/2026"},
			{"paidOn": add_days(nowdate(), 3)},
			{"method": "cash"},
			{"method": None},
		):
			self.assertEqual(_call(admin, "mark_payout_paid", {**ok, **bad})[0], 422, bad)
		self.assertEqual(_call(admin, "mark_payout_paid", {**ok, "id": "GXPO-NOPE"})[0], 404)
		self.assertEqual(frappe.db.get_value("GX Payout", payout, "status"), "draft")

	def test_no_email_when_the_partner_opted_out_but_still_notified(self):
		frappe.db.set_value("GX Partner Profile", self.partner, "notify_commissions", 0)
		payout = self._draft(1200)
		_call(
			admin,
			"mark_payout_paid",
			{"id": payout, "reference": "U", "paidOn": nowdate(), "method": "other"},
		)
		self.sendmail.assert_not_called()
		user = frappe.db.get_value("GX Partner Profile", self.partner, "user")
		self.assertTrue(frappe.db.exists("GX Notification", {"user": user, "category": "payout"}))

	def test_cancelling_a_draft_frees_its_commissions(self):
		payout = self._draft(1200)
		status, body = _call(admin, "cancel_payout", {"id": payout})
		self.assertEqual((status, body["status"]), (200, "cancelled"))
		self.assertEqual(frappe.db.count("GX Commission", {"payout": payout}), 0)
		self.assertEqual(len(_call(admin, "create_payouts", {"period": THIS_MONTH})[1]["created"]), 1)
		paid = self._draft(1300)
		_call(admin, "mark_payout_paid", {"id": paid, "reference": "U", "paidOn": nowdate(), "method": "upi"})
		self.assertEqual(_call(admin, "cancel_payout", {"id": paid})[1]["error"]["code"], "not_draft")

	def test_voiding_a_commission_in_a_draft_payout_recalculates_it(self):
		a, b = self._commission(700), self._commission(800)
		payout = _call(admin, "create_payouts", {"period": THIS_MONTH})[1]["created"][0]["id"]
		_call(admin, "void_commissions", {"ids": [a], "reason": "Refund"})
		doc = frappe.get_doc("GX Payout", payout)
		self.assertEqual(
			(doc.status, doc.commission_count, doc.gross, doc.tds, doc.net), ("draft", 1, 800, 16, 784)
		)
		_call(admin, "void_commissions", {"ids": [b], "reason": "Refund"})
		self.assertEqual(frappe.db.get_value("GX Payout", payout, "status"), "cancelled")

	# ------------------------------------------------------------ admin lists

	def test_admin_lists_filter_by_partner_status_and_month(self):
		a = self._commission(100, "pending", paid_on="2026-09-10")
		b = self._commission(200, "approved", partner_name=self.other, paid_on="2026-10-10")
		status, body = _call(admin, "commissions", {"partner": self.partner})
		self.assertEqual([i["id"] for i in body["items"]], [a])
		self.assertEqual(body["items"][0]["status"], "pending")
		_, body = _call(admin, "commissions", {"status": "approved", "partner": self.other})
		self.assertEqual([i["id"] for i in body["items"]], [b])
		_, body = _call(admin, "commissions", {"month": "2026-10", "partner": self.other})
		self.assertEqual(body["total"], 1)
		_, body = _call(admin, "commissions", {"month": "2026-09", "partner": self.other})
		self.assertEqual(body["total"], 0)
		self.assertEqual(body["totals"]["approved"], {"amount": 0, "currency": "INR"})
		self.assertEqual(_call(admin, "commissions", {"month": "2026-9"})[0], 422)
		self.assertEqual(_call(admin, "commissions", {"status": "nope"})[0], 422)
		self.assertEqual(_call(admin, "payouts", {"period": "x"})[0], 422)

	def test_admin_sees_void_while_a_partner_sees_reversed(self):
		name = self._commission(100, "void")
		self.assertEqual(
			_call(admin, "commissions", {"partner": self.partner})[1]["items"][0]["status"], "void"
		)
		self._as_partner(self.partner)
		item = _call(partner, "commissions", as_admin=False)[1]["items"][0]
		self.assertEqual((item["id"], item["status"]), (name, "reversed"))

	def test_admin_payout_list(self):
		payout = self._draft(1200)
		_, body = _call(admin, "payouts", {"status": "draft"})
		self.assertEqual([i["id"] for i in body["items"]], [payout])
		self.assertEqual(body["items"][0]["partnerName"], "Partner a")
		self.assertEqual(_call(admin, "payouts", {"status": "paid"})[1]["items"], [])

	# ------------------------------------------------------------ who may call

	def test_only_admins_reach_the_admin_endpoints(self):
		name = self._commission(1200, "pending")
		self._as_partner(self.partner)
		for fn, data in (
			("commissions", {}),
			("payouts", {}),
			("approve_commissions", {"ids": [name]}),
			("void_commissions", {"ids": [name], "reason": "x"}),
			("create_payouts", {"period": THIS_MONTH}),
			("mark_payout_paid", {"id": "x", "reference": "x", "paidOn": nowdate(), "method": "upi"}),
			("cancel_payout", {"id": "x"}),
		):
			status, body = _call(admin, fn, data, as_admin=False)
			self.assertEqual((status, body["error"]["code"]), (403, "forbidden"), fn)
		self.assertEqual(self._row(name).status, "pending")

	def test_admins_cannot_use_the_partner_endpoints(self):
		frappe.set_user("Administrator")
		frappe.local.gx_token = "test-token"
		for fn in ("payouts", "commissions", "earnings"):
			self.assertEqual(_call(partner, fn, as_admin=False)[0], 403, fn)

	# ------------------------------------------------------------ the partner's numbers

	def test_partner_earnings_come_from_the_ledger(self):
		today = nowdate()
		self._commission(100, "pending", paid_on=today)
		self._commission(200, "approved", paid_on=today)
		self._commission(400, "paid", paid_on=add_days(today, -90))
		self._commission(800, "void", paid_on=today)
		self._commission(5000, "pending", partner_name=self.other, paid_on=today)  # not mine
		self._as_partner(self.partner)
		status, e = _call(partner, "earnings", as_admin=False)
		self.assertEqual(status, 200, e)
		amounts = {k: v["amount"] for k, v in e.items() if isinstance(v, dict)}
		self.assertEqual(
			amounts,
			{
				"thisMonth": 30000,
				"lastMonth": amounts["lastMonth"],
				"pending": 10000,
				"approved": 20000,
				"paidLifetime": 40000,
				"lifetimeTotal": 70000,
				"minimumPayout": 100000,
			},
		)
		self.assertEqual((e["tdsRate"], e["tier"], e["commissionRate"]), (0.02, "Seed", 0.2))
		self.assertRegex(e["nextPayoutDate"], r"^\d{4}-\d{2}-15$")
		summary = _call(partner, "summary", as_admin=False)[1]
		self.assertEqual(summary["pendingPayout"]["amount"], 30000)  # pending + approved

	def test_commission_history_and_the_partner_ledger(self):
		self._commission(100, "approved", paid_on=nowdate())
		self._commission(50, "void", paid_on=nowdate())
		self._commission(70, "paid", paid_on=add_days(nowdate(), -400))  # outside the 12 months
		self._as_partner(self.partner)
		items = _call(partner, "commission_history", as_admin=False)[1]["items"]
		self.assertEqual(len(items), 12)
		self.assertEqual(items[-1], {"month": THIS_MONTH, "amount": {"amount": 10000, "currency": "INR"}})
		self.assertEqual(sum(i["amount"]["amount"] for i in items), 10000)
		ledger = _call(partner, "commissions", {"status": "approved"}, as_admin=False)[1]
		self.assertEqual((ledger["total"], ledger["items"][0]["amount"]["amount"]), (1, 10000))
		self.assertEqual(ledger["items"][0]["rate"], 0.2)
		self.assertEqual(_call(partner, "commissions", {"from": "nope"}, as_admin=False)[0], 422)

	def test_partner_payouts_show_only_their_own_and_hide_cancelled(self):
		mine = self._draft(1200)
		self._commission(1300, partner_name=self.other)
		_call(admin, "create_payouts", {"period": THIS_MONTH})
		self._as_partner(self.partner)
		items = _call(partner, "payouts", as_admin=False)[1]["items"]
		self.assertEqual(
			[(i["id"], i["status"], i["reference"]) for i in items], [(mine, "processing", None)]
		)
		frappe.set_user("Administrator")
		_call(
			admin, "mark_payout_paid", {"id": mine, "reference": "UTR5", "paidOn": nowdate(), "method": "upi"}
		)
		self._as_partner(self.partner)
		item = _call(partner, "payouts", as_admin=False)[1]["items"][0]
		self.assertEqual(
			(
				item["status"],
				item["reference"],
				item["gross"]["amount"],
				item["tds"]["amount"],
				item["net"]["amount"],
			),
			("paid", "UTR5", 120000, 2400, 117600),
		)
		self.assertEqual(_call(partner, "payouts", {"status": "paid"}, as_admin=False)[1]["total"], 1)
		self.assertEqual(_call(partner, "payouts", {"status": "processing"}, as_admin=False)[1]["total"], 0)
		self._as_partner(self.other)
		self.assertEqual(
			[i["status"] for i in _call(partner, "payouts", as_admin=False)[1]["items"]], ["processing"]
		)
