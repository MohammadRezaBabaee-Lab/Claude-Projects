# Backtesting

`BacktestEngine` (`trading_agent/backtesting/engine.py`) is event-driven over daily bars.

## Timeline per session

1. **Open**: orders queued at the previous close execute at today's open, with slippage
   (`SIM_SLIPPAGE_BPS`) and commissions (`SIM_COMMISSION_PER_ORDER` + `SIM_COMMISSION_PCT`).
2. **Close**: positions are marked; equity, exposure and concentration are recorded.
3. **Signal**: the strategy sees bars up to and including today and queues orders for
   tomorrow's open.

## Bias controls

* **Look-ahead**: the replay provider hides today's bar during the open phase and never
  exposes future bars; a test tampers with post-cutoff prices and asserts that every
  decision and equity point before the cutoff is unchanged.
* **Survivorship**: the history dictionary may include instruments whose data ends early.
  They stop generating decisions, are force-liquidated at their last close and are listed
  under `forced_liquidations` in the report. Build universes from historical constituents,
  not today's index members.
* **Adjusted prices**: the CSV provider uses `adj_close` when present and scales OHLC by the
  same factor; the Alpaca data provider requests `adjustment=all`.

## Metrics

CAGR, total return, annualised volatility, Sharpe, Sortino, maximum drawdown, number of
round trips and fills, win rate, profit factor, average trade P&L, transaction costs,
slippage cost, average/max exposure, average/peak largest position weight, monthly returns
and the drawdown curve. Round trips are matched FIFO per symbol from fills.

## Example

```bash
trading-agent backtest --synthetic --symbols AAA,BBB,CCC --start 2022-01-03 --end 2024-12-31
python scripts/run_backtest_example.py
```

Synthetic prices are a random walk: results on them prove the machinery works, not that the
strategy has edge. Always evaluate on real data, out of sample, with realistic costs.
