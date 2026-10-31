"""Shared plumbing for every `growthx_core.api.v1` endpoint.

Every endpoint is declared with `@endpoint(...)`, which:
- whitelists it with Frappe (always `allow_guest`; authentication is checked here, not by Frappe),
- requires a GrowthX bearer token unless `auth=False` (Frappe cookie sessions are rejected),
- checks the caller's role,
- applies rate limits and `Idempotency-Key` handling,
- returns the resource as plain JSON (no `{"message": ...}` wrapper),
- turns every exception into `{"error": {code, message, fields?, reference?}}` and rolls back.

See openapi.yaml for the contract and docs/api-decisions.md for why it looks like this.
"""

import functools
import hashlib
import hmac
import ipaddress
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

import frappe
from frappe.utils import cint, flt, get_datetime, get_system_timezone, validate_email_address
from frappe.utils.response import json_handler
from werkzeug.wrappers import Response

API_PREFIX = "/api/method/growthx_core.api.v1."
CURRENCY = "INR"
MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 20
IDEMPOTENCY_TTL = 24 * 60 * 60

ROLE_MAP = {
	"admin": "GrowthX Admin",
	"partner": "GrowthX Partner",
	"customer": "GrowthX Customer",
}

DEFAULT_MESSAGES = {
	400: ("bad_request", "The request could not be processed."),
	401: ("unauthenticated", "Please log in again."),
	403: ("forbidden", "You don't have access to this."),
	404: ("not_found", "Not found."),
	405: ("method_not_allowed", "This method is not allowed for this endpoint."),
	409: ("conflict", "The request conflicts with the current state."),
	417: ("validation_failed", "The request is invalid."),
	422: ("validation_failed", "The request is invalid."),
	429: ("rate_limited", "Too many requests. Please try again later."),
}


class ApiError(Exception):
	"""Raise from an endpoint to return a contract error response."""

	def __init__(
		self, status: int, code: str, message: str, fields: dict | None = None, headers=None, extra=None
	):
		super().__init__(message)
		self.status = status
		self.code = code
		self.message = message
		self.fields = fields
		self.headers = headers or {}
		self.extra = extra or {}  # more keys for the error object, e.g. {"loginUrl": ...}


def validation_error(fields: dict, message: str = "Please correct the highlighted fields.") -> ApiError:
	return ApiError(422, "validation_failed", message, fields=fields)


def not_found() -> ApiError:
	return ApiError(404, "not_found", "Not found.")


@dataclass(frozen=True)
class RateLimit:
	limit: int
	seconds: int
	# Optional request field to key on instead of the IP address, e.g. "email"
	by_field: str | None = None
	# Optional request header to key on instead of the IP address, e.g. a tenant key
	by_header: str | None = None
	# Key on the logged-in user instead of the IP address
	by_user: bool = False


# ---------------------------------------------------------------- responses


def json_response(data, status: int = 200, headers: dict | None = None) -> Response:
	response = Response(
		json.dumps(data, default=json_handler, separators=(",", ":")),
		status=status,
		mimetype="application/json",
	)
	response.headers["Cache-Control"] = "no-store"
	for key, value in (headers or {}).items():
		response.headers[key] = str(value)
	return response


def error_response(status, code, message, fields=None, reference=None, headers=None, extra=None) -> Response:
	error = {**(extra or {}), "code": code, "message": message}
	if fields:
		error["fields"] = fields
	if reference:
		error["reference"] = reference
	return json_response({"error": error}, status, headers)


def ok(status: int = 200):
	return status, {"ok": True}


# ---------------------------------------------------------------- the decorator


