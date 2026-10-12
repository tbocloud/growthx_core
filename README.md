### GrowthX Core

GrowthX API: billing, partners, subscriptions and tenant provisioning

### Installation

You can install this app using the [bench](https://github.com/frappe/bench) CLI:

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app $URL_OF_THIS_REPO --branch main
bench --site <site> install-app growthx_core  # requires erpnext
```

### Structure

- `growthx_core/api/v1/` contains the GrowthX API (whitelisted methods). The contract is in [`openapi.yaml`](openapi.yaml); decisions and open items are in [`docs/api-decisions.md`](docs/api-decisions.md).
- Modules: `GrowthX Core`, `Partners`, `Billing`, `Provisioning`.
- Integrating a client? Start with [`docs/web-integration-guide.md`](docs/web-integration-guide.md).

Web and mobile clients call only this API. Business rules (pricing, commissions, status) live here, not in the clients.

- `api/v1/_core.py`: the `@endpoint` decorator (auth, roles, errors, rate limits, idempotency) and output helpers.
- `tokens.py`: bearer tokens (hashed in `GX Auth Token`, checked through `auth_hooks`).
- `accounts.py`: sign-up (User, ERPNext Customer, GX Account, trial GX Subscription, GX Tenant).
- `provisioning/runner.py` + `press.py`: the provisioning job and the Press client (a fake one until DevOps delivers the Press API).
- `billing/gateway.py`: Razorpay checkout and signature checks.

### Running it locally

```bash
bench --site growthX migrate
bench --site growthX execute growthx_core.setup.seed.run   # products, plans, demo users; prints the demo password
bench --site growthX serve --port 8002                       # serves only this site
bench worker                                                 # runs provisioning jobs
curl http://localhost:8002/api/method/growthx_core.api.v1.health.ping
```

Base URL for clients: `http://<host>:8002/api/method/growthx_core.api.v1`, then `/<module>.<function>`, e.g. `/catalogue.products`.

### Tests

```bash
bench --site growthX run-tests --app growthx_core
```

### Contributing

This app uses `pre-commit` for code formatting and linting. Please [install pre-commit](https://pre-commit.com/#installation) and enable it for this repository:

```bash
cd apps/growthx_core
pre-commit install
```

Pre-commit is configured to use the following tools for checking and formatting your code:

- ruff
- eslint
- prettier
- pyupgrade

### License

mit
