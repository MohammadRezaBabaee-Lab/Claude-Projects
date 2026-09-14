"""Download daily bars from the Alpaca Market Data API into CSV files for backtesting.

Requires ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY in the environment (paper keys work).
Run:  python scripts/download_history.py --symbols SPY,QQQ --start 2018-01-01
"""

from __future__ import annotations

import argparse
import csv
from datetime import date
from pathlib import Path

from trading_agent.config import load_settings
from trading_agent.market_data.alpaca_data import AlpacaMarketDataProvider


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", required=True)
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", default=date.today().isoformat())
    ap.add_argument("--out", default=None, help="output directory (default MARKET_DATA_DIR)")
    args = ap.parse_args()
    s = load_settings()
    if not (s.alpaca_api_key_id and s.alpaca_api_secret_key):
        raise SystemExit("ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY are required")
    provider = AlpacaMarketDataProvider(
        s.alpaca_api_key_id, s.alpaca_api_secret_key, base_url=s.alpaca_data_base_url, feed=s.alpaca_data_feed
    )
    out = Path(args.out or s.market_data_dir)
    out.mkdir(parents=True, exist_ok=True)
    for sym in [x.strip().upper() for x in args.symbols.split(",") if x.strip()]:
        bars = provider.get_bars(sym, date.fromisoformat(args.start), date.fromisoformat(args.end))
        path = out / f"{sym}.csv"
        with path.open("w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["date", "open", "high", "low", "close", "volume"])
            for b in bars:
                w.writerow([b.timestamp.date().isoformat(), b.open, b.high, b.low, b.close, b.volume])
        print(f"{sym}: {len(bars)} bars -> {path}")


if __name__ == "__main__":
    main()
