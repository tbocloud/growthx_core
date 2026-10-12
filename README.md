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

- `growthx_core/api/v1/` contains the GrowthX API (whitelisted methods). The contract is in [`openapi.yaml`](openapi.yaml).
- Modules: `GrowthX Core`, `Partners`, `Billing`, `Provisioning`.

Web and mobile clients call only this API. Business rules (pricing, commissions, status) live here, not in the clients.

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
