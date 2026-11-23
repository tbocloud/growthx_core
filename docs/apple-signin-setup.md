# Sign in with Apple: setup for DevOps

"Continue with Apple" on `tbogrowthx.com` lets customers sign up and sign in with their Apple ID. Nothing works until
the Apple side below is done and the values are in Core and the website. Until then Core answers
`503 apple_unavailable` and the website hides the button.

You do not need to write code. You need: someone with the **Account Holder** or **Admin** role in the company's Apple
Developer account, 30 minutes, and access to the two servers' config (Core `site_config`, website `app.env`).

## 0. Apple Developer Program (US$99 per year)

Sign in with Apple on the web needs a paid **Apple Developer Program** membership (US$99 per year, renews yearly;
if it lapses the button stops working at the next sign-in). Check it first:

1. Go to <https://developer.apple.com/account> and sign in with the company's Apple ID.
2. If it shows "Membership details" with an active program, continue. If it offers "Enroll", enroll as an
   **Organization** (needs a D-U-N-S number and a person with legal authority; it can take days, so start early) at
   <https://developer.apple.com/programs/enroll/>.

Use a company Apple ID (not a personal one) so the account survives staff changes.

## 1. Create an App ID with Sign in with Apple

Apple needs a "primary" App ID that the web sign-in hangs off.

1. <https://developer.apple.com/account/resources/identifiers/list> > **+** (blue plus).
2. Choose **App IDs** > Continue > type **App** > Continue.
3. Description: `TBO GrowthX`. Bundle ID: **Explicit**, `com.tbogrowthx.app` (reverse-DNS; any unused ID works, it never
   changes once created).
4. In **Capabilities** tick **Sign in with Apple** (leave it as "Enable as a primary App ID").
5. Continue > Register.

## 2. Create a Services ID (this is the "client id")

1. Same page > **+** > **Services IDs** > Continue.
2. Description: `TBO GrowthX Web`. Identifier: **`com.tbogrowthx.web`** (this exact text is the client id; write it
   down). Continue > Register.
3. Click the new Services ID in the list, tick **Sign in with Apple**, press **Configure**.
4. In the pop-up:
   - **Primary App ID**: the `TBO GrowthX` App ID from step 1.
   - **Domains and Subdomains**: `tbogrowthx.com`
   - **Return URLs**: **`https://tbogrowthx.com/api/auth/apple/callback`** (exact; https, no trailing slash,
     no `localhost`: Apple does not allow it).
5. Next > Done > Continue > **Save**.

Apple may ask you to verify the domain: only for old-style setups. If a domain verification file is requested, download
it and give it to the website owner to serve at
`https://tbogrowthx.com/.well-known/apple-developer-domain-association.txt`.

## 3. Create a Sign in with Apple key (the .p8 file)

1. <https://developer.apple.com/account/resources/authkeys/list> > **+**.
2. Key name: `TBO GrowthX Sign in`. Tick **Sign in with Apple** > **Configure** > choose the primary App ID from step 1 >
   Save > Continue > Register.
3. **Download** the `AuthKey_XXXXXXXXXX.p8` file. **Apple lets you download it exactly once.** Put it in the company
   password manager straight away. If it is lost you must revoke the key and make a new one (and update Core).
4. Write down the **Key ID** shown on that page (10 characters; it is also in the file name).

## 4. Note the Team ID

<https://developer.apple.com/account> > **Membership details** > **Team ID** (10 characters, letters and digits).

## 5. Where each value goes

Core (`growthx-core.tbogrowthx.com`), in `site_config.json` (or Press > Site > Config):

| Key | Value |
|---|---|
| `apple_oauth_client_id` | the Services ID, `com.tbogrowthx.web` |
| `apple_oauth_team_id` | the Team ID (step 4) |
| `apple_oauth_key_id` | the Key ID (step 3) |
| `apple_oauth_private_key` | the **whole contents** of the `.p8` file, including the `-----BEGIN PRIVATE KEY-----` and `-----END PRIVATE KEY-----` lines |
| `apple_oauth_redirect_uri` | optional; default `<growthx_web_url>/api/auth/apple/callback`. Only set it if it differs; it must equal the Return URL in step 2 |

The private key spans several lines. If your tool needs a single-line value, write the line breaks as the two characters
`\n`; Core turns them back into real line breaks. Example:
`"apple_oauth_private_key": "-----BEGIN PRIVATE KEY-----\nMIGTAgEA...\n-----END PRIVATE KEY-----"`.
Core uses this key to sign a short-lived (10 minutes) secret for every sign-in; the key never reaches the website.

Website (`/etc/growthx-web/app.env`), then `systemctl restart growthx-web`:

```
APPLE_CLIENT_ID=com.tbogrowthx.web
```

The button shows only when `APPLE_CLIENT_ID` is set. It is the same Services ID as `apple_oauth_client_id`.

## 6. Check it works

1. Open `https://tbogrowthx.com/login`: "Continue with Apple" appears with the other buttons. (Use a real Apple ID; Apple's
   screen asks whether to share or hide the email.)
2. A new Apple ID gets the sign-up form (company, site) and then a customer account. The same Apple ID signing in again
   goes straight in.
3. Account settings > Sign-in methods > Apple > Link / Unlink works.

Troubleshooting: `invalid_client` in Core's Error Log ("Apple sign-in: invalid_client") means the Services ID, Team ID, Key
ID or key text is wrong, or the Return URL isn't registered exactly. "The private key can't sign" means the key text was
damaged (usually lost line breaks).

## 7. "Hide My Email" and email to Apple's private relay

Apple lets people hide their real email. Then GrowthX receives an address like `abc123@privaterelay.appleid.com`. It is
a real, working address that forwards to the person, and GrowthX treats it as their email (invoices, password
reset, notices). Two things to know:

- Apple only forwards mail from senders **you register**. Register the sender: <https://developer.apple.com/account/resources/services/configure>
  > **Configure** under **Sign in with Apple for Email Communication** > **+** > add the domain **`tbogrowthx.com`**
  and/or the exact sender **`noreply@tbogrowthx.com`** (the address our mail is sent from through Resend). Apple shows
  SPF requirements: make sure the domain's DNS has the SPF/DKIM records Resend gave (they normally already exist because
  the domain sends mail through Resend). Wait for the status to show **Verified**.
- If someone later turns off email forwarding for the app (in their Apple ID settings), mail to the relay address
  bounces; they can still sign in with Apple.

A relay address never matches an existing GrowthX account unless an account was created with that exact address.
Someone who signs up with a hidden email therefore gets a **separate** account from one they made with their real email;
they can link Apple to the existing account from Account settings.