def endpoint(
	methods=("GET",),
	auth: bool = True,
	roles: tuple[str, ...] | None = None,
	rate_limit: RateLimit | tuple[RateLimit, ...] | None = None,
	idempotent: bool = False,
):
	"""Declare a v1 endpoint. The wrapped function takes no arguments; it reads input with
	`body()` / `query()` and returns either data (200) or a `(status, data)` tuple."""

	limits = rate_limit if isinstance(rate_limit, tuple) else ((rate_limit,) if rate_limit else ())

	def decorator(fn):
		@functools.wraps(fn)
		def wrapper(*args, **kwargs):
			try:
				for limit in limits:
					_check_rate_limit(fn.__name__, limit)
				if auth:
					_require_auth(roles)

				replay_key = _idempotency_key(fn) if idempotent else None
				if replay_key:
					cached = _idempotency_lookup(replay_key)
					if cached:
						return json_response(cached["body"], cached["status"])

				result = fn()
				status, data = result if isinstance(result, tuple) else (200, result)

				if replay_key and status < 400:
					_idempotency_store(replay_key, status, data)
				return json_response(data, status)

			except ApiError as e:
				frappe.db.rollback()
				return error_response(e.status, e.code, e.message, e.fields, headers=e.headers, extra=e.extra)
			except frappe.DoesNotExistError:
				frappe.db.rollback()
				return error_response(404, "not_found", "Not found.")
			except frappe.PermissionError:
				frappe.db.rollback()
				return error_response(403, "forbidden", "You don't have access to this.")
			except frappe.ValidationError as e:
				frappe.db.rollback()
				return error_response(
					422, "validation_failed", _clean_message(e) or "The request is invalid."
				)
			except Exception:
				frappe.db.rollback()
				reference = _log_exception(fn)
				return error_response(
					500, "internal_error", "Something went wrong. Please try again.", reference=reference
				)
			finally:
				# Frappe's msgprint queue would otherwise leak into nothing; keep it clean.
				frappe.local.message_log = []

		return frappe.whitelist(allow_guest=True, methods=list(methods))(wrapper)

	return decorator


def after_request(response, request):
	"""Rewrite Frappe's own error bodies on v1 paths into the contract's error shape.

	Frappe validates bearer tokens and HTTP methods before our endpoint runs, so an expired
	token or a wrong method would otherwise return Frappe's `exc_type`/`_server_messages` body."""

	if response is None or not request.path.startswith(API_PREFIX):
		return

	response.headers["Cache-Control"] = "no-store"

	if response.status_code < 400:
		return
	try:
		body = json.loads(response.get_data() or b"{}")
	except ValueError:
		body = {}
	if isinstance(body, dict) and isinstance(body.get("error"), dict):
		return  # already ours

	status = response.status_code
	if status >= 500:
		code, message = "internal_error", "Something went wrong. Please try again."
	else:
		code, message = DEFAULT_MESSAGES.get(status, ("bad_request", "The request could not be processed."))
	if status == 401 and frappe.get_request_header("Authorization"):
		code, message = "unauthenticated", "Your session has expired. Please log in again."

	error = {"code": code, "message": message}
	response.set_data(json.dumps({"error": error}))
	response.mimetype = "application/json"


def _clean_message(e: Exception) -> str:
	return re.sub(r"<[^>]+>", "", str(e)).strip()


def _log_exception(fn) -> str | None:
	try:
		log = frappe.log_error(title=f"GrowthX API error in {fn.__module__}.{fn.__name__}")
		# GET requests are rolled back at the end of the request; keep the error log.
		frappe.local.flags.commit = True
		return log.name if log else None
	except Exception:
		return None


# ---------------------------------------------------------------- auth


def _require_auth(roles):
	if not getattr(frappe.local, "gx_token", None):
		# No GrowthX bearer token. Frappe cookie sessions are deliberately not accepted.
		raise ApiError(401, "unauthenticated", "Please log in.")
	if roles and get_role(frappe.session.user) not in roles:
		raise ApiError(403, "forbidden", "You don't have access to this.")


def get_role(user: str) -> str | None:
	"""The caller's GrowthX role: admin, partner or customer (highest wins)."""
	user_roles = set(frappe.get_roles(user))
	for role, role_name in ROLE_MAP.items():
		if role_name in user_roles:
			return role
	return None


def current_user() -> str:
	return frappe.session.user


# The website calls Core from its own server, so Core would see one IP for every visitor. It
# forwards the visitor's IP; Core trusts it only with the shared key (`growthx_web_key` in
# site_config, GROWTHX_WEB_KEY on the website).
WEB_KEY_HEADER = "X-GrowthX-Web-Key"
CLIENT_IP_HEADER = "X-GrowthX-Client-IP"


