# GrowthX API v1: decisions and open items

**Owner:** Niranjana C (Frappe) · **Reviewers:** Mubashir T (Web), Najath Thekkeyil (Flutter), Sammish (Lead), Arun P S (DevOps, for provisioning)
**Status:** Draft 2, 12 Oct 2026 (all v1 endpoints implemented; see §6). Responds to the web team's `02-api-contract.md` v0.5.
**Contract:** [`openapi.yaml`](../openapi.yaml). Once agreed, it replaces `02-api-contract.md`.

Each decision below was checked against the Frappe/ERPNext **v16** source (Frappe 16.35).

## 1. Decisions (please review)

| # | Topic (02 §2.0) | Decision | Why |
|---|---|---|---|
| 1 | **Auth** (OI-4) | **Option C: tokens issued by `growthx_core`.** `auth.login` / `auth.signup` return `accessToken` (1 h) and `refreshToken` (30 d, rotated on each use). Tokens are opaque random strings (`gxa_…`, `gxr_…`). Only their SHA-256 hashes are stored (`GX Auth Token`), and an `auth_hooks` entry checks them on every request, so expiry, revocation and disabled users are enforced. (Draft 1 said Frappe `OAuth Bearer Token`; that stores tokens in plain text, so it was dropped. Clients see no difference.) `auth.logout` revokes them; a password reset revokes all of them. | Frappe v16's OAuth provider **does not support the password grant** (`frappe/oauth.py`: only `authorization_code` and `refresh_token`), so option B can't back the existing login forms. Option A (`sid` cookie) means handling CSRF on the server and has no refresh token, which is poor for Flutter. Option D is long-lived, as Web already noted. |
| 2 | **Routing** (OI-5) | Frappe whitelisted methods. Base URL: `https://<core-host>/api/method/growthx_core.api.v1`, then `.<module>.<function>` (joined with a **dot**: it is one Frappe method name), e.g. `.catalogue.products`. GET methods take query parameters; POST methods take a JSON body. | `/api/v1/*` and `/api/v2/*` are **Frappe's own namespaces** (`frappe/api/__init__.py`), so REST paths there would collide. The core prompt also requires `growthx_core.api.v1.*`. The web app only needs base URL + path, as §2.0.2 says. |
| 3 | **Response envelope** (OI-5) | **No `{message}` wrapper.** The body is the resource. | Frappe passes a returned `werkzeug.Response` through unchanged (`frappe/api/__init__.py`), so `growthx_core` controls the body and status code. |
| 4 | Errors | `{"error": {"code", "message", "fields"?, "reference"?}}` with real HTTP status codes. The stable codes are listed in `openapi.yaml` → `Error`. Tracebacks are never returned. | Matches what Web assumes today and what the core prompt requires. |
| 5 | Money | Integer paise + `currency: "INR"`. Prices also carry `gstIncluded`. Invoices have `subtotal`, `tax` and `total`. | Matches the web app. The core prompt requires stating whether GST is included. `growthx_core` converts from ERPNext decimals. |
| 6 | Dates | ISO 8601 UTC with `Z`, shown in IST. Months are `YYYY-MM`. | Core prompt. `growthx_core` converts from the site timezone. |
| 7 | Pagination | `page`, `pageSize` (max 100) → `{items, page, pageSize, total}`. Filtering and sorting are server-side (`sort=-createdAt`). | Matches the web app. `growthx_core` returns `total`. |
| 8 | IDs | Opaque strings (Frappe document names). Products also have a `slug` and `availability: available | coming_soon`. | At launch only `solar` is `available`. |
| 9 | Payments (OI-3) | **Gateway-neutral shape adopted**: checkout returns `{provider, reference, publicKey, …}`; verify takes `{provider, reference, payload}`. Methods `billing.create_checkout` and `billing.verify_checkout`. The webhook is server-only and is the source of truth (Phase 3). | Same as Web's proposal. Web can now update `schemas.ts`, the adapter and the mock. |
| 10 | Idempotency | `Idempotency-Key` header (UUID, 24 h window) on `auth.signup`, `billing.create_checkout` and `admin.retry_provisioning`. A reused key with a different body returns `409 idempotency_conflict`. | |
| 11 | Versioning | Additive changes stay in v1. Breaking changes go into a new `growthx_core.api.v2` module, and both run in parallel during migration. | |
| 12 | CSRF, Next.js → core leg (OI-6, part) | Doesn't apply: requests carry a bearer token, not a cookie. **v1 endpoints reject Frappe cookie sessions** and send no CORS headers for browser origins. The browser → Next.js leg (token vs custom header) is still Web's and the Lead's decision. | Answers the questions in §2.0.4. |

