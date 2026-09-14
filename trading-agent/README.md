<h1 align="center">Trading Agent</h1>

<p align="center"><b>Automated trading that defaults to not trading.</b></p>

<p align="center">
  <img alt="Python" src="https://img.shields.io/badge/python-3.11%2B-3776AB?style=flat-square&logo=python&logoColor=white">
  <img alt="Tests" src="https://img.shields.io/badge/tests-116%20passing-3fb950?style=flat-square">
  <img alt="Risk checks" src="https://img.shields.io/badge/risk%20checks-23-f85149?style=flat-square">
  <img alt="Mode" src="https://img.shields.io/badge/default-PAPER%20TRADING-1f6feb?style=flat-square">
  <img alt="License" src="https://img.shields.io/badge/license-MIT-8b98a9?style=flat-square">
</p>

<p align="center">
  <b><a href="https://mohammadrezababaee-lab.github.io/Claude-Projects/demo/">▶ Live dashboard demo</a></b> ·
  <a href="https://mohammadrezababaee-lab.github.io/Claude-Projects/">Project site</a> ·
  <a href="docs/Trading-Agent-How-To-Guide.docx">How-To guide (38 pp)</a> ·
  <a href="docs/architecture.md">Architecture</a>
</p>

An automated algorithmic trading agent that researches markets, generates explainable
buy/sell/hold decisions, manages a portfolio under independent risk limits and executes
orders through a broker abstraction. **It runs in paper-trading mode by default and cannot
be switched to live trading without an explicit multi-flag confirmation.**

> This is an execution engine, not an oracle. It does not and cannot guarantee profits.
> Automated trading can lose money. Read `docs/live_trading.md` before you even think about
> enabling real orders.

## What is in the box

| Stage | Status |
|---|---|
| 1. Backtesting engine (CAGR, return, vol, Sharpe, Sortino, max DD, win rate, profit factor, trades, costs, slippage, exposure, concentration; look-ahead and survivorship controls) | ✅ |
| 2. Paper trading with a realistic simulated broker (cash, positions, market/limit/stop/stop-limit orders, fills, partial fills, commissions, slippage, market hours, rejections, order status, valuation) | ✅ default |
| 3. Live broker adapter (**Alpaca**, official REST API), disabled unless the full live gate passes | ✅ gated |
| Avanza | ❌ no official API exists; the adapter refuses to start (see `docs/broker_integration.md`) |
| Risk manager with veto, volatility-based sizing, kill switch, automatic circuit breaker | ✅ |
| Order-management system with idempotency, pre-trade checklist, reconciliation, crash recovery | ✅ |
| PostgreSQL/SQLite persistence for accounts, positions, instruments, market data, signals, decisions, strategy runs, orders, fills, snapshots, risk/system events, configuration changes | ✅ |
| Local dashboard (portfolio, positions, orders, agent health, performance) with a prominent `PAPER TRADING` banner | ✅ |
| Structured JSON logging with correlation IDs and secret redaction, Prometheus metrics, notifications (log, webhook) | ✅ |
| Automated tests: unit, integration and failure modes (116 tests) | ✅ |

## The dashboard

<p align="center">
  <a href="https://mohammadrezababaee-lab.github.io/Claude-Projects/demo/">
    <img src="../demo/preview.png" alt="Dashboard showing portfolio value, agent health, positions, equity curve and drawdown" width="880">
  </a>
</p>

<p align="center"><i>The banner reads <b>PAPER TRADING</b> whenever live execution is disabled.
<a href="https://mohammadrezababaee-lab.github.io/Claude-Projects/demo/">Try the interactive demo →</a></i></p>

## Quick start

```bash
git clone https://github.com/MohammadRezaBabaee-Lab/Claude-Projects.git
cd Claude-Projects/trading-agent
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env                 # never commit .env

# 1. Backtest on deterministic synthetic data (no network, no credentials)
trading-agent backtest --synthetic --symbols AAA,BBB,CCC --start 2022-01-03 --end 2024-12-31

# 2. Paper trade against the simulated broker (kill switch must be released explicitly)
MARKET_DATA_PROVIDER=synthetic TRADING_ENABLED=true trading-agent run --once
trading-agent dashboard              # http://127.0.0.1:8000 (shows PAPER TRADING)

# 3. Tests
pytest
```

For real data, set `MARKET_DATA_PROVIDER=alpaca` with paper API keys, or download CSVs with
`scripts/download_history.py`. See `docs/setup.md`.

## Safety model in one screen

```
TRADING_MODE=paper            # default; 'live' is refused unless everything below is set
TRADING_ENABLED=false         # kill switch; monitoring continues, no orders
LIVE_TRADING_CONFIRMED=false  # must be true for live
LIVE_TRADING_CONFIRMATION_PHRASE=   # must equal the exact phrase printed by `trading-agent confirm-live`
+ BROKER must be a real adapter (never simulated)
+ `trading-agent run --i-understand-live-trading` on the command line
```

Leaving a live confirmation in place while in paper mode is a configuration error, so no
single-variable change can ever flip the agent into live trading. Even in live mode every
risk limit, the kill switch and the circuit breaker still apply.

## Architecture

```
Market Data → Feature Engineering → Strategy → Signal → Risk Manager → Position Sizer
           → Portfolio Manager → Order Manager → Broker
```

Strategies are pluggable and never touch the broker. The baseline is a configurable
moving-average trend-following strategy with an ATR trailing stop and volatility-based
sizing. Every decision, including HOLD and vetoes, is stored with its signal, features,
portfolio state, risk checks, sizing and reason:

```
BUY AAA

Signal:
  uptrend established: 20-day SMA 98.12 above 50-day SMA 91.40

Risk:
  [PASS] kill_switch: trading enabled
  [PASS] max_portfolio_exposure: exposure after trade 9.50%
  ...

Position sizing:
  risk_budget = 1000.00
  stop_distance_used = 8.00
  quantity = 95

Decision: BUY 95 shares
Reason: uptrend established ...; all 21 risk checks passed
```

## Documentation

* `docs/architecture.md` – components, data flow, cycle, persistence, time handling
* `docs/broker_integration.md` – broker investigation, why Alpaca, permissions, limits, how to add Saxo/Nordnet
* `docs/risk_management.md` – every limit, sizing formula, kill switch, circuit breaker, checklist
* `docs/security.md` – secrets, least privilege, transport, validation, dashboard exposure
* `docs/live_trading.md` – prerequisites and the exact gate for enabling live trading
* `docs/setup.md` – installation, backtests, paper trading, migrations, Docker, operations
* `docs/backtesting.md` – engine timeline, bias controls, metrics

## Repository layout

```
trading_agent/      application package (broker, market_data, strategies, risk, portfolio,
                    execution, backtesting, scheduler, notifications, api, db)
tests/              unit / integration / failure tests
migrations/         Alembic migrations
scripts/            backtest and paper-trading examples, history download
docs/               documentation
.env.example        configuration template (no secrets)
docker-compose.yml  PostgreSQL + agent + dashboard (paper mode)
```

## License

MIT