def has_web_key() -> bool:
	"""The request carries the website's shared key (server-to-server calls from the website)."""
	if getattr(frappe.local, "request", None) is None:
		return False
	key = frappe.conf.get("growthx_web_key")
	sent = frappe.get_request_header(WEB_KEY_HEADER)
	return bool(key and sent and hmac.compare_digest(sent.encode(), str(key).encode()))


def client_ip() -> str:
	"""The caller's IP address, for rate limits and records: the visitor's, for website calls."""
	return _forwarded_client_ip() or getattr(frappe.local, "request_ip", None) or "unknown"


def _forwarded_client_ip() -> str | None:
	if getattr(frappe.local, "request", None) is None:
		return None
	if not has_web_key():
		return None
	ip = (frappe.get_request_header(CLIENT_IP_HEADER) or "").strip()
	try:
		return str(ipaddress.ip_address(ip))
	except ValueError:
		return None  # "unknown" (the website had no X-Real-IP) or junk: use the connection's IP


# ---------------------------------------------------------------- input


def body() -> dict:
	"""The JSON request body (POST), or the query parameters (GET)."""
	request = getattr(frappe.local, "request", None)
	if request is not None and request.method in ("POST", "PUT", "PATCH"):
		data = request.get_json(silent=True)
		if data is None:
			data = {k: v for k, v in frappe.local.form_dict.items() if k != "cmd"}
		if not isinstance(data, dict):
			raise ApiError(400, "bad_request", "The request body must be a JSON object.")
		return data
	return query()


def query() -> dict:
	"""Query-string parameters (also when a client sends a JSON content type on a GET)."""
	params = {k: v for k, v in frappe.local.form_dict.items() if k != "cmd"}
	request = getattr(frappe.local, "request", None)
	if request is not None:
		params.update(request.args.to_dict())
	return params


def get_str(data: dict, key: str, required=False, max_length=140, label=None) -> str | None:
	value = data.get(key)
	if value is None or (isinstance(value, str) and not value.strip()):
		if required:
			raise validation_error({key: f"{label or key} is required."})
		return None
	if not isinstance(value, str):
		raise validation_error({key: f"{label or key} must be a string."})
	value = value.strip()
	if max_length and len(value) > max_length:
		raise validation_error({key: f"{label or key} must be at most {max_length} characters."})
	return value


def get_email(data: dict, key="email", required=True) -> str | None:
	value = get_str(data, key, required=required, max_length=140, label="Email")
	if value and not validate_email_address(value):
		raise validation_error({key: "Enter a valid email address."})
	return value.lower() if value else None


def get_choice(data: dict, key: str, choices, required=False, default=None) -> str | None:
	value = get_str(data, key, required=required)
	if value is None:
		return default
	if value not in choices:
		raise validation_error({key: f"Must be one of: {', '.join(choices)}."})
	return value


def page_params(data: dict | None = None) -> tuple[int, int]:
	data = data if data is not None else query()
	try:
		page = int(data.get("page") or 1)
		page_size = int(data.get("pageSize") or DEFAULT_PAGE_SIZE)
	except (TypeError, ValueError):
		raise validation_error({"page": "page and pageSize must be integers."})
	if page < 1:
		raise validation_error({"page": "page must be 1 or more."})
	if not 1 <= page_size <= MAX_PAGE_SIZE:
		raise validation_error({"pageSize": f"pageSize must be between 1 and {MAX_PAGE_SIZE}."})
	return page, page_size


def get_sort(data: dict, allowed: dict[str, str], default: str) -> str:
	"""Map `sort=-createdAt` to an SQL order-by using the `allowed` field map."""
	value = data.get("sort") or default
	field = value.lstrip("-")
	if field not in allowed:
		raise validation_error({"sort": f"Must be one of: {', '.join(allowed)} (prefix - for descending)."})
	return f"{allowed[field]} {'desc' if value.startswith('-') else 'asc'}"


def page_meta(page: int, page_size: int, total: int, items: list) -> dict:
	return {"items": items, "page": page, "pageSize": page_size, "total": total}


# ---------------------------------------------------------------- output formats


def money(amount) -> dict:
	"""Rupees (ERPNext decimal) -> integer paise."""
	paise = (Decimal(str(flt(amount))) * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP)
	return {"amount": int(paise), "currency": CURRENCY}


