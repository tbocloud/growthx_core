"""Content images: checks and re-encoding, the S3 upload, admin.upload_content_image, and images in
posts, site and page SEO."""

import io
import json
import re
import uuid
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase
from PIL import Image
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request

from growthx_core import content_media
from growthx_core import website_content as wc
from growthx_core.api.v1 import admin, content
from growthx_core.provisioning import runner

EDITOR_ROLE = "GrowthX Content Editor"
KEYS = {"growthx_media_access_key_id": "AKIATEST", "growthx_media_secret_access_key": "secret"}


def _image(fmt="JPEG", size=(40, 20), mode="RGB", exif=None):
	buf = io.BytesIO()
	color = {"RGBA": (200, 30, 30, 128), "RGB": (200, 30, 30)}.get(mode, 120)
	img = Image.new(mode, size, color)
	kwargs = {"exif": exif} if exif is not None else {}
	img.save(buf, fmt, **kwargs)
	return buf.getvalue()


def _gps_exif(orientation=None):
	exif = Image.Exif()
	exif[0x010F] = "PhoneMaker"  # Make
	exif[0x8825] = {1: "N", 2: (12.0, 58.0, 0.0)}  # GPS
	if orientation:
		exif[0x0112] = orientation
	return exif


def _media_url(site="printagx", ext="webp"):
	return f"https://media.tbogrowthx.com/content/{site}/2026/{uuid.uuid4().hex}.{ext}"


class TestProcess(IntegrationTestCase):
	def test_reencodes_and_drops_exif_and_gps(self):
		out = content_media.process(_image(exif=_gps_exif()))
		self.assertEqual(
			(out["contentType"], out["ext"], out["width"], out["height"]), ("image/jpeg", "jpg", 40, 20)
		)
		with Image.open(io.BytesIO(out["data"])) as img:
			self.assertEqual(img.format, "JPEG")
			self.assertEqual(dict(img.getexif()), {})

	def test_orientation_is_applied_before_exif_is_dropped(self):
		out = content_media.process(_image(exif=_gps_exif(orientation=6)))  # rotated 90°
		self.assertEqual((out["width"], out["height"]), (20, 40))

	def test_png_keeps_transparency_and_webp_works(self):
		out = content_media.process(_image("PNG", mode="RGBA"))
		with Image.open(io.BytesIO(out["data"])) as img:
			self.assertEqual((img.format, img.mode), ("PNG", "RGBA"))
		self.assertEqual(content_media.process(_image("WEBP"))["contentType"], "image/webp")

	def test_refuses_other_files(self):
		for raw in (
			_image("GIF", mode="P"),
			_image("BMP"),
			b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
			b"not an image at all",
			_image()[:200],  # truncated JPEG
			b"",
		):
			with self.assertRaises(ValueError):
				content_media.process(raw)

	def test_size_limits(self):
		with self.assertRaises(ValueError) as e:
			content_media.process(_image("PNG", size=(6001, 2), mode="L"))
		self.assertIn("6000", str(e.exception))
		with self.assertRaises(ValueError) as e:
			content_media.process(b"\xff" * (content_media.MAX_BYTES + 1))
		self.assertIn("5 MB", str(e.exception))


class TestUpload(IntegrationTestCase):
	def test_needs_keys(self):
		with patch.dict(frappe.conf, {"growthx_media_access_key_id": None}):
			with self.assertRaises(content_media.StorageUnavailable):
				content_media.upload("printagx", content_media.process(_image()))

	def test_puts_with_content_type_and_long_cache(self):
		client = MagicMock()
		with patch.dict(frappe.conf, KEYS), patch("boto3.client", return_value=client) as make:
			url = content_media.upload("printagx", content_media.process(_image("WEBP")))
		self.assertEqual(make.call_args.kwargs["region_name"], "ap-south-1")
		put = client.put_object.call_args.kwargs
		self.assertEqual(put["Bucket"], "growthx-media-prod-442426868050")
		self.assertRegex(put["Key"], r"^content/printagx/\d{4}/[a-f0-9]{32}\.webp$")
		self.assertEqual(
			(put["ContentType"], put["CacheControl"]), ("image/webp", "public, max-age=31536000, immutable")
		)
		self.assertEqual(url, f"https://media.tbogrowthx.com/{put['Key']}")
		self.assertTrue(wc.media_url_ok(url))

	def test_s3_errors_are_storage_unavailable(self):
		from botocore.exceptions import ClientError

		client = MagicMock()
		client.put_object.side_effect = ClientError({"Error": {"Code": "AccessDenied"}}, "PutObject")
		with patch.dict(frappe.conf, KEYS), patch("boto3.client", return_value=client):
			with self.assertRaises(content_media.StorageUnavailable):
				content_media.upload("printagx", content_media.process(_image()))

	def test_media_is_a_reserved_subdomain(self):
		self.assertIn("media", runner.RESERVED_SUBDOMAINS)


