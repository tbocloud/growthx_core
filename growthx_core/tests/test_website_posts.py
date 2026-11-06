"""GX Website Post (edited in desk) and the public content.posts / content.post / content.sitemap."""

import json
import uuid
from datetime import timedelta
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import now_datetime

from growthx_core import website_content as wc
from growthx_core.api.v1 import _core, content

EDITOR_ROLE = "GrowthX Content Editor"


def _post(**fields):
	doc = {
		"doctype": "GX Website Post",
		"site": "printagx",
		"title": "GST invoicing for print shops",
		"body": "Hello **world**. Read [the guide](/guides/gst).",
		"status": "published",
		**fields,
	}
	return frappe.get_doc(doc).insert(ignore_permissions=True)


def _call(fn, **params):
	frappe.local.form_dict = frappe._dict(params)
	frappe.local.request = None
	response = fn()
	return response.status_code, json.loads(response.get_data())


class TestWebsitePostDoctype(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		frappe.db.delete("GX Website Post")  # rolled back in tearDown

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def test_slug_from_title_and_unique_per_site(self):
		doc = _post(title="GST Invoicing: for Print-Shops!")
		self.assertEqual(doc.slug, "gst-invoicing-for-print-shops")
		with self.assertRaises(frappe.DuplicateEntryError):
			_post(title="Another", slug="gst-invoicing-for-print-shops")
		self.assertTrue(_post(site="willpex", slug="gst-invoicing-for-print-shops").name)  # other site
		with self.assertRaises(frappe.ValidationError):
			_post(slug="Bad Slug")

	def test_tags_are_normalised(self):
		self.assertEqual(_post(tags="GST, Invoicing ,gst\nprint shops").tags, "gst, invoicing, print-shops")
		with self.assertRaises(frappe.ValidationError):
			_post(slug="many", tags=", ".join(f"t{i}" for i in range(11)))
		with self.assertRaises(frappe.ValidationError):
			_post(slug="odd", tags="gst%")

	def test_status_and_dates(self):
		self.assertTrue(_post(slug="now").published_at)  # published defaults to now
		past = now_datetime() - timedelta(days=30)
		self.assertEqual(str(_post(slug="imported", published_at=past).published_at), str(past))
		with self.assertRaises(frappe.ValidationError):
			_post(slug="future-published", published_at=now_datetime() + timedelta(days=1))
		with self.assertRaises(frappe.ValidationError):
			_post(slug="sched-no-date", status="scheduled")
		with self.assertRaises(frappe.ValidationError):
			_post(slug="sched-past", status="scheduled", published_at=past)
		doc = _post(slug="sched", status="scheduled", published_at=now_datetime() + timedelta(hours=1))
		frappe.db.set_value("GX Website Post", doc.name, "published_at", past)  # it went live
		doc.reload()
		doc.title = "Edited after going live"
		doc.save()  # re-saving a live scheduled post is fine

	def test_body_link_rules(self):
		good = (
			"[a](https://printagx.in/x) [b](http://example.com) [c](mailto:hi@example.com) "
			"[d](/pricing) [e](#faq) <https://example.com>\n\n[ref]: https://example.com\n"
		)
		self.assertTrue(_post(slug="good", body=good).name)
		for i, bad in enumerate(
			(
				"[x](javascript:alert(1))",
				"[x](JavaScript:alert(1))",
				"[x]( <javascript:alert(1)> )",
				"[x](data:text/html;base64,AAAA)",
				"[x](&#106;avascript:alert(1))",
				"[x](//evil.example.com)",
				"[x][r]\n\n[r]: javascript:alert(1)",
				"<javascript:alert(1)>",
				"[x](vbscript:msgbox)",
			)
		):
			with self.assertRaises(frappe.ValidationError, msg=bad):
				_post(slug=f"bad-{i}", body=bad)

	def test_images_are_refused_in_phase_1(self):
		for i, body in enumerate(("![logo](https://printagx.in/a.png)", "![logo][img]\n\n[img]: /a.png")):
			with self.assertRaises(frappe.ValidationError):
				_post(slug=f"img-{i}", body=body)

	def test_lengths_and_canonical(self):
		with self.assertRaises(frappe.ValidationError):
			_post(slug="long-seo", seo_title="x" * 71)
		with self.assertRaises(frappe.ValidationError):
			_post(slug="long-body", body="x" * (wc.BODY_MAX + 1))
		self.assertTrue(_post(slug="canon", canonical_url="https://www.tbogrowthx.com/blog/x").name)
		for i, url in enumerate(
			("http://printagx.in/x", "https://evil.com/x", "https://printagx.in.evil.com/x", "javascript:x")
		):
			with self.assertRaises(frappe.ValidationError, msg=url):
				_post(slug=f"canon-{i}", canonical_url=url)

	def test_content_editor_role_sees_only_posts(self):
		self.assertEqual(frappe.db.get_value("Role", EDITOR_ROLE, "desk_access"), 1)
		perms = frappe.get_all("DocPerm", {"role": EDITOR_ROLE}, pluck="parent")
		perms += frappe.get_all("Custom DocPerm", {"role": EDITOR_ROLE}, pluck="parent")
		self.assertEqual(sorted(set(perms)), ["GX Website Post"])
		email = f"gx-editor-{uuid.uuid4().hex[:8]}@example.com"
		user = frappe.get_doc(
			{"doctype": "User", "email": email, "first_name": "Editor", "send_welcome_email": 0}
		).insert(ignore_permissions=True)
		user.add_roles(EDITOR_ROLE)
		frappe.set_user(email)
		self.assertTrue(frappe.has_permission("GX Website Post", "create"))
		for doctype in ("GX Account", "GX Contact Request", "GX Plan", "GX Audit Log"):
			self.assertFalse(frappe.has_permission(doctype, "read"), doctype)


class TestPublicContent(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		frappe.db.delete("GX Website Post")  # rolled back in tearDown
		now = now_datetime()
		self.old = _post(slug="old", title="Old", tags="gst", published_at=now - timedelta(days=10))
		self.new = _post(
			slug="new",
			title="New",
			tags="gst-filing, invoicing",
			author_name="Asha Rao",
			excerpt="Short summary.",
			published_at=now - timedelta(days=1),
		)
		self.draft = _post(slug="draft", status="draft")
		self.future = _post(slug="future", status="scheduled", published_at=now + timedelta(days=1))
		self.went_live = _post(slug="went-live", status="scheduled", published_at=now + timedelta(hours=1))
		frappe.db.set_value("GX Website Post", self.went_live.name, "published_at", now - timedelta(days=5))
		self.other_site = _post(site="willpex", slug="other")
		frappe.set_user("Guest")

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def test_posts_are_live_only_newest_first(self):
		status, body = _call(content.posts, site="printagx")
		self.assertEqual(status, 200, body)
		self.assertEqual([p["slug"] for p in body["items"]], ["new", "went-live", "old"])
		self.assertEqual((body["page"], body["pageSize"], body["total"]), (1, 12, 3))
		item = body["items"][0]
		self.assertEqual(
			set(item),
			{
				"slug",
				"title",
				"excerpt",
				"coverImage",
				"coverAlt",
				"authorName",
				"tags",
				"publishedAt",
				"updatedAt",
			},
		)
		self.assertEqual(
			(item["excerpt"], item["coverImage"], item["coverAlt"], item["authorName"], item["tags"]),
			("Short summary.", None, None, "Asha Rao", ["gst-filing", "invoicing"]),
		)
		self.assertTrue(item["publishedAt"].endswith("Z"))
		self.assertEqual(body["items"][2]["excerpt"], "Hello world. Read the guide.")  # from the body

	def test_tag_filter_is_exact(self):
		self.assertEqual(
			[p["slug"] for p in _call(content.posts, site="printagx", tag="gst")[1]["items"]], ["old"]
		)
		self.assertEqual(
			[p["slug"] for p in _call(content.posts, site="printagx", tag="invoicing")[1]["items"]], ["new"]
		)
		self.assertEqual(_call(content.posts, site="printagx", tag="g%")[0], 422)

	def test_pagination(self):
		frappe.set_user("Administrator")
		for i in range(12):
			_post(slug=f"bulk-{i}", published_at=now_datetime() - timedelta(days=20 + i))
		frappe.set_user("Guest")
		body = _call(content.posts, site="printagx", page=2)[1]
		self.assertEqual((len(body["items"]), body["total"]), (3, 15))

	def test_site_errors(self):
		status, body = _call(content.posts, site="nope")
		self.assertEqual((status, body["error"]["code"]), (404, "site_not_found"))
		status, body = _call(content.posts)
		self.assertEqual((status, list(body["error"]["fields"])), (422, ["site"]))

	def test_one_post(self):
		status, body = _call(content.post, site="printagx", slug="new")
		self.assertEqual(status, 200, body)
		self.assertEqual(body["body"], self.new.body)
		self.assertEqual(body["seo"], {"title": None, "description": None, "ogImage": None, "noindex": False})
		self.assertIsNone(body["canonicalUrl"])
		for slug in ("draft", "future", "other", "missing", "Not A Slug"):
			self.assertEqual(_call(content.post, site="printagx", slug=slug)[0], 404, slug)

	def test_sitemap_leaves_out_noindex_and_canonical(self):
		frappe.set_user("Administrator")
		frappe.db.set_value("GX Website Post", self.old.name, "noindex", 1)
		frappe.db.set_value(
			"GX Website Post", self.went_live.name, "canonical_url", "https://tbogrowthx.com/blog/x"
		)
		frappe.set_user("Guest")
		status, body = _call(content.sitemap, site="printagx")
		self.assertEqual(status, 200, body)
		self.assertEqual([i["slug"] for i in body["items"]], ["new"])
		self.assertTrue(body["items"][0]["updatedAt"].endswith("Z"))


class TestWebKeySkipsTheLimit(IntegrationTestCase):
	def test_skip_with_web_key(self):
		limit = _core.RateLimit(1, 60, skip_with_web_key=True)
		name = f"gx-test-{uuid.uuid4().hex}"
		frappe.flags.gx_enforce_rate_limits = True
		try:
			with patch.object(_core, "has_web_key", return_value=True):
				for _ in range(3):
					_core._check_rate_limit(name, limit)  # never counted
			with patch.object(_core, "has_web_key", return_value=False):
				_core._check_rate_limit(name, limit)
				with self.assertRaises(_core.ApiError) as e:
					_core._check_rate_limit(name, limit)
			self.assertEqual(e.exception.status, 429)
		finally:
			frappe.flags.gx_enforce_rate_limits = False


class TestContentEditorTwoFactor(IntegrationTestCase):
	"""Two-factor login (authenticator app) for GrowthX Content Editor only."""

	def setUp(self):
		frappe.set_user("Administrator")

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def _user(self, role):
		email = f"gx-2fa-{uuid.uuid4().hex[:8]}@example.com"
		user = frappe.get_doc(
			{"doctype": "User", "email": email, "first_name": "Two", "send_welcome_email": 0}
		).insert(ignore_permissions=True)
		user.add_roles(role)
		return email

	def test_migrate_turns_on_otp_app_and_only_the_editor_role_needs_it(self):
		from frappe.twofactor import two_factor_is_enabled

		from growthx_core.setup import install

		frappe.db.set_single_value("System Settings", "enable_two_factor_auth", 0)
		install.apply_settings()
		self.assertEqual(frappe.db.get_single_value("System Settings", "enable_two_factor_auth"), 1)
		self.assertEqual(frappe.db.get_single_value("System Settings", "two_factor_method"), "OTP App")
		self.assertTrue(frappe.db.get_single_value("System Settings", "otp_issuer_name"))

		self.assertEqual(frappe.db.get_value("Role", EDITOR_ROLE, "two_factor_auth"), 1)
		for role in ("GrowthX Admin", "GrowthX Customer", "GrowthX Partner"):
			self.assertEqual(frappe.db.get_value("Role", role, "two_factor_auth"), 0, role)
		self.assertTrue(two_factor_is_enabled(self._user(EDITOR_ROLE)))
		self.assertFalse(two_factor_is_enabled(self._user("GrowthX Admin")))

	def test_api_login_only_while_portal_sign_in_is_on(self):
		# Portal sign-in is password-only by decision (DevOps, 6 Nov 2026); turning it off is the way
		# back to "desk with two-factor only". Details in test_content_editor_login.
		from frappe.utils.password import update_password

		from growthx_core.api.v1 import auth

		email = self._user(EDITOR_ROLE)
		update_password(email, "Long-Enough-Pass-91")
		frappe.set_user("Guest")
		frappe.local.request = None
		frappe.local.form_dict = frappe._dict({"email": email, "password": "Long-Enough-Pass-91"})
		self.assertEqual(auth.login().status_code, 200)
		with patch.dict(frappe.conf, {"growthx_content_portal_login": 0}):
			self.assertEqual(auth.login().status_code, 401)  # last: a refusal rolls back the test user


class TestStoredAsTyped(IntegrationTestCase):
	"""Posts are never stored HTML-escaped (Frappe's XSS filter turned "> quote" into "&gt; quote")."""

	def setUp(self):
		frappe.set_user("Administrator")
		frappe.db.delete("GX Website Post")  # rolled back in tearDown

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def test_body_is_stored_exactly_as_typed(self):
		body = (
			"> A quote.\n\n"
			"Prices: 5 < 10 and 10 > 5, Tom & Jerry.\n\n"
			"<https://tbogrowthx.com> and <b>bold</b> and <script>alert(1)</script>\n\n"
			"```\nif a < b && b > c: pass\n```\n"
		)
		doc = _post(body=body)
		self.assertEqual(frappe.db.get_value("GX Website Post", doc.name, "body"), body)
		doc.reload()
		doc.title = "Edited"
		doc.save()  # a desk re-save keeps it too
		self.assertEqual(frappe.db.get_value("GX Website Post", doc.name, "body"), body)
		frappe.set_user("Guest")
		self.assertEqual(_call(content.post, site="printagx", slug=doc.slug)[1]["body"], body)

	def test_plain_fields_refuse_angle_brackets_and_keep_ampersands(self):
		doc = _post(
			title="Tips & tricks: GST",
			excerpt="Fast & simple",
			author_name="Asha & Ravi",
			seo_title="GST & you",
			seo_description="All about GST & invoices",
		)
		row = frappe.db.get_value(
			"GX Website Post",
			doc.name,
			["title", "excerpt", "author_name", "seo_title", "seo_description"],
			as_dict=True,
		)
		self.assertEqual(
			list(row.values()),
			["Tips & tricks: GST", "Fast & simple", "Asha & Ravi", "GST & you", "All about GST & invoices"],
		)
		for i, field in enumerate(("title", "excerpt", "author_name", "seo_title", "seo_description")):
			with self.assertRaises(frappe.ValidationError, msg=field):
				_post(slug=f"angle-{i}", **{field: "A > B"})
			with self.assertRaises(frappe.ValidationError, msg=field):
				_post(slug=f"tag-{i}", **{field: "<img src=x onerror=alert(1)>"})

	def test_canonical_url_refuses_markup(self):
		with self.assertRaises(frappe.ValidationError):
			_post(canonical_url='https://printagx.in/x"><script>')
