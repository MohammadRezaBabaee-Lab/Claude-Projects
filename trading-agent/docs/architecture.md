# Architecture

## Goals

The agent is an **automated execution engine, not an oracle**. It is optimised for capital
preservation, controlled risk, deterministic behaviour, observability, auditability, reliable
execution and graceful failure, not for maximising the number of trades. Nothing in this
repository claims or guarantees profitability.

## Language and framework choice

**Python 3.11** was chosen because:

* every candidate broker with an official API (Alpaca, Saxo OpenAPI, Nordnet nExt, Interactive
  Brokers) offers REST/JSON, which `httpx` covers with no vendor SDK;
* the quantitative ecosystem (pandas/numpy, `exchange_calendars` for real trading calendars)
  is Python-native;
* SQLAlchemy 2 + Alembic give one code path for SQLite (development) and PostgreSQL
  (production);
* FastAPI provides a small, typed, read-only dashboard API with no extra runtime.

The repository was empty, so there was no existing language, dependency set or broker
integration to preserve.

## Data flow

```
Market Data (CSV | Alpaca | synthetic | replay)
      │  validate_bars / validate_quote / ensure_fresh / abnormal-move check
      ▼
Feature Engineering (trading_agent/features.py: SMA, EMA, ATR, realised vol, returns)
      ▼
Strategy (trading_agent/strategies/*)  ──►  Signal(BUY | SELL | HOLD, reason, features)
      ▼
Risk Manager (trading_agent/risk/manager.py)   ──►  list[RiskCheck]  (any failure = VETO)
      ▼
Position Sizer (trading_agent/risk/sizing.py)  ──►  quantity + sizing detail
      ▼
Decision Engine (trading_agent/decision.py)     ──►  Decision (fully explainable record)
      ▼
Portfolio Manager (trading_agent/portfolio/manager.py)  capital buckets, P&L, drawdown
      ▼
Order Manager (trading_agent/execution/order_manager.py)  11-step checklist, idempotency
      ▼
Broker (trading_agent/broker/*)  Simulated | Alpaca | Avanza(unsupported)
```

The strategy layer imports nothing from the broker layer (a unit test enforces this). The
same `DecisionEngine` is used by the backtester and by the live/paper cycle, so behaviour is
identical across stages.

## Packages

| Package | Responsibility |
|---|---|
| `trading_agent/config.py` | Pydantic settings, startup validation, **live-trading gate** |
| `trading_agent/domain.py` | Enums and dataclasses (`Order`, `Fill`, `Position`, `Decision`, …); all money is `Decimal` |
| `trading_agent/logging_config.py` | JSON structured logging, correlation IDs, secret redaction |
| `trading_agent/db/` | SQLAlchemy models, `Repository` (all persistence goes through it) |
| `trading_agent/broker/` | `Broker` ABC, `SimulatedBroker`, `AlpacaBroker`, `AvanzaBroker` (refuses) |
| `trading_agent/market_data/` | Provider ABC, CSV / Alpaca / synthetic / replay providers, calendar, validation |
| `trading_agent/features.py` | Indicator computation |
| `trading_agent/strategies/` | `Strategy` ABC, registry, `TrendFollowingStrategy` |
| `trading_agent/risk/` | `RiskManager`, `PositionSizer`, `CircuitBreaker` |
| `trading_agent/portfolio/` | `PortfolioManager` (capital buckets), performance metrics |
| `trading_agent/decision.py` | Strategy → risk → sizing → `Decision` |
| `trading_agent/execution/` | `OrderManager` (OMS), `Reconciler` |
| `trading_agent/backtesting/` | Event-driven daily backtester and reports |
| `trading_agent/scheduler/` | `TradingCycle` (one cycle), `AgentRunner` (composition root and loop) |
| `trading_agent/notifications/` | Notifier ABC, log and webhook notifiers |
| `trading_agent/metrics.py` | In-process metrics with Prometheus exposition |
| `trading_agent/api/` | FastAPI read-only dashboard + static UI |
| `migrations/` | Alembic migrations |

## Stages

1. **Backtesting** (`trading-agent backtest`): `BacktestEngine` replays history through the
   `ReplayMarketDataProvider`, which only exposes bars up to the simulation clock. Signals are
   computed at the close of day *d* and executed at the open of *d+1* through the
   `SimulatedBroker` with slippage and commissions. Delisted instruments (history that ends
   early) are force-liquidated at the last available price and flagged, so a universe can
   include dead names to avoid survivorship bias.
2. **Paper trading** (`trading-agent run` with `TRADING_MODE=paper`, the default): the same
   cycle runs against live/delayed data with the `SimulatedBroker`, whose full state is
   persisted in `kv_state` so restarts are seamless. Alternatively `BROKER=alpaca` with
   *paper* keys uses Alpaca's broker-side paper account.
3. **Live infrastructure**: `AlpacaBroker` is the real adapter. It is only pointed at the live
   endpoint when every element of the live gate is satisfied (see `docs/live_trading.md`).

## One trading cycle (`TradingCycle.run_once`)

1. Check market status (exchange calendar **and** broker clock must agree).
2. Track open orders, read account/positions, build `PortfolioState`, check daily-loss and
   drawdown limits (may trip the circuit breaker).
3. Fetch and validate market data for the universe (OHLC sanity, monotonic timestamps,
   staleness against `MAX_DATA_AGE_SECONDS`, abnormal moves).
4. For each symbol: strategy → risk → sizing → `Decision`; every decision is persisted with
   its explanation, whether or not it results in an order.
5. Submit approved orders through the OMS.
6. Reconcile with the broker.
7. Persist a portfolio snapshot and positions; update metrics; publish agent status.
8. Notifications are emitted along the way (orders, rejections, breaker, drawdown, errors).

Monitoring steps always run; only order submission is gated by the kill switch, the circuit
breaker, market hours and the live gate.

## Order management and idempotency

`OrderManager.submit` performs the full pre-trade checklist and persists the order as
`PENDING_SUBMIT` under a unique **idempotency key** (`symbol|side|qty|strategy|trading-day`)
*before* the broker is called. The broker adapters are themselves idempotent on
`client_order_id`. Failure handling:

| Failure | Local status | Next step |
|---|---|---|
| Broker rejects | `REJECTED` | not retried the same day; breaker counts it |
| Connectivity failure before request left | `FAILED` | one retry with a new key |
| Connectivity failure after request left | look up by client id → adopt broker's order | none |
| Lookup also fails | `UNKNOWN` | blocks equivalent orders until reconciled |
| Process crash mid-submit | `PENDING_SUBMIT` | `startup()` reconciles: adopt or mark `FAILED` |

## Persistence

All tables required by the specification exist (`accounts`, `positions`, `instruments`,
`market_data`, `signals`, `strategy_runs`, `orders`, `fills`, `portfolio_snapshots`,
`risk_events`, `system_events`, `configuration_changes`) plus `decisions` (explainable
decision records) and `kv_state` (simulated-broker state, circuit-breaker state, portfolio
tracking, agent status, metrics snapshot). Money columns are `Numeric(20, 6)`.

## Time handling

All timestamps are timezone-aware UTC internally. Trading days and market hours come from
`exchange_calendars` (`XNYS` for US listings, `XSTO` for Nasdaq Stockholm), including
holidays and early closes. `LOCAL_TIMEZONE` (default `Europe/Stockholm`) is for display.
