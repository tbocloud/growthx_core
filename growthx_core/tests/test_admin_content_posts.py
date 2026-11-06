"""admin.content_posts / content_post / save_content_post / delete_content_post (the /content portal)."""

import json
import uuid
from datetime import timedelta
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import now_datetime

from growthx_core import accounts
from growthx_core.api.v1 import _core, admin

EDITOR_ROLE = "GrowthX Content Editor"


def _call(fn, **data):
	frappe.local.form_dict = frappe._dict(data)
	frappe.local.request = None
	# An error response rolls the request back, which would also drop this test's setup.
	with patch.object(frappe.db, "rollback"):
		response = fn()
	return response.status_code, json.loads(response.get_data())


def _post(**over):
	data = {
		"site": "printagx",
		"slug": "gst-invoicing",
		"title": "GST invoicing for print shops",
		"body": "> A quote.\n\nRead [the guide](/guides/gst).\n",
		"status": "published",
		"tags": ["gst", "invoicing"],
		"authorName": "Asha Rao",
		"seo": {"title": "GST invoicing", "description": "How to do it", "ogImage": None, "noindex": False},
		"canonicalUrl": None,
		"coverImage": None,
		"coverAlt": None,
	}
	data.update(over)
	return _call(admin.save_content_post, **data)


class TestAdminContentPosts(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		frappe.db.delete("GX Website Post")  # rolled back in tearDown
		self.editor = f"gx-editor-{uuid.uuid4().hex[:8]}@example.com"
		frappe.get_doc(
			{"doctype": "User", "email": self.editor, "first_name": "Sree", "send_welcome_email": 0}
		).insert(ignore_permissions=True).add_roles(EDITOR_ROLE)
		self._as(self.editor)

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.local.gx_token = None
		frappe.db.rollback()

	def _as(self, user):
		frappe.set_user(user)
		frappe.local.gx_token = "test-token"

	def test_create_returns_the_full_post_and_is_audited(self):
		status, body = _post()
		self.assertEqual(status, 200, body)
		self.assertEqual(
			{k: body[k] for k in ("site", "slug", "status", "isLive", "tags", "authorName", "updatedBy")},
			{
				"site": "printagx",
				"slug": "gst-invoicing",
				"status": "published",
				"isLive": True,
				"tags": ["gst", "invoicing"],
				"authorName": "Asha Rao",
				"updatedBy": self.editor,
			},
		)
		self.assertEqual(body["body"], "> A quote.\n\nRead [the guide](/guides/gst).\n")  # as typed
		self.assertEqual(body["url"], "https://printagx.in/blog/gst-invoicing")
		self.assertEqual(
			body["seo"],
			{"title": "GST invoicing", "description": "How to do it", "ogImage": None, "noindex": False},
		)
		self.assertEqual((body["excerpt"], body["coverImage"], body["coverAlt"]), ("", None, None))
		self.assertTrue(body["publishedAt"].endswith("Z") and body["createdAt"].endswith("Z"))
		log = frappe.get_all(
			"GX Audit Log",
			{"reference_doctype": "GX Website Post", "reference_name": body["id"]},
			["actor", "action"],
		)
		self.assertEqual([(r.actor, r.action) for r in log], [(self.editor, "content.post_created")])

	def test_update_replaces_the_post_and_checks_for_conflicts(self):
		created = _post()[1]
		status, body = _post(
			id=created["id"],
			title="New title",
			tags=[],
			authorName=None,
			expectedUpdatedAt=created["updatedAt"],
		)
		self.assertEqual(status, 200, body)
		self.assertEqual((body["title"], body["tags"], body["authorName"]), ("New title", [], None))
		frappe.db.set_value(
			"GX Website Post",
			created["id"],
			"modified",
			now_datetime() + timedelta(minutes=1),
			update_modified=False,
		)
		status, body = _post(id=created["id"], expectedUpdatedAt=created["updatedAt"])
		self.assertEqual((status, body["error"]["code"]), (409, "edit_conflict"))

	def test_slug_taken_is_409_but_only_on_the_same_site(self):
		_post()
		status, body = _post(title="Another")
		self.assertEqual((status, body["error"]["code"]), (409, "slug_taken"))
		self.assertEqual(_post(site="willpex")[0], 200)

	def test_scheduling_with_a_utc_time(self):
		when = (now_datetime() + timedelta(days=3)).replace(microsecond=0)
		utc = _core.iso(when)
		status, body = _post(status="scheduled", publishedAt=utc)
		self.assertEqual(status, 200, body)
		self.assertEqual((body["publishedAt"], body["isLive"]), (utc, False))

	def test_field_errors(self):
		past = _core.iso(now_datetime() - timedelta(days=1))
		cases = [
			({"body": ""}, "body"),
			({"title": ""}, "title"),
			({"slug": "Bad Slug"}, "slug"),
			({"site": "nope"}, "site"),
			({"status": "live"}, "status"),
			({"body": "[x](javascript:alert(1))"}, "body"),
			({"body": "![logo](https://printagx.in/a.png)"}, "body"),
			({"coverImage": "https://media.tbogrowthx.com/a.webp"}, "coverImage"),
			({"seo": {"ogImage": "https://media.tbogrowthx.com/a.webp"}}, "seo.ogImage"),
			({"seo": {"title": "x" * 71}}, "seo.title"),
			({"seo": {"description": "x" * 201}}, "seo.description"),
			({"tags": ["Bad Tag!"]}, "tags"),
			({"tags": "gst"}, "tags"),
			({"title": "A > B"}, "title"),
			({"authorName": "<b>me</b>"}, "authorName"),
			({"status": "scheduled", "publishedAt": past}, "publishedAt"),
			({"status": "scheduled"}, "publishedAt"),
			({"publishedAt": "next tuesday"}, "publishedAt"),
			({"canonicalUrl": "https://evil.com/x"}, "canonicalUrl"),
		]
		for over, field in cases:
			status, body = _post(**over)
			self.assertEqual(status, 422, (over, body))
			self.assertIn(field, body["error"].get("fields", {}), (over, body))
		self.assertEqual(frappe.db.count("GX Website Post"), 0)

	def test_list_filters_and_shape(self):
		a = _post(slug="a", title="Alpha GST", tags=["gst"])[1]
		b = _post(slug="b", title="Beta", tags=["gst-filing"], status="draft")[1]
		_post(site="willpex", slug="c", title="Other site")
		frappe.db.set_value(
			"GX Website Post", a["id"], "modified", now_datetime() - timedelta(days=1), update_modified=False
		)

		status, body = _call(admin.content_posts, site="printagx")
		self.assertEqual(status, 200, body)
		self.assertEqual(([i["id"] for i in body["items"]], body["total"]), ([b["id"], a["id"]], 2))
		self.assertNotIn("body", body["items"][0])
		ids = lambda **f: [i["id"] for i in _call(admin.content_posts, site="printagx", **f)[1]["items"]]  # noqa: E731
		self.assertEqual(ids(status="draft"), [b["id"]])
		self.assertEqual(ids(tag="gst"), [a["id"]])  # exact tag, not gst-filing
		self.assertEqual(ids(q="alpha"), [a["id"]])
		self.assertEqual(ids(q="b"), [b["id"]])  # slug
		self.assertEqual(ids(page=2, pageSize=1), [a["id"]])
		self.assertEqual(_call(admin.content_posts)[0], 422)  # site is required

	def test_get_and_delete(self):
		post = _post()[1]
		status, body = _call(admin.content_post, id=post["id"])
		self.assertEqual((status, body["slug"]), (200, "gst-invoicing"))
		res = _call(admin.delete_content_post, id=post["id"])
		self.assertEqual(res, (200, {"ok": True}), json.dumps(res))
		self.assertFalse(frappe.db.exists("GX Website Post", post["id"]))
		self.assertTrue(
			frappe.db.exists("GX Audit Log", {"action": "content.post_deleted", "reference_name": post["id"]})
		)
		self.assertEqual(_call(admin.content_post, id=post["id"])[0], 404)
		self.assertEqual(_call(admin.delete_content_post, id=post["id"])[0], 404)

	def test_admins_too_but_not_customers(self):
		frappe.set_user("Administrator")
		admin_user = f"gx-admin-{uuid.uuid4().hex[:8]}@example.com"
		frappe.get_doc(
			{"doctype": "User", "email": admin_user, "first_name": "Ad", "send_welcome_email": 0}
		).insert(ignore_permissions=True).add_roles("GrowthX Admin")
		customer, _ = accounts.create_customer_account(
			email=f"gx-cust-{uuid.uuid4().hex[:8]}@example.com",
			full_name="Cust",
			company="Cust Co",
			password="Long-Enough-Pass-91",
		)
		self._as(admin_user)
		self.assertEqual(_post()[0], 200)
		self._as(customer)
		for fn in (
			admin.content_posts,
			admin.content_post,
			admin.save_content_post,
			admin.delete_content_post,
		):
			self.assertEqual(_call(fn, site="printagx", id="x")[0], 403, fn.__name__)
		self._as(self.editor)
		with patch.dict(frappe.conf, {"growthx_content_portal_login": 0}):
			self.assertEqual(_call(admin.content_posts, site="printagx")[0], 401)

	def test_desk_can_delete_a_post_saved_through_the_portal(self):
		post = _post()[1]  # has an audit row pointing at it
		frappe.set_user(self.editor)
		frappe.delete_doc("GX Website Post", post["id"])
		self.assertFalse(frappe.db.exists("GX Website Post", post["id"]))
		self.assertTrue(frappe.db.exists("GX Audit Log", {"reference_name": post["id"]}))  # history kept
