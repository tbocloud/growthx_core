# GrowthX API: integration guide for the web app

**For:** Mubashir T (Web) · **From:** Niranjana C (Frappe) · **Date:** 12 Oct 2026
**Contract:** [`openapi.yaml`](../openapi.yaml) (OpenAPI v1, draft 2). If this guide and `openapi.yaml` disagree, `openapi.yaml` wins.
**Why things look the way they do:** [`api-decisions.md`](api-decisions.md).

This guide answers your Phase 0 request: base URL and route format, authentication and CSRF, every endpoint with real example responses, the four tenant capabilities, and test access. Examples were captured from the running API, with tokens removed. Two can't happen locally yet and are written from the schema instead: an invoice item (§5.6) and a successful checkout (§5.7).

> **Status:** every endpoint below is implemented and running on my **local bench**, not on staging. Staging depends on DevOps (Arun). Please review `openapi.yaml` in the PR and approve it, or comment, by **Fri 16 Oct**.

---

## 1. Base URL and route format

| Environment | Base URL (`GROWTHX_API_URL`) |
|---|---|
| Local (my laptop, office network only) | `http://192.168.1.13:8002/api/method/growthx_core.api.v1` |
| Staging | Pending DevOps. It will be `https://<core-staging-host>/api/method/growthx_core.api.v1` |
| Production | Pending DevOps |

Each endpoint is **base URL + `.` + `<module>.<function>`**. The joiner is a **dot**, not a slash, because the whole thing is one Frappe method name:

```
GET  {GROWTHX_API_URL}.catalogue.products
POST {GROWTHX_API_URL}.auth.login
GET  {GROWTHX_API_URL}.customer.site?id=GXT-00004
```

A slash (`{GROWTHX_API_URL}/catalogue.products`) is not a Frappe method and returns an error. In `openapi.yaml` the server URL ends at `/api/method` and each path is the full method name (`/growthx_core.api.v1.catalogue.products`), so generated clients build the right URL.

- These are Frappe whitelisted methods, so the paths aren't REST-style. REST paths like `/api/v1/...` would clash with Frappe's own `/api/v1` namespace.
- **GET** takes query parameters. **POST** takes a JSON body (`Content-Type: application/json`).
- **Ids are never in the path.** Use `?id=` on GET and `{"id": "..."}` in the body on POST.
- Quick check: `GET {GROWTHX_API_URL}.health.ping` → `{"status":"ok","version":"0.0.1"}`.

## 2. Conventions

| Topic | Rule |
|---|---|
| Response body | The body **is** the resource. There is **no** Frappe `{"message": …}` wrapper. |
| Errors | Any non-2xx returns `{"error": {"code", "message", "fields"?, "reference"?}}`. Branch on `code`; show `message`. `fields` maps form fields to messages. `reference` (on 5xx only) is a support id. Tracebacks are never returned. |
| Money | `{"amount": 149900, "currency": "INR"}`: integer **paise**. Prices also carry `gstIncluded`. |
| Dates | ISO 8601 UTC with `Z`, e.g. `2026-10-26T11:37:47Z`. Show them in IST. Months are `YYYY-MM`. |
| Lists | `?page=1&pageSize=20` (max 100) → `{items, page, pageSize, total}`. Admin lists also take `q`, `status` and `sort` (e.g. `sort=-createdAt`). |
| Ids | Opaque strings (e.g. `GXT-00004`, `solar-growth-month`). Don't parse them. |
| Idempotency | `auth.signup`, `billing.create_checkout` and `admin.retry_provisioning` accept an `Idempotency-Key: <uuid>` header. Same key + same body within 24 h → the original response. Same key + different body → `409 idempotency_conflict`. |
| Rate limits | Exceeding one → `429 rate_limited` with a `Retry-After` header (seconds). Login: 5 per email and 20 per IP per 15 min. Sign-up: 10 per IP per hour. Forgot password: 3 per email per 15 min. Contact form: 5 per IP per hour. |
| Caching | Every response sends `Cache-Control: no-store`. Don't cache them in the Next.js data cache. |

## 3. Authentication and sessions (OI-4)

**Chosen: option C from your §2.0.1. `growthx_core` issues its own bearer tokens.**