### Changes to the web app's current shapes
- **Login and sign-up** also return `refreshToken` and `refreshExpiresAt`. The Next.js server refreshes before `expiresAt`. They take an optional `client: web | mobile`.
- **Paths are renamed**: `/products` → `/catalogue.products`, `/me` → `/auth.me`, `/customer/checkout` → `/billing.create_checkout`, and so on. The full list is in `openapi.yaml`.
- **Endpoints taking an id** use `?id=` for GET and `{"id"}` in the body for POST, since whitelisted methods have no path parameters.
- **`Subscription.status`** adds `suspended`. It also gains `trialEndsAt`, `cancelAtPeriodEnd` and `siteId`.
- **`Invoice.status`** is now `draft | open | overdue | paid | void`, mapped from ERPNext. `uncollectible` is gone and `overdue` is added. `total` is split into `subtotal`, `tax` and `total`.
- **`Plan`** gains `productSlug`, and `price` gains `gstIncluded`. `PartnerSummary` gains `referralUrl` (for the share link and QR code), and `openLeads` can be null.
- **New endpoints**: `auth.refresh`, `auth.forgot_password`, `auth.reset_password`.

## 2. Answers to 02 §2.5 (provisioning)

1. **Open-app sign-in:** a one-time link (`customer.open_site`): opaque token, valid 60 s, single use, `no-store`, audited. The tenant exchanges it in `growthx_theme.sso.login`, which calls Core's `tenant.redeem_login` server to server, authenticated by the tenant's own key (`X-GX-Tenant-Key`; Core stores only its hash), then logs the customer in as their own user and redirects to the desk. Customers have no tenant password; "Login with Email Link" is the fallback. A token issued for one tenant is refused (and spent) on any other. *On Press, DevOps must let provisioning write `growthx_core_url` and `growthx_tenant_key` into the tenant's site_config.*
2. **Timing:** unknown until Press runs on staging (Arun). The client polls `customer.site` every 5 s for up to 15 min. The server also emails the customer when the site is ready.
3. **Failures:** customers see `failed` and a safe `statusMessage`. Admins see the step, the error and the attempt count (`admin.tenant`). The customer never signs up again; an admin retries.
4. **Several sites:** the contract allows a list. *Recommendation: one site per product subscription at v1 (pending the Lead).*
5. **Sign-up response:** returns `201` immediately, with `site: {id, status: "requested"}`.

**Site statuses:** `requested → provisioning → ready`, or `→ failed` (retryable). Later states are `suspended` and `archived`.
**Provisioning steps:** `create_site → wait_for_site → install_apps → configure → health_check`.

## 3. Needs a decision from the Lead

| ID | Question | My recommendation |
|---|---|---|
| OI-2 | Does the trial start at sign-up or when the site is `ready`? Is the account kept if provisioning fails? | Start when the site is ready; keep the account. |
| — | One site per product subscription at v1? | Yes. |
| — | Partner sign-up: approved automatically, or by an admin? | Admin approval (`partnerStatus: pending_approval`). |
| — | Business rules in 02 §2.0.3 (prices, GST inclusion, 14-day trial, 90-day referral window, single-level commission) | The contract carries them as data only; these need owners' approval. |
| OI-6 | Browser → Next.js CSRF: token, or custom header | Web's call. |

## 4. 02 §2.4 gaps: target phase (not yet specified)

These are deliberately left out of `openapi.yaml` until their business rules are agreed. Each gets added through a `contract` card in its phase.

| Phase | Endpoints |
|---|---|
| 2 (Self sign-up) | `customer.products`; `admin.partners` (list, approve, suspend) |
| 3 (Money) | `customer.payments`, `customer.invoice_pdf`, `customer.change_plan`, `customer.cancel_subscription`, `customer.billing_profile` (GSTIN, state); `admin.subscriptions`, `admin.billing`, `admin.commissions`, `admin.run_payouts` |
| 4 (Launch) | `admin.products`, `admin.plans` (the ERPNext desk is used until then); `admin.analytics` |
| Needs a decision | `customer.access` (tenant users and seats), `customer.support_tickets`; `partner.team` (only if multi-level commission is approved) |

