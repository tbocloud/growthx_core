import growthx_core
from growthx_core.api.v1._core import endpoint


@endpoint(methods=("GET",), auth=False)
def ping():
	"""Liveness check for web, mobile and monitoring. Returns no tenant or user data."""
	return {"status": "ok", "version": growthx_core.__version__}
