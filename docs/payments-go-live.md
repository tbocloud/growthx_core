# Turning on online payment (PayU) in production

Audit of 22 Nov 2026. **Online payment is OFF in production today**, so a customer's "Pay now" (including the
ERPNext Direct flow) answers `503 payment_unavailable` and the website shows the bank-transfer fallback. This is
the checklist to turn it on. Each step is marked **[DevOps]**, **[Business]** or **[Core dev]**.

Production now: Core `growthx-core.tbogrowthx.com` on Press release group `bench-0016` with only
`frappe`, `erpnext`, `growthx_core`; web `tbogrowthx.com` with `ONLINE_PAYMENTS` unset (= `off`).

## What the code needs (why it is off)

| Needs | Where checked | Today in prod |
|---|---|---|
| App `frappe_payu` (repo `tbocloud/payu_billing`) installed, which needs Frappe's `payments` app first | `billing/payu.py` `gateway_account()` | Missing: `payu.Unavailable` |
| site_config `growthx_payu_gateway_account` = the ERPNext **Payment Gateway Account** for PayU | `payu.gateway_account()` | Not set |
| A selling **Company**: site_config `growthx_company`, or ERPNext's default company | `payu.company()` | Not verified; the GST set-up needs it |
| App `india_compliance` (v16, CI pins v16.11.1): GST invoices, and `billing_details.available()` | `api/v1/billing.py` `create_checkout`, `billing/billing_details.py` | Missing: `503` before PayU is even asked, and the billing-address form can't save |
| GST set-up: seller GSTIN, tax templates, invoice series, SAC on each plan's Item | `setup/gst.py` | Not done (needs India Compliance first) |
| Web `ONLINE_PAYMENTS=on` | `growthx-web` `src/env.ts`, `billing/page.tsx`, `/api/gx` proxy | Off |

Each missing item alone keeps payment off, so a half-done set-up fails safe (customers see bank transfer).

Known trap once India Compliance is installed: `gst.item_tax_template()` reads `Item Tax Template.gst_rate`,
which only exists with India Compliance (on prod today it fails with `1054 Unknown column 'gst_rate'`). Run the
GST set-up (step 7) right after installing it, before any checkout.

Also note: `frappe_payu` from `tbocloud/payu_billing` carries a **Site Billing** module (reminders and
"suspend the site when an invoice is unpaid", read by Press). It only acts on customers who have a *Billed
Site* record, which Core never creates, but leave **Site Billing Settings** switched off on Core so nothing
there can ever suspend a customer's site.

## Checklist

### A. Business inputs (before anything else)

1. **[Business]** The legal entity that invoices: the code's seller is `TBO TEAM BACK OFFICE INTERNATIONAL LLP`,
   GSTIN `32AAVFT5109L1ZQ`, Calicut, Kerala (`growthx_core/setup/gst.py` `SELLER`). Confirm it is correct and
   the GSTIN is active; changing it is a code change.
2. **[Business]** The accountant confirms: SAC code (default `998315`, `site_config` `growthx_sac_code` to
   override), GST 18% included in plan prices, financial-year invoice series `GX/26-27/0001`.
3. **[Business]** A PayU India **live** merchant account with KYC done, **live Merchant Key and Salt**, the
   website domain `tbogrowthx.com` and the Core domain approved for live transactions, and the bank account for
   settlement. (Auto-debit/SI is a separate PayU enablement and is not needed here.)
4. **[Business]** Confirm the `erpnext-direct-lifetime` price (₹1,50,000 incl. GST today, the solar D6 price) or
   create the real direct plans with `admin.create_plan`.

### B. Install the apps

5. **[DevOps]** Add three apps to Press release group `bench-0016`, in this order, and deploy a new bench:
   `payments` (Frappe's, version-16 branch), `frappe_payu` (`https://github.com/tbocloud/payu_billing`, branch
   `main`; the app inside is `frappe_payu`), `india_compliance` (`version-16`, same version CI uses). Then install
   each on `growthx-core.tbogrowthx.com`: `payments`, `frappe_payu`, `india_compliance`
   (Press "Install app", or `bench --site growthx-core.tbogrowthx.com install-app <app>`). Run `migrate`.
   Test first on `core-test.tbogrowthx.com`, which is the CI-like site with a throwaway test Company.
6. **[DevOps]** Check `bench --site growthx-core.tbogrowthx.com list-apps` shows the three new apps and that
   Core's `GX Checkout` queries still work (open `/app/gx-checkout`).

### C. Company, GST, accounts (Frappe desk on the Core site)