| | |
|---|---|
| Log in | `POST auth.login` → `accessToken`, `expiresAt`, `refreshToken`, `refreshExpiresAt`, `user` |
| Send | `Authorization: Bearer <accessToken>`, from the **Next.js server only**. Keep both tokens inside your encrypted `gx_session` cookie. |
| Access token | Valid for **1 hour** |
| Refresh token | Valid for **30 days**. `POST auth.refresh` returns a **new pair**, and the old refresh token **and** its access token stop working at once, so store the new pair straight away. If two requests refresh at the same moment, the second gets `401 invalid_token`. Do the refresh in one place on your server. |
| Expired or revoked access token | `401 unauthenticated` → refresh once → if that returns `401 invalid_token`, send the user to log in |
| Log out | `POST auth.logout` revokes the current access token and its refresh token |
| Password reset | Revokes **all** of the user's tokens |
| Disabled user | Their tokens stop working at once |
| `client` | Send `"client": "web"` on login and sign-up (Flutter sends `"mobile"`) |

**CSRF (your §2.0.4, Next.js → core leg):**
- **No Frappe CSRF token is needed.** Every v1 endpoint authenticates with the bearer token only.
- Frappe cookie sessions (`sid`) are **not accepted**. A request with only a cookie gets `401`.
- Browser CSRF doesn't apply to this leg, because the token never reaches the browser.
- `growthx_core` sends **no CORS headers**, so browsers can't call it directly. Only your server and the Flutter app call it.
- The browser → Next.js leg (OI-6: CSRF token or custom header) is still yours and the Lead's decision.

**Roles:** `user.role` is `customer`, `partner` or `admin`. Calling an endpoint for another role → `403 forbidden`. Asking for a resource that isn't yours (e.g. someone else's site) → `404 not_found`, never `403`.

## 4. Endpoints

**Phase** is when the endpoint must work on staging (1 Foundation, 2 Self sign-up, 3 Money). All of them already run locally.

| Method | Path (after base URL) | Auth | Phase | Replaces in your 02 contract |
|---|---|---|---|---|
| GET | `health.ping` | none | 0 | — |
| GET | `catalogue.products` | none | 1 | `GET /products` |
| GET | `catalogue.plans?product=solar` | none | 1 | `GET /plans` |
| POST | `contact.submit` | none | 2 | `POST /contact` |
| GET | `appointments.slots?days=28` | none | 2 | `GET /appointments/slots` |
| POST | `appointments.book` | none | 2 | `POST /appointments` |
| GET | `appointments.manage?token=` | none (token) | 2 | *new* |
| POST | `appointments.cancel` | none (token) | 2 | *new* |
| POST | `appointments.reschedule` | none (token) | 2 | *new* |
| GET | `partner.referrer?code=` | none | 2 | *new* (welcome page) |
| GET | `admin.appointments?status&expert&from&to&q&sort` | admin | 2 | *new* |
| GET | `admin.appointment?id=` | admin | 2 | *new* |
| POST | `admin.update_appointment` | admin | 2 | *new* |
| GET | `admin.appointment_experts` | admin | 2 | *new* |
| POST | `auth.login` | none | 1 | `POST /auth/login` |
| POST | `auth.signup` | none | 2 | `POST /auth/signup` |
| POST | `auth.google` | none | 2 | *new* (§5.12) |
| POST | `auth.send_signup_code` | none | 2 | *new* (§5.4) |
| POST | `auth.refresh` | none | 1 | *new* |
| POST | `auth.logout` | any | 1 | `POST /auth/logout` |
| GET | `auth.me` | any | 1 | `GET /me` |
| POST | `auth.forgot_password` | none | 1 | *new* |
| POST | `auth.reset_password` | none | 1 | *new* |
| GET | `customer.sites` | customer | 2 | `GET /customer/sites` |
| GET | `customer.site?id=` | customer | 2 | `GET /customer/sites/{id}` |
| POST | `customer.open_site` | customer | 2 | `POST /customer/sites/{id}/open` |
| GET | `customer.subscriptions` | customer | 3 | `GET /customer/subscriptions` |
| GET | `customer.invoices?status=` | customer | 3 | `GET /customer/invoices` |
| POST | `billing.create_checkout` | customer | 3 | `POST /customer/checkout` |
| POST | `billing.verify_checkout` | customer | 3 | `POST /customer/checkout/verify` |
| GET | `partner.summary` | partner | 2 | `GET /partner/summary` |
| GET | `partner.commission_history` | partner | 3 | `GET /partner/commission-history` |
| GET | `admin.summary` | admin | 3 | `GET /admin/summary` |
| GET | `admin.customers?q&status&sort` | admin | 2 | `GET /admin/customers` |
| GET | `admin.tenants?q&status&sort` | admin | 2 | `GET /admin/tenants` |
| GET | `admin.tenant?id=` | admin | 2 | `GET /admin/tenants/{id}` |
| POST | `admin.retry_provisioning` | admin | 2 | `POST /admin/tenants/{id}/retry` |

