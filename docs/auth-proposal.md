# GrowthX authentication proposal

**Task:** Phase 0, deliverable 2 (`02-TASK-phase-0-alignment.md`) · **Author:** Niranjana C · **Date:** 13 Oct 2026
**Decision needed from:** Sammish · **For information:** Mubashir T (Web), Najath Thekkeyil (Flutter)
**Closes:** decision D4 in the decisions log, and open item OI-4 in the web contract (`02-api-contract.md` §2.0.1)

## 1. Decision requested

**Approve option C: `growthx_core` issues its own short-lived access tokens and rotating refresh tokens.**
- **Web** keeps them in an encrypted httpOnly cookie on the Next.js server.
- **Flutter** keeps them in secure storage.

The option is already built and tested on the local bench. Nothing is on staging yet, because staging doesn't exist.

## 2. Versions and what they support

| | Version | Source |
|---|---|---|
| Frappe | **16.35.0** | Local bench `frappe-bench-v16` |
| ERPNext | **16.35.0** | Local bench |

Arun (DevOps) must pin these same versions in the Core release group on Press.

What Frappe v16 offers for logging in, checked in the v16 source:

| Option | What v16 supports | Problem for GrowthX |
|---|---|---|
| **A. Frappe session cookie (`sid`)** | `/api/method/login` sets a `sid` cookie | No refresh token, so Flutter has to keep the password or log in again. Every write needs Frappe's CSRF token. The cookie is tied to Frappe's browser-session model, which isn't built for a server-to-server client like the Next.js server. |
| **B. Frappe OAuth2 provider** | Grants: `authorization_code` (with **PKCE**, S256 or plain) and `refresh_token` only (`frappe/oauth.py`, `validate_grant_type`). **No password grant.** | Users would log in on **Frappe's own `/login` page** and be redirected back. That breaks the web team's login and sign-up forms, which are already built, and shows a Frappe-branded page (against white-labelling). Tokens are stored **in plain text**: the token is the `OAuth Bearer Token` document's name. Sign-up still needs a custom endpoint anyway. |
| **C. Token endpoint in `growthx_core`** (recommended) | Custom whitelisted methods, plus Frappe's `auth_hooks` to check our token on every request | We write and test it ourselves (done: about 200 lines plus tests). |
| D. Per-user API key and secret | `Authorization: token key:secret` | Long-lived with no expiry, so a leaked key works until someone notices. Not suitable for end users. |

## 3. Recommendation: option C

**Why:**
1. **It fits what's already built.** Web's login and sign-up forms and Flutter's login screen post an email and password. Option C accepts exactly that, and B can't.
2. **The same API works for both clients.** Only where the tokens are kept differs.
3. **It's more secure than B as Frappe ships it.** Tokens are random, and only their SHA-256 hashes are stored. A database leak or a desk user can't reuse them.
4. **There's no Frappe branding and no Frappe pages**, as white-labelling requires.
5. **It needs no CSRF token** on the Core API (§6), which simplifies Web.

**Cost:** we own the code. It's small, covered by tests, and uses Frappe's own password check, reset keys and user status.

## 4. How it works

### 4.1 Tokens

| | Access token | Refresh token |
|---|---|---|
| Format | `gxa_` + 43 random characters | `gxr_` + 64 random characters |
| Lifetime | **1 hour** | **30 days** |
| Stored on the server | SHA-256 hash only (`GX Auth Token`) | SHA-256 hash only |
| Used for | `Authorization: Bearer gxa_…` on every API call | Only for `auth.refresh` |
| On use | — | **Rotated**: a new pair is issued, and the old pair is revoked at once |

Each login creates one token pair per device (`client: web | mobile`), so logging out on a phone doesn't log the user out on the web.

### 4.2 Web (Next.js), using an httpOnly cookie through the Next.js server

```
Browser ──form──► Next.js server ──POST auth.login {email, password, client:"web"}──► growthx_core
                                ◄── {accessToken, expiresAt, refreshToken, refreshExpiresAt, user}
        ◄── Set-Cookie: gx_session (encrypted, httpOnly, Secure, SameSite=Lax), holding both tokens
Browser ──/api/gx/* ──► Next.js server ── Authorization: Bearer gxa_… ──► growthx_core
```

**The browser never sees a token.** The rest of the flow:

| Flow | What happens |
|---|---|
| **Log in** | The Next.js server calls `auth.login` and stores both tokens in `gx_session`. |
| **Sign up** | Same as login, via `auth.signup` (returns `201` with the same token pair). |
| **Refresh** | Before `expiresAt`, or after a `401`, the Next.js server calls `auth.refresh` and **replaces both tokens** in the cookie. Run this in one place on the server, so two requests don't refresh at once (the second would get `401 invalid_token`). |
| **Log out** | The Next.js server calls `auth.logout`, which revokes this device's tokens, then deletes the cookie. |
| **Session ends** | `auth.refresh` returns `401 invalid_token` (refresh token expired, spent or revoked) → send the user to the login page. |
| **Forgot password** | `auth.forgot_password {email}` → always `202`. The email links to `{website}/reset-password?key=…`. |
| **Reset password** | That page posts `auth.reset_password {key, newPassword}`. This **revokes all the user's tokens** on every device. |

### 4.3 Flutter, using tokens in secure storage

