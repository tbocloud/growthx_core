"""GX Billing Settings: store the default of every setting that has none stored yet.

Frappe uses a Single's defaults only while none of its fields is stored. The Press-side job stores the
platform site list every 5 minutes; after its first report every other setting read 0 or blank (dry run
off, no grace days, archive at once), and the first save in Desk would have kept those."""

from growthx_core.billing.doctype.gx_billing_settings.gx_billing_settings import fill_defaults


def execute():
	fill_defaults()
