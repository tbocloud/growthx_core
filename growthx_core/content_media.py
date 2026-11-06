"""Images for website content: check, re-encode, upload to S3 behind media.tbogrowthx.com.

Nothing is stored on the Core host. The file is decoded (not trusted by name or Content-Type), must be a
JPEG, PNG or WebP of at most 5 MB and 6000 x 6000 px, and is re-encoded, which drops EXIF, GPS and any other
metadata. Each upload gets a new random name, so it can be cached for a year (immutable).

site_config (DevOps):
	growthx_media_access_key_id      IAM user growthx-core-media-upload (s3:PutObject on content/* only)
	growthx_media_secret_access_key
	growthx_media_bucket             default growthx-media-prod-442426868050
	growthx_media_region             default ap-south-1
	growthx_media_host               default media.tbogrowthx.com (serves the bucket's content/ prefix)
"""

import io
import secrets

import frappe
from frappe.utils import now_datetime

from growthx_core import website_content as wc

MAX_BYTES = 5 * 1024 * 1024
MAX_SIDE = 6000
CACHE_CONTROL = "public, max-age=31536000, immutable"
DEFAULT_BUCKET = "growthx-media-prod-442426868050"
DEFAULT_REGION = "ap-south-1"
# Pillow format -> (Content-Type, extension)
FORMATS = {"JPEG": ("image/jpeg", "jpg"), "PNG": ("image/png", "png"), "WEBP": ("image/webp", "webp")}


class StorageUnavailable(Exception):
	"""S3 isn't configured or didn't accept the file. Nothing was stored."""


def process(raw: bytes) -> dict:
	"""Check and re-encode an uploaded image. Raises ValueError with a message for the editor."""
	from PIL import Image, ImageOps, UnidentifiedImageError

	if not raw:
		raise ValueError("Choose an image to upload.")
	if len(raw) > MAX_BYTES:
		raise ValueError("The image must be 5 MB or smaller.")
	not_an_image = "Upload a JPEG, PNG or WebP image."
	try:
		with Image.open(io.BytesIO(raw)) as probe:
			fmt = probe.format
			width, height = probe.size
			if fmt in FORMATS and width <= MAX_SIDE and height <= MAX_SIDE:
				probe.verify()  # structure check, before decoding the pixels
	except (UnidentifiedImageError, Image.DecompressionBombError, OSError, SyntaxError, ValueError):
		raise ValueError(not_an_image)
	if fmt not in FORMATS:
		raise ValueError(not_an_image)  # SVG, GIF, BMP, TIFF, …
	if width > MAX_SIDE or height > MAX_SIDE:
		raise ValueError(f"The image must be at most {MAX_SIDE} x {MAX_SIDE} pixels.")

	try:
		with Image.open(io.BytesIO(raw)) as img:
			img.seek(0)  # first frame of an animated PNG/WebP
			img = ImageOps.exif_transpose(img)  # keep the photo upright once EXIF is gone
			has_alpha = img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info)
			out = io.BytesIO()
			if fmt == "JPEG":
				img.convert("RGB").save(out, "JPEG", quality=85, optimize=True, progressive=True)
			elif fmt == "PNG":
				img.convert("RGBA" if has_alpha else "RGB").save(out, "PNG", optimize=True)
			else:
				img.convert("RGBA" if has_alpha else "RGB").save(out, "WEBP", quality=85, method=4)
			width, height = img.size
	except (OSError, SyntaxError, ValueError):
		raise ValueError(not_an_image)

	content_type, ext = FORMATS[fmt]
	data = out.getvalue()
	return {"data": data, "contentType": content_type, "ext": ext, "width": width, "height": height}


def upload(site: str, image: dict) -> str:
	"""Store a processed image in S3; return its public URL on the media host."""
	conf = frappe.conf
	key_id = conf.get("growthx_media_access_key_id")
	secret = conf.get("growthx_media_secret_access_key")
	if not (key_id and secret):
		raise StorageUnavailable("growthx_media_access_key_id / growthx_media_secret_access_key not set")
	try:
		import boto3
		from botocore.config import Config
		from botocore.exceptions import BotoCoreError, ClientError
	except ImportError as e:
		raise StorageUnavailable("boto3 is not installed") from e

	key = f"content/{site}/{now_datetime():%Y}/{secrets.token_hex(16)}.{image['ext']}"
	client = boto3.client(
		"s3",
		region_name=conf.get("growthx_media_region") or DEFAULT_REGION,
		aws_access_key_id=key_id,
		aws_secret_access_key=secret,
		config=Config(connect_timeout=5, read_timeout=20, retries={"max_attempts": 2}),
	)
	try:
		client.put_object(
			Bucket=conf.get("growthx_media_bucket") or DEFAULT_BUCKET,
			Key=key,
			Body=image["data"],
			ContentType=image["contentType"],
			CacheControl=CACHE_CONTROL,
		)
	except (BotoCoreError, ClientError) as e:
		raise StorageUnavailable(str(e)) from e
	return f"https://{wc.media_host()}/{key}"
