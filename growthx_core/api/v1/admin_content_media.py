"""admin.upload_content_image for the /content portal (re-exported from admin.py).

Multipart: `site` and `file`. The website server forwards the editor's file; Core checks it, re-encodes it
(dropping EXIF and location data) and stores it in S3 behind the media host. Nothing stays on Core.
"""

import frappe

from growthx_core import content_media
from growthx_core import website_content as wc
from growthx_core.api.v1._core import ApiError, RateLimit, audit, endpoint, get_choice, validation_error


@endpoint(
	methods=("POST",),
	roles=("admin", "content_editor"),
	rate_limit=RateLimit(60, 60 * 60, by_user=True),
)
def upload_content_image():
	form = {k: v for k, v in (frappe.local.form_dict or {}).items() if k != "cmd"}
	site = get_choice(form, "site", wc.SITES, required=True)
	request = getattr(frappe.local, "request", None)
	upload = request.files.get("file") if request is not None else None
	if upload is None:
		raise validation_error({"file": "Choose an image to upload."})

	raw = upload.stream.read(content_media.MAX_BYTES + 1)
	try:
		image = content_media.process(raw)
	except ValueError as e:
		raise validation_error({"file": str(e)})
	try:
		url = content_media.upload(site, image)
	except content_media.StorageUnavailable:
		frappe.log_error(title="GrowthX: content image upload failed")
		raise ApiError(
			503, "storage_unavailable", "Images can't be uploaded right now. Please try again later."
		)

	audit("content.image_uploaded", details=f"{site}: {url}")
	return 201, {
		"url": url,
		"contentType": image["contentType"],
		"width": image["width"],
		"height": image["height"],
		"size": len(image["data"]),
	}
