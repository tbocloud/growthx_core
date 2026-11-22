# Partner commissions and payouts

Rules live in the `GX Partner Program Settings` Single (desk), so the business can change them without code.
Defaults follow the published program.

| Setting | Default | Note |
|---|---|---|
| Tiers | Seed 20 % from 0 active clients, Sprout 25 % from 10, Canopy 30 % from 30 | Rate is chosen by the partner's active referred customers when the payment is received. Empty table = these defaults. |
| Minimum payout | Rs 1,000 | Approved, unpaid commission before TDS; smaller balances carry over. |
| Payout day | 15 | Shown to partners as the next payout date. |
| TDS rate | 2 % (Sec. 194H) | Withheld once per payout. Needs business/finance confirmation. |
| Hold days | 0 | Days after the customer's payment before approval is allowed. |

Flow: a payment for a referred customer (`admin.record_payment`, PayU Payment Entry) creates one `GX Commission`
(pending, idempotent per payment) on the amount excluding GST. An admin approves it, `admin.create_payouts` makes
draft `GX Payout`s, `admin.mark_payout_paid` records the UTR and marks commissions paid and notifies the partner.
A cancelled Payment Entry voids an unpaid commission. All math is whole paise, half up.