The **Partner app** endpoints are now in `openapi.yaml` as **proposed shapes** (`x-status: proposed`), from Flutter's
"GrowthX Partner App - Proposed API Contract" (14 Oct 2026). They are for review by Flutter and Web and are not built yet:

| Phase | Proposed endpoints | Blocked on |
|---|---|---|
| 2 | `partner.dashboard`, `partner.customers`, `partner.customer`, `partner.referral_stats`, `partner.notifications`, `partner.mark_notification_read`, `partner.mark_all_notifications_read`, `partner.dismiss_notification` (optional), `partner.register_device`, `partner.unregister_device`, `partner.profile`, `partner.update_profile`, `partner.notification_preferences`, `partner.update_notification_preferences` | — (click counts in `referral_stats` need the website to record visits) |
| 2 | `partner.leads`, `partner.create_lead`, `partner.update_lead` | Lead rules: duplicates across partners, who changes status (Sammish) |
| 2 | `partner.campaigns` | Do campaigns exist at launch? Global or per partner? (Sammish) |
| 3 | `partner.earnings`, `partner.commissions`, `partner.payouts`, `partner.payout` | Commission rules, review period, TDS, payout date (D8, finance) |
| 3 | `partner.request_payout_method_otp`, `partner.update_payout_method` | Verification method (SMS or email OTP, or password) and an SMS provider (Sammish) |

How Flutter's proposal was adapted to the agreed conventions: no `{data, meta, error}` envelope (the body is the
resource); camelCase fields; money as integer paise; `{items, page, pageSize, total}`; Frappe methods (POST with `id`
in the body instead of `PATCH /…/{id}`); lower-case error codes. Added: device registration for push, `reversed`
commission status (refunds), `lead` notification category, and an OTP step before changing payout details.

## 5. Known gaps in this draft
- Real staging and production hosts are pending DevOps.
- Rate limits are set per endpoint (e.g. login: 5 per email and 20 per IP per 15 min; sign-up: 10 per IP per hour) and return 429 `rate_limited` with `Retry-After`. They will be tuned on staging.

## 6. Implementation status (12 Oct 2026, local bench)

All 23 operations in `openapi.yaml` are implemented in `growthx_core/api/v1/` and pass an end-to-end HTTP smoke test (50 checks) plus 21 unit tests. They run on a **local bench only**; nothing is on staging yet, so nothing is "done" under the Definition of Done.

| Area | State | What's still missing |
|---|---|---|
| health, catalogue, contact | Working | — |
| auth (login, signup, refresh, logout, me, forgot/reset password) | Working | Email delivery needs an outgoing email account on the site. The reset link points to `growthx_web_url` + `/reset-password?key=…` (the web app needs that page). |
| customer.sites / site / subscriptions / invoices | Working | Invoices stay empty until Phase 3 creates Sales Invoices. |
| customer.open_site + tenant.redeem_login | Single sign-on works end to end locally (13 Oct 2026): the customer lands in their tenant's desk as their own user | On Press: writing the tenant's site_config (DevOps). Web server access logs contain the one-time token (it is in the URL); it is useless once used or after 60 s. |
| Provisioning (sign-up → ready) | Working with a **fake Press client** | The real Press client is waiting on DevOps (endpoints, token). The `configure` step is waiting on the Solar set-up method (Nahala). |
| billing.create_checkout / verify_checkout | Implemented against Razorpay | Returns 503 `payment_unavailable` until Razorpay keys (D5) and each plan's `gateway_plan_id` are set. The webhook (source of truth) is Phase 3. |
| partner.summary / commission_history | Working | Commission figures use placeholder rules until D8. |
| admin.summary / customers / tenants / tenant / retry_provisioning | Working | — |

**Placeholder business data** (seed script): prices ₹699 / ₹1,499 / ₹3,999 per month, yearly = 10 × monthly, GST 18 % excluded, 14-day trial starting at sign-up (OI-2), demo partner commission 10 %. These are for testing only (D6, D7, D8).

**Site config keys:** `growthx_web_url` (website base URL for reset and referral links), `growthx_press` (`{"mode": "fake"}` by default), `growthx_tenant_domain`, `growthx_tenant_login_path`, `razorpay_key_id`, `razorpay_key_secret`, `growthx_default_commission_rate`.