class TestUploadEndpoint(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.editor = f"gx-editor-{uuid.uuid4().hex[:8]}@example.com"
		frappe.get_doc(
			{"doctype": "User", "email": self.editor, "first_name": "Sree", "send_welcome_email": 0}
		).insert(ignore_permissions=True).add_roles(EDITOR_ROLE)
		frappe.set_user(self.editor)
		frappe.local.gx_token = "test-token"

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.local.gx_token = None
		frappe.local.request = None
		frappe.db.rollback()

	def _upload(self, raw=None, site="printagx", client=None):
		data = {"site": site}
		if raw is not None:
			data["file"] = (io.BytesIO(raw), "photo.jpg", "image/jpeg")
		frappe.local.request = Request(EnvironBuilder(method="POST", data=data).get_environ())
		frappe.local.form_dict = frappe._dict({"site": site})
		with patch.dict(frappe.conf, KEYS), patch("boto3.client", return_value=client or MagicMock()):
			with patch.object(frappe.db, "rollback"):
				response = admin.upload_content_image()
		return response.status_code, json.loads(response.get_data())

	def test_upload(self):
		status, body = self._upload(_image())
		self.assertEqual(status, 201, body)
		self.assertTrue(wc.media_url_ok(body["url"]))
		self.assertEqual((body["contentType"], body["width"], body["height"]), ("image/jpeg", 40, 20))
		self.assertGreater(body["size"], 0)
		self.assertTrue(
			frappe.db.exists("GX Audit Log", {"action": "content.image_uploaded", "actor": self.editor})
		)

	def test_errors(self):
		self.assertEqual(self._upload(None)[1]["error"]["fields"], {"file": "Choose an image to upload."})
		self.assertIn("file", self._upload(b"<svg/>")[1]["error"]["fields"])
		self.assertEqual(self._upload(_image(), site="nope")[0], 422)
		failing = MagicMock()
		from botocore.exceptions import EndpointConnectionError

		failing.put_object.side_effect = EndpointConnectionError(endpoint_url="https://s3")
		status, body = self._upload(_image(), client=failing)
		self.assertEqual((status, body["error"]["code"]), (503, "storage_unavailable"))

	def test_customers_get_403(self):
		frappe.set_user("Administrator")
		customer = f"gx-cust-{uuid.uuid4().hex[:8]}@example.com"
		frappe.get_doc(
			{"doctype": "User", "email": customer, "first_name": "Cu", "send_welcome_email": 0}
		).insert(ignore_permissions=True).add_roles("GrowthX Customer")
		frappe.set_user(customer)
		self.assertEqual(self._upload(_image())[0], 403)


class TestImagesInContent(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		frappe.db.delete("GX Website Post")  # rolled back in tearDown

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def _post(self, **fields):
		return frappe.get_doc(
			{
				"doctype": "GX Website Post",
				"site": "printagx",
				"title": "With images",
				"body": "Hello",
				"status": "published",
				**fields,
			}
		).insert(ignore_permissions=True)

	def test_post_images_from_the_media_host_only(self):
		cover, share, inline = _media_url(), _media_url(ext="jpg"), _media_url(ext="png")
		self._post(
			cover_image=cover,
			cover_alt="A printer",
			og_image=share,
			body=f"Look:\n\n![a printer]({inline})\n",
		)
		frappe.set_user("Guest")
		frappe.local.form_dict = frappe._dict({"site": "printagx"})
		frappe.local.request = None
		item = json.loads(content.posts().get_data())["items"][0]
		self.assertEqual((item["coverImage"], item["coverAlt"]), (cover, "A printer"))
		frappe.local.form_dict = frappe._dict({"site": "printagx", "slug": "with-images"})
		self.assertEqual(json.loads(content.post().get_data())["seo"]["ogImage"], share)
		frappe.set_user("Administrator")
		for i, fields in enumerate(
			(
				{"cover_image": cover},  # no description
				{"cover_image": "https://example.com/a.jpg", "cover_alt": "x"},
				{"cover_image": "https://media.tbogrowthx.com/other/a.jpg", "cover_alt": "x"},
				{"og_image": "http://media.tbogrowthx.com/content/printagx/2026/" + "a" * 32 + ".jpg"},
				{"body": "![x](https://printagx.in/a.png)"},
				{"body": f"![x][r]\n\n[r]: {inline}"},
				{"cover_alt": "<b>x</b>", "cover_image": cover},
			)
		):
			with self.assertRaises(frappe.ValidationError, msg=fields):
				self._post(slug=f"bad-{i}", **fields)

	def test_site_and_page_share_images(self):
		wc.ensure_sites()
		url = _media_url()
		site = frappe.get_doc("GX Content Site", "printagx")
		site.default_og_image = url
		site.save()
		frappe.get_doc(
			{"doctype": "GX Content Page", "site": "printagx", "path": "/pricing", "og_image": url}
		).insert(ignore_permissions=True)
		frappe.set_user("Guest")
		frappe.local.form_dict = frappe._dict({"site": "printagx"})
		frappe.local.request = None
		body = json.loads(content.site().get_data())
		self.assertEqual(body["defaults"]["ogImage"], url)
		self.assertEqual([p["ogImage"] for p in body["pages"] if p["path"] == "/pricing"], [url])
		frappe.set_user("Administrator")
		site.default_og_image = "https://example.com/a.jpg"
		with self.assertRaises(frappe.ValidationError):
			site.save()

	def test_media_url_shape(self):
		self.assertTrue(re.match(r"^https://media\.tbogrowthx\.com/", _media_url()))
		with patch.dict(frappe.conf, {"growthx_media_host": "media-test.example.com"}):
			self.assertFalse(wc.media_url_ok(_media_url()))
