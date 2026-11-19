"""Countries a customer can choose at sign-up, and the company defaults Core sets from them.

Only countries with an ERPNext chart of accounts and tax set-up (docs 09 §1), and only those we
sell in: India for now (UAE was removed in 2026-11; no tenant had it). Currency, timezone
and fiscal year follow the country, so the website never sends them. Adding a country here is
all the website needs: it reads the list from `signup.options`.
"""

import frappe

DEFAULT = "IN"

COUNTRIES = {
	"IN": frappe._dict(
		country="India",
		currency="INR",
		timezone="Asia/Kolkata",
		fiscal_year_start="04-01",
		default_chart="India - Chart of Accounts",
	),
}


def charts(code: str) -> list[str]:
	"""ERPNext's own list for the country, so it always matches what the set-up wizard accepts."""
	from erpnext.accounts.doctype.account.chart_of_accounts.chart_of_accounts import (
		get_charts_for_country,
	)

	return get_charts_for_country(COUNTRIES[code].country, with_standard=True)


def for_tenant(tenant) -> frappe._dict:
	"""The tenant's country defaults. Tenants from before sign-up v2 have none: India."""
	for c in COUNTRIES.values():
		if c.country == tenant.get("country"):
			return c
	return COUNTRIES[DEFAULT]