| Flow | What happens |
|---|---|
| **Log in** | `auth.login {email, password, client:"mobile"}` → save both tokens in `flutter_secure_storage`. |
| **API calls** | A Dio interceptor adds `Authorization: Bearer gxa_…`. |
| **Refresh** | On a `401`, the interceptor calls `auth.refresh` **once** (lock it so parallel requests wait for one refresh), saves the new pair and retries the request. If the refresh returns `401`, clear the storage and go to login. This matches the Flutter role's "refresh once, then log out". |
| **Log out** | `auth.logout`, then clear the secure storage. |
| **Forgot or reset password** | Same endpoints as Web. The reset link opens the website page. Opening it in the app is a later improvement. |

### 4.4 On the server, for every request
1. Frappe calls our `auth_hooks` entry (`growthx_core.tokens.authenticate_request`).
2. It hashes the bearer token and looks it up. The token must be `Active` and not expired, and the user must be **enabled**.
3. If it passes, the request runs as that user. The endpoint then checks the **role** (`customer`, `partner` or `admin`) and **ownership** (a customer sees only their own sites; someone else's site returns `404`).

## 5. Security controls (already built)

| Control | Detail |
|---|---|
| No token readable at rest | Only SHA-256 hashes are stored. Tokens are never logged. |
| Short exposure | Access token 1 h, refresh token 30 d, rotated on every use |
| Revocation | On logout (this device), password reset (all devices), and a disabled user (immediately) |
| Brute force | Login: 5 tries per email and 20 per IP per 15 min. Sign-up: 10 per IP per hour. Forgot password: 3 per email per 15 min. Exceeding a limit → `429` with `Retry-After`. |
| No account discovery | A wrong email and a wrong password give the same `401 invalid_credentials`. Forgot password always returns `202`. |
| Passwords | Frappe's own hashing and checks. Minimum 8 characters, plus the site's password-strength policy. |
| Who can log in | Only users with a GrowthX role. Frappe system users (e.g. Administrator) can't get an API token. |
| No cookies on the API | `growthx_core` ignores Frappe `sid` cookies on v1 endpoints and sends no CORS headers, so browsers can't call it directly. |
| Reset links | Single use. Valid for 24 hours, or the site's own setting. Only the hash is stored. |
| Clean-up | Expired and revoked token rows are deleted daily. |

## 6. CSRF

- **Browser → Next.js:** the web team owns this (`SameSite=Lax`, origin check, plus the token-or-header decision OI-6).
- **Next.js → `growthx_core`:** **no CSRF token needed.** The call carries a bearer token in a header, not a cookie, so a malicious site can't make the browser send it. Frappe's `X-Frappe-CSRF-Token` isn't used.
- **Flutter → `growthx_core`:** not applicable (no browser).

## 7. Status

| Item | State |
|---|---|
| Endpoints `auth.login`, `signup`, `refresh`, `logout`, `me`, `forgot_password`, `reset_password` | Built (`growthx_core/api/v1/auth.py`, `growthx_core/tokens.py`), on the `growthx` branch |
| Tests | Unit tests (`tests/test_tokens.py`: hashing, expiry, revocation, rotation) pass. An HTTP smoke test of the full login → refresh → logout cycle passes locally. |
| Contract | `openapi.yaml` (draft 2): `bearerAuth`, `AuthSession`, and the error codes `unauthenticated`, `invalid_credentials`, `invalid_token`, `invalid_reset_key`, `rate_limited` |
| Staging | **Not yet.** Needs the Core staging site from DevOps. Under the Definition of Done, this isn't "done" until it's verified there. |
| Reset emails | Need an outgoing email account (SES) on the Core site (DevOps) |

## 8. Known limits and follow-ups

| # | Limit | Plan |
|---|---|---|
| 1 | Two refreshes at the same moment: the second fails | Web and Flutter refresh in one place (§4.2, §4.3). If it still causes trouble on staging, add a short grace window on the server. |
| 2 | No two-factor login (MFA) | Not a launch requirement. Can be added to `auth.login` later without changing the token design. |
| 3 | No "reuse detection" (a stolen, already-used refresh token doesn't revoke the whole session) | Low risk with 1 h access tokens. Can be added later. |
| 4 | "Log out of all devices" isn't an endpoint | Password reset already does this. Add an endpoint if the business wants it. |
| 5 | Signing in to tenant ERP sites ("Open my app") | **Built (13 Oct 2026, PR #3):** a separate one-time 60-second link, not these tokens. The tenant redeems it with Core using its own key (`tenant.redeem_login`) and logs the customer in. Customers have no tenant password; "Login with Email Link" is the fallback. See `api-decisions.md` and `growthx_theme`. |

## 9. Decisions for Sammish

| # | Question | Recommendation |
|---|---|---|
| 1 | Approve option C for Web and Flutter? | **Yes** |
| 2 | Token lifetimes: access 1 h, refresh 30 d | Yes. Shorter refresh (e.g. 7 d) means more logins on mobile. |
| 3 | Logout scope: this device only | Yes. Password reset logs out everywhere. |
| 4 | Password reset link validity | 24 hours, single use |
| 5 | Two-factor login | After launch |

Once these are approved, I'll record D4 as decided in a `DECISION` file, and Web and Flutter can build against it. They already can: the contract is in `openapi.yaml`.