def price(amount, gst_included) -> dict:
	return {**money(amount), "gstIncluded": bool(cint(gst_included))}


def iso(value) -> str | None:
	"""Site-timezone datetime/date -> ISO 8601 UTC with Z."""
	if not value:
		return None
	if isinstance(value, date) and not isinstance(value, datetime):
		value = datetime(value.year, value.month, value.day)
	value = get_datetime(value)
	if value.tzinfo is None:
		value = value.replace(tzinfo=ZoneInfo(get_system_timezone()))
	return value.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")


def net_of_gst(amount, gst_included, gst_rate) -> float:
	amount = flt(amount)
	if cint(gst_included) and flt(gst_rate):
		return amount / (1 + flt(gst_rate) / 100)
	return amount


def monthly(amount, interval) -> float:
	return flt(amount) / 12 if interval == "year" else flt(amount)


# ---------------------------------------------------------------- rate limits


def _check_rate_limit(name: str, limit: RateLimit):
	if frappe.flags.in_test and not frappe.flags.gx_enforce_rate_limits:
		return
	parts = [name, client_ip()]
	if limit.by_field:
		value = body().get(limit.by_field)
		if isinstance(value, str) and value.strip():
			parts = [name, limit.by_field, value.strip().lower()]
	elif limit.by_header:
		value = frappe.get_request_header(limit.by_header)
		if value and value.strip():
			parts = [name, limit.by_header, value.strip()]
	elif limit.by_user and frappe.session.user != "Guest":
		parts = [name, "user", frappe.session.user]
	key = frappe.cache.make_key("gx:rl:" + hashlib.sha256("|".join(parts).encode()).hexdigest())
	count = frappe.cache.incrby(key, 1)
	if count == 1:
		frappe.cache.expire(key, limit.seconds)
	if count > limit.limit:
		retry_after = frappe.cache.ttl(key)
		raise ApiError(
			429,
			"rate_limited",
			"Too many requests. Please try again later.",
			headers={"Retry-After": max(int(retry_after or limit.seconds), 1)},
		)


# ---------------------------------------------------------------- idempotency


def _idempotency_key(fn) -> tuple[str, str] | None:
	key = frappe.get_request_header("Idempotency-Key")
	if not key:
		return None
	key = key.strip()
	if not re.fullmatch(r"[A-Za-z0-9\-]{8,64}", key):
		raise validation_error({"Idempotency-Key": "Must be a UUID."})
	scope = frappe.session.user if getattr(frappe.local, "gx_token", None) else "guest"
	payload_hash = hashlib.sha256(json.dumps(body(), sort_keys=True, default=str).encode()).hexdigest()
	return f"gx:idem:{fn.__module__}.{fn.__name__}:{scope}:{key}", payload_hash


def _idempotency_lookup(replay_key):
	key, payload_hash = replay_key
	cached = frappe.cache.get_value(key)
	if not cached:
		return None
	if cached["hash"] != payload_hash:
		raise ApiError(
			409, "idempotency_conflict", "This Idempotency-Key was already used with a different request."
		)
	return cached


def _idempotency_store(replay_key, status, data):
	key, payload_hash = replay_key
	frappe.cache.set_value(
		key,
		{"hash": payload_hash, "status": status, "body": json.loads(json.dumps(data, default=json_handler))},
		expires_in_sec=IDEMPOTENCY_TTL,
	)


# ---------------------------------------------------------------- misc


def audit(action: str, reference_doctype=None, reference_name=None, details=None, actor=None):
	frappe.get_doc(
		{
			"doctype": "GX Audit Log",
			"actor": actor or frappe.session.user,
			"action": action,
			"reference_doctype": reference_doctype,
			"reference_name": reference_name,
			"ip_address": client_ip(),
			"details": details,
		}
	).insert(ignore_permissions=True)


def web_url(path: str = "") -> str:
	"""URL on the GrowthX website (Next.js). Set `growthx_web_url` in site_config."""
	base = (frappe.conf.get("growthx_web_url") or frappe.utils.get_url()).rstrip("/")
	return base + path


def now_plus(**kwargs) -> datetime:
	return frappe.utils.now_datetime() + timedelta(**kwargs)
