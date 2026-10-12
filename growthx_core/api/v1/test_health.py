from frappe.tests.utils import FrappeTestCase

import growthx_core
from growthx_core.api.v1.health import ping


class TestHealth(FrappeTestCase):
	def test_ping(self):
		self.assertEqual(ping(), {"status": "ok", "version": growthx_core.__version__})
