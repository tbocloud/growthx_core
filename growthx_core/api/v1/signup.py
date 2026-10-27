"""What the sign-up form offers (docs 09 §3)."""

from growthx_core.api.v1._core import endpoint
from growthx_core.provisioning import countries
from growthx_core.provisioning.press import tenant_domain


@endpoint(methods=("GET",), auth=False)
def options():
	return {
		"tenantDomain": tenant_domain(),
		"countries": [
			{
				"code": code,
				"name": c.country,
				"currency": c.currency,
				"timezone": c.timezone,
				"fiscalYearStart": c.fiscal_year_start,
				"charts": countries.charts(code),
				"defaultChart": c.default_chart,
			}
			for code, c in countries.COUNTRIES.items()
		],
		"defaultCountry": countries.DEFAULT,
	}