The other §2.4 gaps (payments list, invoice PDF, change plan, cancel, billing profile, partner commissions and payouts, admin billing, and so on) are **not in v1 yet**. Each will be added through a `contract` card in its phase; see `api-decisions.md` §4.

### The four tenant capabilities from your Web task

| Capability | Your proposed path | Final endpoint | Response |
|---|---|---|---|
| Customer's tenant list and status | `GET /customer/sites` | `GET customer.sites` | Paginated `Site` (§5.4) |
| Provisioning status for one tenant | `GET /customer/sites/{id}` | `GET customer.site?id={id}` | `SiteDetail`, with a `steps` list (§5.4) |
| Secure open-app link | `POST /customer/sites/{id}/open` | `POST customer.open_site` with body `{"id"}` | `{url, expiresAt}` (§5.5) |
| Admin provisioning retry | `POST /admin/tenants/{id}/retry` | `POST admin.retry_provisioning` with body `{"id"}` | `202` + `AdminTenantDetail` (§5.9) |

## 5. Examples

### 5.1 Log in, refresh, me, log out

```http
POST {GROWTHX_API_URL}.auth.login
{"email": "customer@growthx.test", "password": "…", "client": "web"}
```
```json
200
{
  "accessToken": "gxa_…",
  "expiresAt": "2026-10-12T12:54:09Z",
  "refreshToken": "gxr_…",
  "refreshExpiresAt": "2026-11-11T11:54:09Z",
  "user": {
    "id": "customer@growthx.test",
    "name": "Amina Noor",
    "email": "customer@growthx.test",
    "role": "customer",
    "company": "Noor Solar",
    "partnerStatus": null
  }
}
```
Wrong email or password, or a disabled account → `401 {"error": {"code": "invalid_credentials", "message": "The email or password is incorrect."}}`.

```http
POST {GROWTHX_API_URL}.auth.refresh
{"refreshToken": "gxr_…"}
```
→ `200`, same shape as login (a new pair). A spent or expired refresh token → `401 invalid_token`.

`GET {GROWTHX_API_URL}.auth.me` → the `user` object above. With no token or a bad token → `401 {"error": {"code": "unauthenticated", …}}`.

`POST {GROWTHX_API_URL}.auth.logout` with body `{}` → `200 {"ok": true}`.

**Partner** users have `"role": "partner"` and `"partnerStatus": "pending_approval" | "active" | "suspended"`. **Admins** have `"role": "admin"` and `"company": null`.

### 5.2 Forgot and reset password

```http
POST {GROWTHX_API_URL}.auth.forgot_password      {"email": "customer@growthx.test"}
```
→ always `202 {"ok": true}`, whether or not the email exists. The email links to **`{growthx_web_url}/reset-password?key=…`**, so the web app needs that page.

```http
POST {GROWTHX_API_URL}.auth.reset_password       {"key": "<key from the link>", "newPassword": "…"}
```
→ `200 {"ok": true}`. An unknown, used or expired key (24 h) → `400 invalid_reset_key`. A weak password → `422` with `fields.newPassword`.

### 5.3 Catalogue

`GET {GROWTHX_API_URL}.catalogue.products`
```json
{
  "items": [
    {"id": "solar", "slug": "solar", "name": "GrowthX Solar", "category": "Solar installers & distributors",
     "summary": "Site surveys, projects, installation and service for solar businesses.", "availability": "available"},
    {"id": "salon", "slug": "salon", "name": "GrowthX Salon", "category": "Salons & spas",
     "summary": "Bookings, stylists and client history in one calendar.", "availability": "coming_soon"}
  ]
}
```
`laundry` and `tailoring` follow, also `coming_soon`.

`GET {GROWTHX_API_URL}.catalogue.plans?product=solar` (an unknown product → `422`)
```json
{
  "items": [
    {
      "id": "solar-growth-month",
      "productSlug": "solar",
      "name": "Growth",
      "description": "For busy businesses ready to grow.",
      "price": {"amount": 149900, "currency": "INR", "gstIncluded": false},
      "interval": "month",
      "trialDays": 14,
      "features": ["Up to 10 staff logins", "Up to 3 locations", "Customer reminders by SMS", "Priority support & onboarding"],
      "highlighted": true
    }
  ]
}
```
Six plans in total: `solar-{starter,growth,scale}-{month,year}`.

**Each plan has one interval.** Group plans by `name` to show the monthly/yearly toggle, and send the matching plan `id`. The prices (₹699 / ₹1,499 / ₹3,999, yearly = 10 × monthly, GST excluded) are **placeholders** until the pricing decision (D6).

### 5.4 Sign-up, then poll provisioning

**First, verify the email** (not needed for a Google sign-up, §5.12):

