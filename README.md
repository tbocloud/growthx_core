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

#### Real tenant sites on your machine (`mode: local`)

By default provisioning uses a fake Press client: tenants reach `ready` instantly but no site exists. To get real tenant sites before the Press API is available, switch to the local client. Each tenant becomes an ERPNext site on this bench, named `<subdomain>.localhost`.

```bash
bench --site growthX set-config --parse growthx_press '{"mode": "local", "db_root_username": "root", "db_root_password": "", "admin_password": "admin", "user_password": "admin", "port": 8003, "allow_missing_apps": 1}'
bench start   # the worker and scheduler run the job

# Tenant web server: picks the site from the Host header.
cd sites && ../env/bin/python -c "from frappe.app import serve; serve(port=8003, sites_path='.', no_reload=True)"
```

- Why a separate server: `bench serve` (and so `bench start`) pins itself to `default_site` when it is set in `common_site_config.json`, and then serves that site for every hostname. The command above passes no site, so `noor-solar.localhost:8003` reaches `noor-solar.localhost`.
- Sign-up queues the job as usual. `bench new-site` runs in the background (a few minutes); progress is in `logs/growthx-local-press/<site>.log`.
- The configure step runs ERPNext's setup wizard with the customer's company and creates a user for their email, with `user_password`, as System Manager.
- Open the tenant at `http://<subdomain>.localhost:8003` (browsers resolve `*.localhost` to this machine).
- `allow_missing_apps: 1` reports `growthx_theme` and the vertical app as installed when this bench doesn't have them, so the job can finish. Leave it out to make the job fail when they are missing.
- "Open my app" still needs the tenant-side token exchange in `growthx_theme` (`site_login.py`). Until it exists, send the link to the tenant's login page instead: `bench --site growthX set-config growthx_tenant_login_path "/login#login"` (the token lands after `#`, so it never reaches the tenant's server or logs). The customer then logs in with their email and `user_password`.
- Development only. Never set `mode: local` on a server.

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