7. **[Core dev + Business]** In ERPNext make sure a **Company** exists for the legal entity (INR, India, standard
   chart of accounts, fiscal year April to March). Set `growthx_company` in `site_config` to its name if it isn't
   the global default. Then run:
   `bench --site growthx-core.tbogrowthx.com execute growthx_core.setup.gst.run`
   (seller GSTIN and address, In-State/Out-State templates, the `GX/26-27/0001` series, GST Settings, the tax-invoice
   print format, and the SAC code + Item Tax Template on every plan's billing Item). Safe to re-run.
8. **[Core dev]** Check there is an `Item` for each plan (`GX Plan.item`), including
   `GX-ERPNEXT-DIRECT-LIFETIME`: `bench ... execute growthx_core.setup.seed.plan_items` (also run by step 7).
9. **[Business]** In ERPNext create the **bank account / "PayU" receiving account** under the Company and a
   **Payment Gateway Account** for the PayU gateway (below): company, currency INR, the receiving account.
   Note its exact name (it looks like `PayU-Main - INR - TBO`).

### D. PayU in the desk

10. **[DevOps/Core dev + Business credentials]** Create **PayU Settings**: Payment Gateway Name (e.g. `Main`, which
    makes the gateway `PayU-Main`), **Environment = Live**, the live **Merchant Key** and **Merchant Salt**
    (the Business supplies them; never in Git or chat), Accepted Currencies `INR`. Press **Check Credentials**.
11. **[Core dev]** In PayU Settings set **After-payment Link** = `https://tbogrowthx.com/customer/billing` and tick
    **Return Customers Straight to the After-payment Link**. The customer then returns to the billing page with
    `?payu_txnid=...`, which asks `billing.checkout_status`; nothing in the link is proof of payment.
12. **[Business/DevOps]** In PayU's dashboard add the **Webhook URL** shown on the PayU Settings record
    (`https://growthx-core.tbogrowthx.com/api/method/frappe_payu.gateway.webhook`) for successful and failed
    payments. The browser return is `.../api/method/frappe_payu.gateway.payment_response`; both domains must be
    approved by PayU. Leave **Email Payment Links for Submitted Invoices** off (customers pay from the dashboard).
13. **[DevOps]** Core needs an outgoing Email Account (it already sends sign-up and invite email).

### E. site_config on the Core site

14. **[DevOps]** Set (Press site config on `growthx-core.tbogrowthx.com`), then `bench restart` / Press site update:

    | Key | Value |
    |---|---|
    | `growthx_payu_gateway_account` | the Payment Gateway Account name from step 9 |
    | `growthx_company` | the Company (if not the global default) |
    | `growthx_web_url` | `https://tbogrowthx.com` (links in invoice and invite emails) |
    | `growthx_sac_code` | only if the accountant changes `998315` |
    | `growthx_web_key` | already set for the website's server calls; do not change |

### F. Website

15. **[DevOps]** Deploy the web build that contains the ERPNext Direct flow, then set `ONLINE_PAYMENTS=on` in
    `/etc/growthx-web/app.env` and `systemctl restart growthx-web` (run-time flag, no rebuild). Turn it on only
    after steps 5 to 14 are done: with the flag on but Core not ready, customers get "online payment isn't
    available right now" (`503 payment_unavailable`), not a broken page. The CSP `form-action` already allows
    `secure.payu.in` and `test.payu.in`. While the flag is `off`, `/customer/billing` shows the bank-transfer
    panel, whose account details are `null` in `src/lib/program.ts` `bankTransfer` until **[Business]** supplies
    them (customers are asked to email `support@tbogrowthx.com`).

### G. Verify with a real ₹1 payment

16. **[Core dev]** In the admin panel create a plan on product `erpnext-direct`: name `Live test`, interval
    `one_time`, price `100` paise (₹1), **GST included** (online GST invoices are refused for a plan priced without GST: checkout answers 503; `add_direct_site` warns with `plan_not_payable_online`). (Direct customers can only pay the plan assigned to them, so
    nobody else can buy it.)
17. **[Core dev]** `Add ERPNext site` for an internal test customer (a real mailbox of ours, any of our own
    domains as the site) on that plan. Open the invite, set a password, add the billing address, press **Pay now**,
    pay ₹1 on PayU live.
18. Verify: `billing.checkout_status` shows `paid`; the subscription is `active` on the plan; the **Sales Invoice**
    carries the GST breakup and number `GX/26-27/…`; the **Payment Entry** exists; the audit log has
    `customer.online_payment`; PayU's dashboard shows the settlement; the PDF downloads from `/customer/invoices`.
19. Clean up: refund the ₹1 from the Payment Request (**Refund via PayU**), terminate the test site from
    `/admin/tenants`, set the `Live test` plan inactive in the desk (`GX Plan`, untick *Is Active*).

### Rollback

- Fastest: web `ONLINE_PAYMENTS=off` + restart (customers see bank transfer again, checkout calls are refused by
  the proxy). Then remove `growthx_payu_gateway_account` from site_config (Core answers `503`).
- Unpaid open checkouts stay `pending` and are cancelled the next time that customer starts a checkout or an
  admin records a payment; an admin can also `admin.record_payment` for anyone who paid by bank transfer.
- A payment already received stays valid: the Payment Entry hook activates the plan whenever it is submitted.
- Do not uninstall `india_compliance`/`frappe_payu` once invoices exist (they own fields on those documents).

## Open questions for the business

- Which legal entity and GSTIN go on the invoice (the code has TBO Team Back Office International LLP)?
- Final prices for the direct plans, and whether GST is included.
- PayU: live key/salt, domain approval and the settlement account, plus the bank details to show while online
  payment is off.