```http
POST {GROWTHX_API_URL}.auth.send_signup_code     {"email": "customer@growthx.test", "client": "web"}
```
→ `202 {"expiresAt", "resendAfter"}`. The 6-digit code lasts 10 minutes; show the resend countdown until
`resendAfter` (60 s). Asking sooner → `429` with `Retry-After`. `409 email_taken` (with `fields.email`),
`503 email_unavailable` (couldn't send; try again). Then send the code as `emailCode` with the sign-up below.
A wrong code → `422` with `fields.emailCode` (it says how many tries are left); expired or 5 wrong tries →
`400 email_code_expired` (ask for a new code). Other form errors don't use the code up.

```http
POST {GROWTHX_API_URL}.auth.signup
Idempotency-Key: 6f1c2a8e-…
{
  "accountType": "customer",
  "name": "Ravi Menon",
  "company": "Menon Solar Works",
  "email": "ravi@example.com",
  "phone": "+919812345678",
  "password": "…",
  "productSlug": "solar",
  "planId": "solar-growth-month",
  "billingInterval": "month",
  "referralCode": "OKAFOR-7GB",
  "client": "web"
}
```
```json
201
{
  "accessToken": "gxa_…",
  "expiresAt": "2026-10-12T12:54:11Z",
  "refreshToken": "gxr_…",
  "refreshExpiresAt": "2026-11-11T11:54:11Z",
  "user": {"id": "ravi@example.com", "name": "Ravi Menon", "email": "ravi@example.com",
           "role": "customer", "company": "Menon Solar Works", "partnerStatus": null},
  "site": {"id": "GXT-00024", "status": "requested"}
}
```

**Required fields:**
- `accountType`, `name`, `email` and `password` (at least 8 characters, and it must pass the site's strength policy).
- `company`, for customers.
- `productSlug`, `planId` and `billingInterval` are optional. With only `productSlug`, the product's highlighted plan is used.

**Behaviour:**
- **Sign-up returns straight away; it doesn't wait for the site.** `site` is only present for a customer who picked a product.
- A **partner** sign-up returns no `site`, and the user has `partnerStatus: "pending_approval"` until an admin approves them.
- An unknown or self-referral code is **ignored silently**; sign-up never fails because of it.

**Errors:**
- `409 email_taken` with `fields.email`.
- `422 validation_failed` with `fields`. For example, a weak password gives `{"fields": {"password": "This is a very common password."}}`.
- `422` if the product is `coming_soon`.

**Then poll** `GET {GROWTHX_API_URL}.customer.site?id=GXT-00024` every 5 s while the status is `requested` or `provisioning`. Stop at `ready` or `failed`, or after 15 min.

```json
{
  "id": "GXT-00004",
  "productSlug": "solar",
  "displayName": "Noor Solar",
  "status": "ready",
  "statusMessage": null,
  "subscriptionId": "GXS-00003",
  "createdAt": "2026-10-12T11:37:47Z",
  "readyAt": "2026-10-12T11:37:48Z",
  "steps": [
    {"key": "create_site",   "label": "Creating your site",        "state": "done", "startedAt": "2026-10-12T11:37:48Z", "finishedAt": "2026-10-12T11:37:48Z"},
    {"key": "wait_for_site", "label": "Setting up the server",     "state": "done", "startedAt": "…", "finishedAt": "…"},
    {"key": "install_apps",  "label": "Installing apps",           "state": "done", "startedAt": "…", "finishedAt": "…"},
    {"key": "configure",     "label": "Configuring your business", "state": "done", "startedAt": "…", "finishedAt": "…"},
    {"key": "health_check",  "label": "Final checks",              "state": "done", "startedAt": "…", "finishedAt": "…"}
  ]
}
```

| `status` | Meaning | `statusMessage` |
|---|---|---|
| `requested` | Queued after sign-up | `"Preparing your site."` |
| `provisioning` | Steps are running; each step's `state` is `pending`, `running`, `done` or `failed` | `"Preparing your site."` |
| `ready` | The site can be opened | `null` |
| `failed` | Admins are alerted and will retry. **Don't** ask the customer to sign up again. | `"We hit a problem while preparing your site. Our team has been alerted and will fix it shortly."` |
| `suspended`, `archived` | Phase 3 (non-payment, cancellation) | — |

`statusMessage` is always safe to show the customer. Internal error detail only appears in the admin endpoints. A site that isn't the caller's returns `404`.

`GET {GROWTHX_API_URL}.customer.sites?page=1&pageSize=20` returns the same fields as above without `steps`, as `{items, page, pageSize, total}`. Use it when the customer comes back to the dashboard.

> Locally, provisioning uses a **fake Press** and finishes in about a second. On real Press it will take minutes; DevOps will measure it on staging.

### 5.5 Open-app link

```http
POST {GROWTHX_API_URL}.customer.open_site        {"id": "GXT-00004"}
```
```json
200   (Cache-Control: no-store)
{"url": "https://noor-solar.<tenant-domain>/api/method/growthx_theme.sso.login?token=…", "expiresAt": "2026-10-12T11:55:10Z"}
```

**Rules:**
- The link is **single-use and valid for 60 s**. Request it only when the user clicks "Open my app", then navigate to it straight away. Never store, log or prefetch it.
- If the site isn't `ready` → `409 site_not_ready`.
- Someone else's site → `404`.

**What happens when the browser opens the link** (nothing for the web app to do beyond navigating):
- The tenant site (`growthx_theme`) redeems the token with Core, server to server, logs the customer in **as their own user** and redirects to the desk (`/desk`; in Frappe v16 `/app` redirects there too). The token doesn't stay in the address bar.
- The customer never sees the tenant's login page and has no tenant password.
- An expired (over 60 s), reused or wrong-site link ends on the tenant's `/login` page with "This link has expired. Open your app again from GrowthX." The fix for the user is to click "Open my app" again.
- If GrowthX is down, the customer can still get in with **"Login with Email Link"** on the tenant's login page (needs outgoing email on the tenant).

### 5.6 Subscriptions and invoices

`GET {GROWTHX_API_URL}.customer.subscriptions`
```json
{
  "items": [
    {
      "id": "GXS-00003",
      "productName": "GrowthX Solar",
      "planName": "Growth",
      "status": "trialing",
      "price": {"amount": 149900, "currency": "INR", "gstIncluded": false},
      "interval": "month",
      "trialEndsAt": "2026-10-26T11:37:47Z",
      "currentPeriodEnd": null,
      "cancelAtPeriodEnd": false,
      "siteId": "GXT-00004"
    }
  ],
  "page": 1, "pageSize": 20, "total": 1
}
```
`status` is one of `trialing`, `active`, `past_due`, `suspended` or `canceled`.

`GET {GROWTHX_API_URL}.customer.invoices?page=1&pageSize=10&status=paid` returns `{items, page, pageSize, total}`. **It's empty until Phase 3** creates ERPNext Sales Invoices. Each item looks like this:
```json
{"id": "ACC-SINV-2026-00001", "number": "ACC-SINV-2026-00001", "status": "paid",
 "subtotal": {"amount": 149900, "currency": "INR"}, "tax": {"amount": 26982, "currency": "INR"},
 "total": {"amount": 176882, "currency": "INR"}, "issuedAt": "2026-10-31T18:30:00Z", "dueAt": "2026-11-07T18:30:00Z"}
```
Invoice `status` is one of `draft`, `open`, `overdue`, `paid` or `void`. There is no `uncollectible`.

### 5.7 Checkout (OI-3: your gateway-neutral shape, adopted)

```http
POST {GROWTHX_API_URL}.billing.create_checkout
Idempotency-Key: …
{"planId": "solar-growth-year", "billingInterval": "year", "subscriptionId": "GXS-00003"}
```
→ `201`:
```json
{
  "provider": "razorpay",
  "reference": "sub_NgQ8kTgV5bE2xY",
  "publicKey": "rzp_test_…",
  "plan": {"id": "solar-growth-year", "name": "Growth", "interval": "year"},
  "amount": {"amount": 1499000, "currency": "INR", "gstIncluded": false},
  "description": "GrowthX Solar Growth — billed yearly",
  "prefill": {"name": "Amina Noor", "email": "customer@growthx.test", "phone": "+919876543210"}
}
```
`subscriptionId` is the trial subscription being converted. Leave it out for a new subscription.

```http
POST {GROWTHX_API_URL}.billing.verify_checkout
{"provider": "razorpay", "reference": "sub_NgQ8kTgV5bE2xY", "payload": { /* exactly what Razorpay Checkout returned */ }}
```
→ `200 {"subscription": Subscription, "invoice": null}`. The invoice arrives later through the gateway webhook. Repeating the call is safe.

**Errors:**
- `400 payment_verification_failed`.
- `404` for an unknown `reference`.
- `422 invalid_plan`.
- **`503 payment_unavailable`**, which is **what you get today**: no Razorpay keys are configured yet (D5). Show a friendly "payments are not available yet" message.

### 5.8 Partner

`GET {GROWTHX_API_URL}.partner.summary`
```json
{
  "referralCode": "OKAFOR-7GB",
  "referralUrl": "{growthx_web_url}/r/OKAFOR-7GB",
  "activeCustomers": 0,
  "referredSignups": 1,
  "openLeads": null,
  "monthlyRecurringCommission": {"amount": 0, "currency": "INR"},
  "pendingPayout": {"amount": 0, "currency": "INR"}
}
```
- Use `referralUrl` for the share link and the QR code. **Your sign-up page should read `?ref=` and send it as `referralCode`.**
- `openLeads` is `null` until leads are agreed.

`GET {GROWTHX_API_URL}.partner.commission_history` → `{"items": [{"month": "2025-11", "amount": {"amount": 0, "currency": "INR"}}, …]}`: always 12 months, oldest first. The commission figures use placeholder rules until D8.

### 5.9 Admin

`GET {GROWTHX_API_URL}.admin.summary` → `{"mrr": {"amount": 0, "currency": "INR"}, "activeCustomers": 0, "trialCustomers": 3, "suspendedCustomers": 0, "activePartners": 1}`. MRR excludes GST.

`GET {GROWTHX_API_URL}.admin.customers?q=noor&status=trial&page=1&pageSize=20&sort=-createdAt`
- `sort` is `createdAt` or `company`, with `-` for descending.
- `q` searches the company and the contact email.
```json
{"items": [{"id": "GXA-00002", "company": "Noor Solar", "contactEmail": "customer@growthx.test", "status": "trial",
            "plan": "Growth", "partnerName": "Chidi Okafor", "mrr": {"amount": 0, "currency": "INR"},
            "createdAt": "2026-10-12T11:37:47Z"}],
 "page": 1, "pageSize": 20, "total": 1}
```
Customer `status` is one of `trial`, `active`, `suspended` or `churned`.

`GET {GROWTHX_API_URL}.admin.tenants?status=failed&q=&sort=-updatedAt`
- `sort` is `createdAt` or `updatedAt`.
- `q` searches the customer, site URL and app.
```json
{"items": [{"id": "GXT-00004", "customerId": "GXA-00002", "customerName": "Noor Solar", "productSlug": "solar",
            "siteUrl": "https://noor-solar.growthx.local", "status": "ready", "lastError": null, "attempts": 1,
            "createdAt": "2026-10-12T11:37:47Z", "updatedAt": "2026-10-12T11:37:48Z"}],
 "page": 1, "pageSize": 20, "total": 1}
```

`GET {GROWTHX_API_URL}.admin.tenant?id=GXT-00004` returns the same fields plus the provisioning log in `steps`. Each step adds `attempt` and `error`:
```json
"steps": [{"key": "create_site", "label": "Creating your site", "state": "done", "startedAt": "…", "finishedAt": "…", "attempt": 1, "error": null}, …]
```
For a failed tenant, `lastError` is something like `"configure: <detail>"`, and the failed step has `state: "failed"` and an `error`. This detail is for admins only.

```http
POST {GROWTHX_API_URL}.admin.retry_provisioning
Idempotency-Key: …
{"id": "GXT-00004"}
```
- **`202`** + the tenant detail (as `admin.tenant`). The retry resumes **from the failed step**.
- If a job is already running, you get `202` with its current state, and no new job starts.
- If the tenant isn't `failed` → `409 {"error": {"code": "not_retryable", "message": "Only a failed site can be retried."}}`.

**Partners** (`/admin/partners`):
- `GET admin.partners?q=&status=&page=&pageSize=&sort=` → every partner (not only those with referrals): `id`, `name`,
  `email`, `company`, `referralCode`, `status` (`pending_approval` / `active` / `suspended`), `tier`, `joinedAt`,
  `referredCustomers`, `payingCustomers`, `mrr`. `q` searches name, company, referral code and email.
- `GET admin.partner?id=` → the same plus `phone`, `city`, `gstin`, `customers` (each referred customer with status,
  plan and MRR) and `history` (who approved or suspended them, when, and why).
- `POST admin.update_partner_status {id, action, reason?}`: `approve` (pending → active; creates their commission
  record in ERPNext and emails them), `suspend` (`reason` required; pending or active → suspended; their code stops
  working for new sign-ups; use it to reject an application too), `reactivate` (suspended → active). Returns the
  partner's details. Wrong state → `409 not_changeable`.

### 5.10 Contact form

`POST {GROWTHX_API_URL}.contact.submit` with body `{"name", "email", "company"?, "message"}` → `202 {"ok": true}`. A bad email gives `422` with `fields.email`.

### 5.11 Talk to an expert (appointments)

`GET {GROWTHX_API_URL}.appointments.slots?days=28` (1 to 60, default 28) → `{"timezone": "Asia/Kolkata", "durationMinutes": 30, "slots": ["2026-10-14T04:30:00Z", …]}`. Only free start times at least 2 hours away. Show them in `timezone`. Don't cache.

`POST {GROWTHX_API_URL}.appointments.book` with `{"start", "name", "email", "phone", "company"?, "app"?, "notes"?}` → `201 {"id", "status": "confirmed", "start", "end"}`.
- `start` must be one of the slots, in UTC. `phone` is a 10-digit Indian mobile, optionally with `+91`; spaces and hyphens are ignored.
- `409 slot_taken` if someone booked it first: reload the slots and ask the visitor to choose again.
- Accepts `Idempotency-Key`. Rate limit: 5 bookings per IP per hour.
- The API assigns an expert, adds the video link, records a Lead and an Event in ERPNext and emails the confirmation with a calendar invite. Opening hours, holidays, experts and the link template are in **GX Appointment Settings** on the core site.

**Workflow after booking** (instant confirmation; the team can change it afterwards):

| Status | Meaning |
|---|---|
| `confirmed` | Booked. The only status that can be rescheduled, reassigned or cancelled |
| `completed` | The call happened (set by the team once it has started) |
| `no_show` | The visitor didn't join (set by the team once it has started) |
| `cancelled` | By the visitor (manage link) or the team. Frees the slot |

- **Visitor:** the confirmation email links to `{growthx_web_url}/appointments/manage?token=…`. `appointments.manage` returns the booking and `canChange`; `appointments.cancel {token, reason?}` and `appointments.reschedule {token, start}` change it. The token is stored only as a hash and is only in the first email.
- **Team:** `admin.appointments` lists and filters; `admin.appointment?id=` adds `adminNotes`, `cancelledBy`, `cancelReason` and `history` (from GX Audit Log); `admin.update_appointment {id, action, …}` with `action` = `reschedule` {start} · `reassign` {expert} · `cancel` {reason?} · `complete` · `no_show` · `note` {note}. Conflicts: `409 slot_taken`, `expert_busy`, `not_changeable`, `not_started`.
- **Emails:** every change emails the visitor (copy to the expert) with an updated calendar invite (`.ics` with the same UID, so calendars move or cancel the event). Reminders go out 24 h and 1 h before the call (scheduler, every 5 minutes; a call booked less than 24 h ahead gets only the 1-hour one).
- **ERPNext:** the Event moves or is cancelled with the booking.

### 5.12 Sign in with Google (web)

The website runs the Google redirect; Core does the code exchange (it holds the client secret).

1. **"Continue with Google"** → redirect to `https://accounts.google.com/o/oauth2/v2/auth` with `client_id`,
   `redirect_uri={website}/api/auth/google/callback`, `response_type=code`, `scope=openid email profile`, a random
   `state` (keep it in a short-lived httpOnly cookie) and, recommended, PKCE (`code_challenge`, `S256`).
   - Client ID: production `942801595002-v2nb2bdu6vl3t4dfun707sqkco3f7je9.apps.googleusercontent.com`; core-test
     (your local site on port 3000) `942801595002-he8h4q519jkgesln920aq39d14pinu5p.apps.googleusercontent.com`.
     The client ID is public; the secret is only on Core.
2. **`GET /api/auth/google/callback`** (Next.js route): check `state`, then server-to-server:

```http
POST {GROWTHX_API_URL}.auth.google     {"code": "<code>", "codeVerifier": "<if PKCE>"}
```

- `200` → the same session as `auth.login`: store it and go to the dashboard.
- `202 {"signupRequired": true, "signupToken": "gxg_…", "expiresAt", "email", "name"}` → no account yet. Show the
  sign-up form with name and email filled in (email read-only, no password fields), then send
  `auth.signup` with `"googleSignupToken": "<signupToken>"` instead of `email` and `password` (everything else as
  usual: `accountType`, `company`, plan, site choices). The token lasts 30 minutes; after a 422 or 409 it still
  works, so the user can fix the form. `400 google_signup_expired` → start again from step 1.
- `400 google_failed` (code used, expired, or redirect URI mismatch), `403 email_not_verified`,
  `403 account_unavailable` (disabled or staff account), `503 google_unavailable` → back to the login page with a
  message.

Core sends Google the redirect URI from its config, so it must be exactly the one in step 1. An existing
email-and-password account can also sign in with Google (same email); a Google-only account can set a password
later with "Forgot password".

## 6. Error codes

| HTTP | `code` | When |
|---|---|---|
| 400 | `bad_request` | Body isn't a JSON object |
| 400 | `invalid_reset_key` | Reset key unknown, used or expired |
| 400 | `google_failed`, `google_signup_expired` | Google sign-in (§5.12) |
| 400 | `email_code_expired` | Sign-up email code expired, never sent, or 5 wrong tries (§5.4) |
| 403 | `email_not_verified`, `account_unavailable` | Google sign-in (§5.12) |
| 400 | `payment_verification_failed` | Gateway signature doesn't match |
| 401 | `unauthenticated` | No token, or the token expired or was revoked → refresh once, then log in |
| 401 | `invalid_credentials` | Wrong email or password |
| 401 | `invalid_token` | Refresh token unusable → log in |
| 403 | `forbidden` | Wrong role for this endpoint |
| 404 | `not_found` | Doesn't exist, or isn't yours |
| 405 | `method_not_allowed` | e.g. POST to a GET endpoint |
| 409 | `email_taken`, `site_not_ready`, `not_retryable`, `idempotency_conflict`, `slot_taken` | See the endpoints above |
| 422 | `validation_failed`, `invalid_plan` | See `fields` |
| 429 | `rate_limited` | Wait `Retry-After` seconds |
| 500 | `internal_error` | Show a friendly message with `reference` |
| 503 | `payment_unavailable` | Gateway not configured or unreachable |
| 503 | `google_unavailable` | Google sign-in not configured or Google unreachable |
| 503 | `email_unavailable` | The sign-up code email couldn't be sent |

## 7. What the web app needs to add

1. **Unwrap nothing.** Read the body directly, and map errors from `error.code`.
2. **Session:**
   - store `refreshToken` and `refreshExpiresAt` next to `accessToken` in `gx_session`,
   - refresh on the server before `expiresAt` (or on a 401), in a single place,
   - replace both tokens after every refresh.
3. **Routes:** switch the paths to the table in §4 (ids go in `?id=` or the body).
4. **Plans:** use per-interval plan ids. Group by `name` for the toggle.
5. **Changed shapes:**
   - Invoice has `subtotal`, `tax` and `total`, with statuses `draft | open | overdue | paid | void`.
   - Subscription adds `suspended`, `trialEndsAt`, `cancelAtPeriodEnd` and `siteId`.
   - Plan adds `productSlug`. Price adds `gstIncluded`.
   - Product adds `availability`.
   - PartnerSummary adds `referralUrl`, and `openLeads` can be `null`.
6. **Checkout:** use the neutral shape (OI-3), and handle `503 payment_unavailable`.
7. **New pages:**
   - `/reset-password?key=…` (from the reset email),
   - `/r/CODE` (referral welcome page: "<partner> invited you"; also accepts `/signup?ref=CODE`),
   - `/dashboard` (linked from the "site is ready" email).

   Tell me if your paths differ; they're one setting (`growthx_web_url`) plus these paths.
8. **Mock:** regenerate it from `openapi.yaml`.

## 8. Test access

| | |
|---|---|
| Local API | `http://192.168.1.13:8002/api/method/growthx_core.api.v1`. Works only while my laptop is serving and you're on the office network. Ping me if it's down. |
| Demo users | `customer@growthx.test` (has a ready Solar site), `partner@growthx.test` (active partner, code `OKAFOR-7GB`), `admin@growthx.test` |
| Password | Sent to you separately. It is **not** in this file or in Git. |
| Make your own | `auth.signup` with any email. Provisioning finishes in about 1 s locally. |
| Staging | Pending DevOps. I'll send the URL and test accounts the same way once it exists. |

## 9. Open items affecting the web app

| ID | Item | State |
|---|---|---|
| OI-2 | Trial starts at sign-up or when the site is ready? Account kept if provisioning fails? | **Implemented for now:** the trial starts at sign-up, and the account is always kept. Waiting on the Lead. |
| OI-3 | Payment contract shape | Your neutral shape is in `openapi.yaml`. Please confirm. |
| OI-4 | Auth option | Option C, as above. Please confirm. |
| OI-5 | Base URL, route format, envelope | As above. Please confirm. |
| OI-6 | Browser → Next.js CSRF | Yours and the Lead's |
| — | Open-app token exchange on the tenant | **Done** (`growthx_theme.sso.login` + `tenant.redeem_login`). Works locally end to end; staging needs Press to write the tenant's site config (DevOps). |
| — | Appointments: video provider and the expert list | Endpoints are live. Which video service to use (the link template) and who the experts are: the Lead. Until set, the confirmation says the link follows separately. |
| — | Caching `catalogue.products` / `catalogue.plans` for 5 minutes on the website | Web's proposal. **To confirm:** can a price change take up to 5 minutes to show? `appointments.slots` must never be cached. |
| — | Real Press provisioning | DevOps (Press API), then me |
| — | Business rules (prices, GST, trial, commission) | D6, D7 and D8 with the Lead. The data shown is placeholder. |
