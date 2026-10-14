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

Base URL for clients: `http://<host>:8002/api/method/growthx_core.api.v1`, then `.<module>.<function>` (a dot, not a slash), e.g. `.catalogue.products`.

#### Real tenant sites on your machine (`mode: local`)

By default provisioning uses a fake Press client: tenants reach `ready` instantly but no site exists. To get real tenant sites before the Press API is available, switch to the local client. Each tenant becomes an ERPNext site on this bench, named `<subdomain>.localhost`.

```bash
# This bench needs growthx_theme: it is installed on every tenant (single sign-on).
bench get-app https://github.com/tbocloud/growthx_theme.git

bench --site growthX set-config --parse growthx_press '{"mode": "local", "db_root_username": "root", "db_root_password": "<your MariaDB root password>", "admin_password": "admin", "port": 8003, "allow_missing_apps": 1}'
bench --site growthX set-config growthx_core_url "http://127.0.0.1:8002"   # where tenants reach Core
bench start   # the worker and scheduler run the job

# Tenant web server: picks the site from the Host header.
cd sites && ../env/bin/python -c "from frappe.app import serve; serve(port=8003, sites_path='.', no_reload=True)"
```

- Why a separate server: `bench serve` (and so `bench start`) pins itself to `default_site` when it is set in `common_site_config.json`, and then serves that site for every hostname. The command above passes no site, so `noor-solar.localhost:8003` reaches `noor-solar.localhost`.
- Sign-up queues the job as usual. `bench new-site` runs in the background (a few minutes); progress is in `logs/growthx-local-press/<site>.log`.
- The configure step runs ERPNext's setup wizard with the customer's company, creates a System Manager user for the customer's email with a **random password nobody knows**, enables "Login with Email Link", and writes `growthx_core_url` and the tenant's own key (`growthx_tenant_key`) to the tenant's site_config.
- Open the tenant from the website with **"Open my app"**: `growthx_theme.sso.login` redeems the one-time link with Core (`tenant.redeem_login`) and logs the customer in. There is no shared tenant password. `admin_password` is only for the tenant's Administrator, on your machine.
- `allow_missing_apps: 1` reports the vertical app (e.g. `solar_operations`) as installed when this bench doesn't have it, so the job can finish. `growthx_theme` is never faked: the job fails if it's missing.
- **Tenants created before single sign-on** (they used a shared `user_password` and `growthx_tenant_login_path: "/login#login"`): run
  `bench --site growthX execute growthx_core.provisioning.backfill.enable_sso`. It installs `growthx_theme`, writes a new key, replaces the owner's password with a random one, enables "Login with Email Link", and removes `growthx_tenant_login_path`. Safe to run again. Tenants made by the fake client have no real site and are reported as failed.
- Development only. Never set `mode: local` on a server.

### Tests

```bash
bench --site growthX run-tests --app growthx_core
```

CI (`.github/workflows/ci.yml`) runs on every pull request and on every push to `main`:

- **Lint:** `ruff check` and `ruff format --check` (ruff 0.6.9).
- **Install on a fresh bench + tests:** creates a new bench with Frappe and ERPNext **v16.35.0**, installs
  `growthx_core` on a brand-new site (MariaDB 11.8, Python 3.14, Node 24) and runs every test.

To check locally that the app installs cleanly before pushing:

```bash
bench new-site gx-ci-test.localhost --db-root-username root --admin-password admin \
  --install-app erpnext --install-app growthx_core
bench --site gx-ci-test.localhost set-config allow_tests true
bench --site gx-ci-test.localhost run-tests --app growthx_core
bench drop-site gx-ci-test.localhost --force --no-backup
```

A fresh site has no Territory or Customer Group until ERPNext's setup wizard runs; `after_install`
(`growthx_core/setup/defaults.py`) creates the ones sign-up and partner approval need.

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
