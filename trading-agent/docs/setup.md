# Setup

## Requirements

* Python 3.11+
* SQLite (bundled) for development, PostgreSQL 14+ for production (optional)
* Docker (optional)

## Install

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"          # add ",postgres" for PostgreSQL support
cp .env.example .env             # edit; never commit .env
trading-agent validate-config    # must succeed; it prints the redacted configuration
trading-agent init-db            # SQLite dev convenience (Alembic for production)
```

## Backtest

```bash
# deterministic synthetic data (no network)
trading-agent backtest --synthetic --symbols AAA,BBB,CCC --start 2022-01-03 --end 2024-12-31 --report reports/synthetic.json

# your own CSV history: data/history/<SYMBOL>.csv with date,open,high,low,close,volume[,adj_close]
trading-agent backtest --symbols SPY,QQQ --start 2018-01-01 --data-dir data/history

# download Alpaca daily bars into CSV (paper keys are enough for market data)
python scripts/download_history.py --symbols SPY,QQQ,AAPL --start 2018-01-01
```

See `scripts/run_backtest_example.py` for the programmatic API.

## Paper trading

```bash
# fully local: simulated broker, synthetic prices (demo)
MARKET_DATA_PROVIDER=synthetic TRADING_ENABLED=true trading-agent run --once

# realistic: simulated broker fed by Alpaca market data (paper keys)
BROKER=simulated MARKET_DATA_PROVIDER=alpaca TRADING_ENABLED=true trading-agent run

# broker-side paper account at Alpaca (paper keys, paper endpoint)
BROKER=alpaca MARKET_DATA_PROVIDER=alpaca TRADING_ENABLED=true trading-agent run
```

The agent sleeps between cycles (`CYCLE_INTERVAL_SECONDS`) and much longer while the exchange
calendar says the market is closed; monitoring still happens each cycle.

## Dashboard

```bash
trading-agent dashboard            # http://127.0.0.1:8000
```

Read-only; shows the `PAPER TRADING` banner whenever live execution is disabled.

## Database migrations (PostgreSQL)

```bash
export DATABASE_URL=postgresql+psycopg://trading:***@localhost:5432/trading
alembic upgrade head
```

## Docker

```bash
cp .env.example .env && edit .env
docker compose up --build          # postgres + agent (paper) + dashboard on :8000
```

## Tests and checks

```bash
pytest                              # unit, integration and failure tests
ruff check trading_agent tests
pip-audit                           # dependency vulnerabilities
```

## Operations

* `trading-agent status` prints the last cycle, breaker state and last run.
* `trading-agent reset-breaker --reason "investigated X"` after a circuit-breaker trip.
* Logs are JSON lines on stdout (`LOG_FORMAT=text` for humans); every record carries a
  correlation id shared by all events of a cycle.
* `/metrics` on the dashboard exposes Prometheus metrics (cycle/strategy/API latency,
  rejection rate, data age, exposure, drawdown, P&L, error rate).
