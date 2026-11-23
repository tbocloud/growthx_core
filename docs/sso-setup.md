# Social sign-in setup (Microsoft, GitHub and Apple)

Google sign-in is already configured. Microsoft, GitHub and Apple need an app each (Apple: see `docs/apple-signin-setup.md`); nothing works until the keys below are
set, and each provider answers `503 <provider>_unavailable` (button hidden on the website) until then.

## 1. Microsoft Entra app (one multi-tenant registration)

Sign in to <https://entra.microsoft.com> with an account in the **teambackoffice.com** directory.

1. **Identity > Applications > App registrations > New registration**.
   - Name: `TBO GrowthX`.
   - Supported account types: **Accounts in any organizational directory (Any Microsoft Entra ID tenant -
     Multitenant) and personal Microsoft accounts (e.g. Skype, Xbox)**.
   - Redirect URI: platform **Web**, `https://tbogrowthx.com/api/auth/microsoft/callback`.
     (For local testing add `http://localhost:3000/api/auth/microsoft/callback` as a second URI.)
2. Copy **Application (client) ID** and **Directory (tenant) ID** from the Overview page. The directory ID is the
   *staff* tenant: people signing in from it become GrowthX Admins.
3. **Token configuration > Add optional claim > ID**: add **`email`** and **`xms_edov`** (accept the prompt to turn on
   the Microsoft Graph `email` permission). `xms_edov` tells Core that a work account's email domain is verified; without it
   a customer's work email is treated as unverified (they can still sign up, with an emailed code, but are never
   matched to an existing account by email).
4. **Certificates & secrets > New client secret**; copy the **Value** now (it is shown once). Note the expiry date.
5. Leave the manifest's `requestedAccessTokenVersion` at its default; Core reads the **ID token**, which is v2.0
   (`iss` = `https://login.microsoftonline.com/{tid}/v2.0`).

Core `site_config` (growthx-core.tbogrowthx.com):

| Key | Value |
|---|---|
| `microsoft_oauth_client_id` | Application (client) ID |
| `microsoft_oauth_client_secret` | the secret Value |
| `microsoft_oauth_staff_tenant_id` | the teambackoffice.com Directory (tenant) ID. The old `microsoft_oauth_tenant_id` is read when this is unset (GUID only; `organizations` means no staff) |
| `microsoft_oauth_redirect_uri` | optional; default `<growthx_web_url>/api/auth/microsoft/callback` |

Website `/etc/growthx-web/app.env`, then `systemctl restart growthx-web`:

```
MICROSOFT_CLIENT_ID=<Application (client) ID>
```

(`MICROSOFT_TENANT_ID` is no longer used; the authority is always `common`.)

Behaviour: a token from the staff directory signs in as `GrowthX Admin` (a user is created on the first sign-in, with no
password; a user disabled in GrowthX stays blocked; directory guests are not staff). Anyone else is a customer and
never gets admin, even with an `@teambackoffice.com` address.

## 2. GitHub OAuth app

<https://github.com/organizations/tbocloud/settings/applications> > **New OAuth App** (an organization-owned app, not a
personal one).

- Application name: `TBO GrowthX`; Homepage URL: `https://tbogrowthx.com`.
- Authorization callback URL: **`https://tbogrowthx.com/api/auth/github/callback`** (GitHub allows one; make a second
  OAuth app for local testing with `http://localhost:3000/api/auth/github/callback`).
- Leave "Enable Device Flow" off. After creating it, **Generate a new client secret** and copy it.

Core `site_config`:

| Key | Value |
|---|---|
| `github_oauth_client_id` | the Client ID |
| `github_oauth_client_secret` | the client secret |
| `github_oauth_redirect_uri` | optional; default `<growthx_web_url>/api/auth/github/callback` |

Website `/etc/growthx-web/app.env`, then `systemctl restart growthx-web`:

```
GITHUB_CLIENT_ID=<Client ID>
```

Scope requested: `read:user user:email`. Core uses only the primary, verified email (never a `noreply` address).

## 3. Checklist after configuring

1. `https://tbogrowthx.com/login` shows Continue with Google / Microsoft / GitHub / Apple (each only when its env is set).
2. A teambackoffice.com person signs in with Microsoft and lands in `/admin`.
3. A personal Microsoft or GitHub account with a new email gets the sign-up form (email read-only); after submitting it
   has a customer account.
4. Rotate secrets before they expire (Entra secrets have an expiry date) and update `site_config`.
