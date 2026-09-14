# Risk management

Risk management is a separate component (`trading_agent/risk/`) that runs after the strategy
and can veto any proposed order. A strategy can only *propose*; if any check fails the order
is never created. Every check is stored with the decision so vetoes are explainable.

## Configurable limits

| Setting | Default | Check |
|---|---|---|
| `MAX_PORTFOLIO_EXPOSURE` | 0.70 | invested capital after the trade ≤ 70 % of portfolio value |
| `MAX_POSITION_SIZE` | 0.10 | single position (and single trade) ≤ 10 % of portfolio value |
| `MAX_RISK_PER_TRADE` | 0.01 | position sizing risk budget = 1 % of portfolio value |
| `MAX_DAILY_LOSS` | 0.02 | no new orders once the day's P&L is below −2 %; breaker trips |
| `MAX_DRAWDOWN` | 0.15 | no new orders once drawdown from peak exceeds 15 %; breaker trips |
| `MAX_OPEN_POSITIONS` | 10 | number of simultaneous positions |
| `CASH_RESERVE` | 0.10 | 10 % of portfolio value is never spent |
| `MAX_ORDER_VALUE` | 10 000 | absolute cap on a single order's notional |
| `MAX_ORDERS_PER_DAY` | 20 | order frequency, portfolio-wide |
| `MAX_ORDERS_PER_SYMBOL_PER_DAY` | 2 | order frequency per symbol |
| `MAX_SECTOR_CONCENTRATION` | 0.30 | sector weight after trade (when sector data exists) |
| `STOP_LOSS_PCT` | 0.08 | minimum stop distance used for sizing |
| `STRATEGY_ATR_STOP_MULTIPLIER` | 3.0 | ATR multiple for the volatility stop |
| `ABNORMAL_PRICE_MOVE_PCT` | 0.25 | quotes moving > 25 % vs the last close are rejected and trip the breaker |
| `MAX_DATA_AGE_SECONDS` | 900 | no trading on data older than 15 minutes |
| `ALLOW_MARGIN` / `ALLOW_SHORT_SELLING` / `ALLOW_LEVERAGED_ETFS` / `ALLOW_DERIVATIVES` | false | asset-class and leverage guards (`ALLOW_DERIVATIVES=true` is rejected outright) |

Configuration is validated at startup: fractions must be in [0, 1], the position limit cannot
exceed the exposure limit, exposure + cash reserve cannot exceed 100 %, etc. Invalid
configuration means the process does not start.

## Position sizing

```
risk_budget    = portfolio_value × MAX_RISK_PER_TRADE
stop_distance  = max(ATR_STOP_MULTIPLIER × ATR, STOP_LOSS_PCT × price)
raw_quantity   = floor(risk_budget / stop_distance)
quantity       = min(raw_quantity,
                     MAX_POSITION_SIZE × portfolio_value / price  (minus existing position),
                     MAX_ORDER_VALUE / price,
                     exposure headroom / price,
                     buying_power / price)
```

The sizer never returns more than the account can pay for from cash; buying power itself is
`min(broker buying power, cash − reserved cash)`.

## Capital buckets

`PortfolioState` tracks `available_cash`, `reserved_cash` (open buy orders + cash reserve),
`invested_capital`, `portfolio_value` and `buying_power`. The OMS re-reads the broker account
immediately before submission and refuses any order larger than the broker's reported buying
power.

## Kill switch

`TRADING_ENABLED=false` (the default) stops all new orders. Monitoring, reconciliation,
snapshots, the dashboard and alerts continue. Existing positions are left untouched.

## Circuit breaker

Trips automatically and stays tripped until a human runs
`trading-agent reset-breaker --reason "..."`:

* `CIRCUIT_BREAKER_MAX_API_ERRORS` unexpected API/connectivity errors in the window
* `CIRCUIT_BREAKER_MAX_REJECTED_ORDERS` rejected orders in the window
* `CIRCUIT_BREAKER_MAX_AUTH_FAILURES` authentication failures
* any abnormal price (move > `ABNORMAL_PRICE_MOVE_PCT`)
* daily loss or drawdown beyond the limits
* inconsistent account state found by reconciliation (orders at the broker we did not place,
  positions that differ from our fill history) when the broker is real

While tripped no order passes the risk manager or the OMS. The state is persisted so a
restart does not clear it.

## Pre-trade checklist (OMS)

1. market open (calendar and broker clock)  2. symbol tradable  3. fresh account state
4. buying power  5. current position  6. risk checks passed  7. no equivalent open order
(DB and broker)  8. decision logged  9. order persisted with idempotency key
10. broker submission + response verification  11. tracking until terminal state.

## Stop-loss handling

The baseline strategy emits a SELL when the close falls below the ATR trailing stop
anchored at the highest close since entry, or when the trend reverses. Stops are evaluated
every cycle rather than resting at the broker so they work identically in backtests, with
the simulated broker and with any real broker.
