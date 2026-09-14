# Security

## Secrets

* Credentials come only from environment variables or a local `.env` file. `.env`, key
  files and the live-confirmation marker are git-ignored; `.env.example` contains no values.
* Nothing is hard-coded: no passwords, API keys, tokens or personal identification numbers
  exist in the source tree (the test suite uses obviously fake values against mock
  transports).
* `Settings.redacted()` masks every field whose name contains `key`, `secret`, `password`,
  `token` or `webhook_url`; only the redacted form is persisted in `configuration_changes`
  and shown in the dashboard.
* The logging pipeline runs a `SecretRedactingFilter` on every record and the JSON formatter
  redacts nested data, so API keys never appear in logs. Notifications pass through the same
  redactor.
* For production, prefer injecting secrets from an OS credential store or a secrets manager
  into the environment rather than a `.env` file on disk.

## Least privilege

* Alpaca keys are account-scoped. Use **paper keys** during development and paper trading;
  create live keys only when enabling live trading, and rotate them from the Alpaca
  dashboard if they are ever exposed.
* Where a broker offers read-only credentials (e.g. Saxo OpenAPI application scopes),
  request read-only until execution is needed.
* The dashboard is read-only: it cannot place orders, reset the breaker or change
  configuration.

## Transport

* All broker/data/webhook URLs are validated to be `https://`; the config validator rejects
  anything else. The adapters never disable TLS verification.
* Retries with exponential backoff are applied only to idempotent GET requests. `POST
  /orders` is never blindly retried; the order is looked up by `client_order_id` first.

## Input validation

* Configuration is validated by Pydantic at startup and the process fails closed.
* Market data is validated (positive finite prices, OHLC consistency, monotonic timestamps,
  timezone-aware stamps, staleness, abnormal moves) before any strategy sees it.
* Order requests are validated (positive whole share counts, prices present for limit/stop
  orders, symbol sanity) before persistence and again inside the broker adapter.
* Broker responses are checked: a mismatching `client_order_id` trips the breaker.

## Dashboard exposure

The dashboard binds to `127.0.0.1` by default and has **no authentication**. Keep it local or
put it behind an authenticating reverse proxy. The CLI warns when it is bound elsewhere. The
API returns only redacted configuration and never returns credentials.

## Dependencies

* Versions are pinned to compatible ranges in `pyproject.toml`; run `pip-audit --skip-editable`
  (included in the `dev` extra) regularly. The audit at the time of writing flagged only the
  build tool `setuptools` < 83 (PYSEC-2026-3447), not a runtime dependency; upgrade it in your
  environment with `pip install -U setuptools` (the Dockerfile does this).
* No dynamic code loading: strategies are registered in code, configuration cannot point at
  arbitrary modules, and there is no remote-code path anywhere.

## Operational safety

* The default configuration is paper mode with the kill switch off; live orders require the
  multi-flag gate described in `docs/live_trading.md`.
* All state needed to recover safely lives in the database; restarts reconcile with the
  broker before any new order.
* Session handling: the agent keeps no browser sessions or cookies; broker auth is
  header-based per request over TLS.
