# Enabling live trading

**Do not enable live trading until paper trading has passed.** The default state of the
project is paper trading with the kill switch engaged, and there is no code path that flips
it at runtime.

## Prerequisites (all of them)

1. A backtest over at least two years of real data has been reviewed, including drawdown,
   costs and trade count (`trading-agent backtest --report reports/backtest.json`).
2. At least four weeks of continuous paper trading (`TRADING_MODE=paper`) with the same
   universe, strategy parameters and risk limits you intend to use live, with:
   * no unexplained circuit-breaker trips,
   * no reconciliation mismatches,
   * every order in the dashboard having a recorded decision and rationale,
   * notifications verified to arrive.
3. Risk limits reviewed against the size of the live account (`MAX_ORDER_VALUE`,
   `MAX_POSITION_SIZE`, `MAX_DAILY_LOSS`, `MAX_DRAWDOWN`).
4. Live broker credentials created with the minimum privilege the broker offers and stored
   only in the environment/credential store.
5. A funded live account that you are prepared to lose money in. Automated trading can and
   does lose money; nothing here guarantees profits.

## How the gate works

Live orders are submitted only when **every** condition below is true. Each is checked at
startup by the config validator and again inside the order manager at submission time.

| Condition | Where enforced |
|---|---|
| `TRADING_MODE=live` | `Settings` |
| `TRADING_ENABLED=true` | `Settings`, `RiskManager.kill_switch`, `OrderManager` |
| `LIVE_TRADING_CONFIRMED=true` | `Settings` (required when mode is live) |
| `LIVE_TRADING_CONFIRMATION_PHRASE` equals exactly `I UNDERSTAND THAT LIVE TRADING RISKS REAL MONEY` | `Settings` |
| `BROKER` is a real adapter (never `simulated`) | `Settings` |
| `--i-understand-live-trading` passed on the command line | `trading-agent run` |
| Broker adapter constructed with `live_orders_permitted=True` | `broker/factory.py` → `AlpacaBroker` selects the live base URL only then |

Additional protections:

* While `TRADING_MODE=paper`, setting `LIVE_TRADING_CONFIRMED=true` or the phrase is a
  **configuration error**: you cannot leave a latent confirmation in place and later flip a
  single variable.
* The Alpaca adapter refuses to submit a live order to any base URL other than the official
  live endpoint.
* Every risk limit, the kill switch and the circuit breaker apply unchanged in live mode.
* The dashboard shows a red `LIVE TRADING` banner instead of `PAPER TRADING`; the startup log
  contains a `live_mode` warning; `configuration_changes` records the mode switch.

## Step by step

1. Stop the paper agent. Back up the database.
2. Run `trading-agent confirm-live` to print the procedure (it changes nothing).
3. Edit `.env`:
   ```
   BROKER=alpaca
   TRADING_MODE=live
   TRADING_ENABLED=true
   LIVE_TRADING_CONFIRMED=true
   LIVE_TRADING_CONFIRMATION_PHRASE=I UNDERSTAND THAT LIVE TRADING RISKS REAL MONEY
   ALPACA_API_KEY_ID=<live key>
   ALPACA_API_SECRET_KEY=<live secret>
   MARKET_DATA_PROVIDER=alpaca
   ```
4. `trading-agent validate-config` must succeed and print `live_orders_permitted=True`.
5. Start with a deliberately small `MAX_ORDER_VALUE` and `MAX_POSITION_SIZE` for the first
   weeks: `trading-agent run --i-understand-live-trading`.
6. Watch the dashboard and notifications daily. Any circuit-breaker trip requires a human to
   investigate and `trading-agent reset-breaker --reason "..."`.

## Emergency stop

Set `TRADING_ENABLED=false` and restart, or simply stop the process. Open orders at the
broker are not cancelled automatically (positions remain untouched by design); cancel them in
the broker's own interface if needed.

## Going back to paper

Set `TRADING_MODE=paper` **and remove** `LIVE_TRADING_CONFIRMED` and the phrase, otherwise
the validator rejects the configuration.
