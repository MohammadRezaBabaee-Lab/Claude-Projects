"""Backtesting example: synthetic history (or CSVs) through the full decision pipeline.

Run:  python scripts/run_backtest_example.py [--data-dir data/history --symbols SPY,QQQ]
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from trading_agent.backtesting import BacktestEngine
from trading_agent.backtesting.report import render_text, write_report
from trading_agent.config import load_settings
from trading_agent.logging_config import configure_logging
from trading_agent.market_data.calendar import MarketCalendar
from trading_agent.market_data.csv_provider import CsvMarketDataProvider
from trading_agent.market_data.synthetic import generate_bars
from trading_agent.strategies import build_strategy


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", help="directory of <SYMBOL>.csv files; omit for synthetic data")
    ap.add_argument("--symbols", default="AAA,BBB,CCC,DEAD")
    ap.add_argument("--start", default="2022-01-03")
    ap.add_argument("--end", default="2024-12-31")
    ap.add_argument("--report", default="reports/backtest_example.json")
    args = ap.parse_args()
    configure_logging("WARNING", "text")

    settings = load_settings(_env_file=None, universe=args.symbols, max_order_value="50000")
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    cal = MarketCalendar(settings.exchange_calendar, start=start.isoformat(), end=end.isoformat())
    symbols = settings.universe

    if args.data_dir:
        provider = CsvMarketDataProvider(Path(args.data_dir))
        history = {s: provider.get_bars(s, start, end) for s in symbols}
        history = {s: b for s, b in history.items() if b}
    else:
        sessions = cal.sessions_in_range(start, end)
        history = {
            s: generate_bars(s, sessions, start_price=60 + 20 * i, drift=0.0005, vol=0.015, seed=i) for i, s in enumerate(symbols)
        }
        if "DEAD" in history:  # simulate a delisting to exercise survivorship handling
            history["DEAD"] = history["DEAD"][:250]

    strategy = build_strategy(
        "trend_following",
        fast_window=settings.strategy_fast_window,
        slow_window=settings.strategy_slow_window,
        atr_window=settings.strategy_atr_window,
        atr_stop_multiplier=settings.strategy_atr_stop_multiplier,
    )
    result = BacktestEngine(settings, strategy, history, calendar=cal).run(start, end)
    print(render_text(result))
    write_report(result, Path(args.report))
    print(f"\nJSON report: {args.report}")
    sample = next((d for d in result.decisions if d.decision == "BUY"), None)
    if sample:
        print("\nExample explainable decision:\n")
        print(sample.explanation())
    print("\nSummary:", json.dumps(result.summary(), default=str)[:400], "...")


if __name__ == "__main__":
    main()
