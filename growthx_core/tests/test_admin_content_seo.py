"""Site SEO settings and page SEO: admin.content_sites / update_content_site / content_pages /
save_content_page / delete_content_page, and the public content.site."""

import json
import uuid
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from growthx_core import website_content as wc
from growthx_core.api.v1 import admin, content

EDITOR_ROLE = "GrowthX Content Editor"


def _call(fn, **data):
	frappe.local.form_dict = frappe._dict(data)
	frappe.local.request = None
	with patch.object(frappe.db, "rollback"):  # an error response would drop the test's setup
		response = fn()
	return response.status_code, json.loads(response.get_data())


class TestContentSeo(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		frappe.db.delete("GX Content Page")  # all rolled back in tearDown
		frappe.db.delete("GX Website Post")
		frappe.db.delete("GX Content Site")
		wc.ensure_sites()
		self.editor = f"gx-editor-{uuid.uuid4().hex[:8]}@example.com"
		frappe.get_doc(
			{"doctype": "User", "email": self.editor, "first_name": "Sree", "send_welcome_email": 0}
		).insert(ignore_permissions=True).add_roles(EDITOR_ROLE)
		frappe.set_user(self.editor)
		frappe.local.gx_token = "test-token"

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.local.gx_token = None
		frappe.db.rollback()

	# ------------------------------------------------------------ sites

	def test_sites_list_all_six_in_order_with_counts(self):
		frappe.set_user("Administrator")
		frappe.get_doc(
			{"doctype": "GX Website Post", "site": "printagx", "title": "A", "body": "x", "status": "draft"}
		).insert(ignore_permissions=True)
		frappe.set_user(self.editor)
		status, body = _call(admin.content_sites)
		self.assertEqual(status, 200, body)
		self.assertEqual([i["site"] for i in body["items"]], list(wc.SITES))  # growthx first
		printagx = body["items"][1]
		self.assertEqual(printagx["postCounts"], {"draft": 1, "scheduled": 0, "published": 0})
		self.assertEqual(
			(
				printagx["siteName"],
				printagx["baseUrl"],
				printagx["titleTemplate"],
				printagx["analyticsActive"],
			),
			("PrintagX", "https://printagx.in", "%s · PrintagX", False),
		)

	def test_update_site_partial_and_audited(self):
		status, body = _call(
			admin.update_content_site,
			site="printagx",
			defaultTitle="Print shop software",
			googleVerification="abcDEF123_-xyz",
			bingVerification="0123456789ABCDEF0123456789abcdef",
			analyticsId="G-AB12CD34EF",
			blogEnabled=False,
		)
		self.assertEqual(status, 200, body)
		self.assertEqual(
			(body["defaultTitle"], body["siteName"], body["analyticsId"], body["blogEnabled"]),
			("Print shop software", "PrintagX", "G-AB12CD34EF", False),
		)
		body = _call(admin.update_content_site, site="printagx", defaultTitle=None)[1]
		self.assertEqual((body["defaultTitle"], body["googleVerification"]), (None, "abcDEF123_-xyz"))
		self.assertTrue(
			frappe.db.exists("GX Audit Log", {"action": "content.site_updated", "reference_name": "printagx"})
		)

	def test_site_validation(self):
		cases = [
			({"titleTemplate": "No placeholder"}, "titleTemplate"),
			({"titleTemplate": "%s and %s"}, "titleTemplate"),
			({"siteName": ""}, "siteName"),
			({"siteName": "A > B"}, "siteName"),
			({"defaultTitle": "x" * 71}, "defaultTitle"),
			({"defaultDescription": "x" * 201}, "defaultDescription"),
			({"googleVerification": "short"}, "googleVerification"),
			(
				{"googleVerification": '<meta name="google-site-verification" content="abc">'},
				"googleVerification",
			),
			({"bingVerification": "nothex"}, "bingVerification"),
			({"analyticsId": "UA-12345"}, "analyticsId"),
			({"defaultOgImage": "https://media.tbogrowthx.com/a.webp"}, "defaultOgImage"),
			({"blogEnabled": "yes"}, "blogEnabled"),
		]
		for over, field in cases:
			status, body = _call(admin.update_content_site, site="printagx", **over)
			self.assertEqual(status, 422, (over, body))
			self.assertIn(field, body["error"].get("fields", {}), (over, body))
		self.assertEqual(_call(admin.update_content_site, site="nope")[0], 422)

	# ------------------------------------------------------------ pages

	def test_pages_upsert_by_path_and_validate(self):
		status, body = _call(
			admin.save_content_page, site="printagx", path="/pricing/", title="Pricing", noindex=False
		)
		self.assertEqual(status, 200, body)
		self.assertEqual(
			(body["path"], body["title"], body["ogImage"], body["updatedBy"]),
			("/pricing", "Pricing", None, self.editor),
		)
		again = _call(admin.save_content_page, site="printagx", path="/pricing", title="Plans", noindex=True)[
			1
		]
		self.assertEqual((again["id"], again["title"], again["noindex"]), (body["id"], "Plans", True))
		_call(admin.save_content_page, site="printagx", path="/about", title="About")
		_call(admin.save_content_page, site="willpex", path="/about", title="Other site")
		items = _call(admin.content_pages, site="printagx")[1]["items"]
		self.assertEqual([i["path"] for i in items], ["/about", "/pricing"])
		for over, field in [
			({"path": "pricing"}, "path"),
			({"path": "/blog/some-post"}, "path"),
			({"path": "/pricing?x=1"}, "path"),
			({"path": "/a#b"}, "path"),
			({"path": "//evil.com"}, "path"),
			({"path": "/x", "title": "x" * 71}, "title"),
			({"path": "/x", "description": "a < b"}, "description"),
			({"path": "/x", "ogImage": "https://media.tbogrowthx.com/a.webp"}, "ogImage"),
		]:
			status, body = _call(admin.save_content_page, site="printagx", **over)
			self.assertEqual(status, 422, (over, body))
			self.assertIn(field, body["error"].get("fields", {}), (over, body))
		self.assertEqual(
			_call(admin.save_content_page, site="printagx", path="/blog")[0], 200
		)  # the list page is fine

	def test_delete_page_from_portal_and_desk(self):
		page = _call(admin.save_content_page, site="printagx", path="/pricing", title="Pricing")[1]
		self.assertEqual(_call(admin.delete_content_page, id=page["id"]), (200, {"ok": True}))
		self.assertTrue(
			frappe.db.exists("GX Audit Log", {"action": "content.page_deleted", "reference_name": page["id"]})
		)
		self.assertEqual(_call(admin.delete_content_page, id=page["id"])[0], 404)
		desk = _call(admin.save_content_page, site="printagx", path="/about", title="About")[1]
		frappe.delete_doc("GX Content Page", desk["id"])  # desk: audit rows don't block it
		self.assertFalse(frappe.db.exists("GX Content Page", desk["id"]))

	# ------------------------------------------------------------ public content.site

	def test_public_site_shape_analytics_and_version(self):
		_call(admin.update_content_site, site="printagx", defaultTitle="Print", analyticsId="G-AB12CD34EF")
		_call(admin.save_content_page, site="printagx", path="/pricing", title="Pricing")
		frappe.set_user("Guest")
		frappe.local.gx_token = None
		status, body = _call(content.site, site="printagx")
		self.assertEqual(status, 200, body)
		self.assertEqual(
			{
				k: body[k]
				for k in (
					"site",
					"siteName",
					"baseUrl",
					"titleTemplate",
					"analyticsId",
					"blogEnabled",
					"redirects",
				)
			},
			{
				"site": "printagx",
				"siteName": "PrintagX",
				"baseUrl": "https://printagx.in",
				"titleTemplate": "%s · PrintagX",
				"analyticsId": None,  # saved, not active
				"blogEnabled": True,
				"redirects": [],
			},
		)
		self.assertEqual(body["defaults"], {"title": "Print", "description": None, "ogImage": None})
		self.assertEqual(body["verification"], {"google": None, "bing": None})
		self.assertEqual(
			body["pages"],
			[
				{
					"path": "/pricing",
					"title": "Pricing",
					"description": None,
					"ogImage": None,
					"noindex": False,
				}
			],
		)
		with patch.dict(frappe.conf, {"growthx_content_analytics": 1}):
			self.assertEqual(_call(content.site, site="printagx")[1]["analyticsId"], "G-AB12CD34EF")
		version = body["version"]
		self.assertEqual(_call(content.site, site="printagx")[1]["version"], version)  # stable
		frappe.set_user("Administrator")
		frappe.get_doc({"doctype": "GX Content Page", "site": "printagx", "path": "/about"}).insert(
			ignore_permissions=True
		)
		frappe.set_user("Guest")
		self.assertNotEqual(_call(content.site, site="printagx")[1]["version"], version)
		status, body = _call(content.site, site="nope")
		self.assertEqual((status, body["error"]["code"]), (404, "site_not_found"))

	def test_blog_switch_hides_posts(self):
		frappe.set_user("Administrator")
		frappe.get_doc(
			{
				"doctype": "GX Website Post",
				"site": "printagx",
				"title": "Live",
				"body": "x",
				"status": "published",
			}
		).insert(ignore_permissions=True)
		frappe.set_user(self.editor)
		_call(admin.update_content_site, site="printagx", blogEnabled=False)
		frappe.set_user("Guest")
		frappe.local.gx_token = None
		self.assertEqual(_call(content.posts, site="printagx")[1]["total"], 0)
		self.assertEqual(_call(content.post, site="printagx", slug="live")[0], 404)
		self.assertEqual(_call(content.sitemap, site="printagx")[1]["items"], [])
		self.assertFalse(_call(content.site, site="printagx")[1]["blogEnabled"])

	# ------------------------------------------------------------ roles and audit

	def test_customers_get_403_and_audit_lists_content(self):
		_call(admin.save_content_page, site="printagx", path="/pricing", title="Pricing")
		frappe.set_user("Administrator")
		admin_user = f"gx-admin-{uuid.uuid4().hex[:8]}@example.com"
		frappe.get_doc(
			{"doctype": "User", "email": admin_user, "first_name": "Ad", "send_welcome_email": 0}
		).insert(ignore_permissions=True).add_roles("GrowthX Admin")
		frappe.set_user(admin_user)
		rows = _call(admin.audit_log, entityType="content")[1]["items"]
		self.assertEqual({(r["action"], r["entityType"]) for r in rows}, {("page_saved", "content")})
		self.assertEqual(rows[0]["entityLabel"], "/pricing")
		customer = f"gx-cust-{uuid.uuid4().hex[:8]}@example.com"
		frappe.set_user("Administrator")
		frappe.get_doc(
			{"doctype": "User", "email": customer, "first_name": "Cu", "send_welcome_email": 0}
		).insert(ignore_permissions=True).add_roles("GrowthX Customer")
		frappe.set_user(customer)
		for fn in (
			admin.content_sites,
			admin.update_content_site,
			admin.content_pages,
			admin.save_content_page,
			admin.delete_content_page,
		):
			self.assertEqual(_call(fn, site="printagx", id="x")[0], 403, fn.__name__)


class TestPhoebixNamePatch(IntegrationTestCase):
	def tearDown(self):
		frappe.db.rollback()

	def test_renames_the_seeded_name_only(self):
		from growthx_core.patches.v1 import phoebix_site_name

		wc.ensure_sites()
		frappe.db.set_value(
			"GX Content Site", "phoebix", {"site_name": "Phoebix", "title_template": "%s · Phoebix"}
		)
		phoebix_site_name.execute()
		self.assertEqual(
			frappe.db.get_value("GX Content Site", "phoebix", ["site_name", "title_template"]),
			("PhoebiX", "%s · PhoebiX"),
		)
		frappe.db.set_value(
			"GX Content Site", "phoebix", {"site_name": "Phoebix Solar", "title_template": "%s | Solar"}
		)
		phoebix_site_name.execute()  # an editor's own choice stays
		self.assertEqual(
			frappe.db.get_value("GX Content Site", "phoebix", ["site_name", "title_template"]),
			("Phoebix Solar", "%s | Solar"),
		)
		self.assertEqual(wc.SITE_NAMES["phoebix"], "PhoebiX")
