"""CSV-backed historical provider.

Expects one file per symbol under ``data_dir``: ``<SYMBOL>.csv`` with columns
``date,open,high,low,close,volume`` (adjusted close is used as close if an
``adj_close`` column is present, keeping backtests consistent across splits).

Bars are timestamped at the session close in UTC. A quote is the last bar's close,
timestamped at that bar's close, so the staleness check correctly rejects it for
intraday paper trading unless the file is refreshed.
"""

from __future__ import annotations

import csv
from datetime import UTC, date, datetime, time
from decimal import Decimal, InvalidOperation
from pathlib import Path

from trading_agent.domain import Bar, Quote
from trading_agent.market_data.base import MalformedDataError, MarketDataProvider
from trading_agent.market_data.validation import validate_bars


def _parse_date(raw: str) -> datetime:
    raw = raw.strip()
    try:
        if "T" in raw or " " in raw:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
        d = date.fromisoformat(raw)
        return datetime.combine(d, time(21, 0), tzinfo=UTC)  # 16:00 New York ~ 21:00 UTC (approx.)
    except ValueError as exc:
        raise MalformedDataError(f"bad date {raw!r}") from exc


class CsvMarketDataProvider(MarketDataProvider):
    name = "csv"

    def __init__(self, data_dir: Path, session_close_utc: time = time(21, 0)) -> None:
        self.data_dir = Path(data_dir)
        self.session_close_utc = session_close_utc
        self._cache: dict[str, list[Bar]] = {}

    def _load(self, symbol: str) -> list[Bar]:
        if symbol in self._cache:
            return self._cache[symbol]
        path = self.data_dir / f"{symbol}.csv"
        if not path.exists():
            return []
        bars: list[Bar] = []
        with path.open(newline="") as fh:
            reader = csv.DictReader(fh)
            if reader.fieldnames is None:
                raise MalformedDataError(f"{path}: empty file")
            cols = {c.lower().strip(): c for c in reader.fieldnames}
            required = {"date", "open", "high", "low", "close", "volume"}
            missing = required - set(cols)
            if missing:
                raise MalformedDataError(f"{path}: missing columns {sorted(missing)}")
            close_col = cols.get("adj_close", cols["close"])
            for row in reader:
                try:
                    ts = _parse_date(row[cols["date"]])
                    if ts.time() == time(0, 0):
                        ts = ts.replace(hour=self.session_close_utc.hour, minute=self.session_close_utc.minute)
                    o, h, lo, c = (Decimal(row[cols[k]].strip()) for k in ("open", "high", "low", "close"))
                    adj = Decimal(row[close_col].strip())
                    if adj != c and c > 0:
                        factor = adj / c
                        o, h, lo, c = o * factor, h * factor, lo * factor, adj
                    vol = Decimal(row[cols["volume"]].strip() or "0")
                except (InvalidOperation, KeyError, ValueError) as exc:
                    raise MalformedDataError(f"{path}: bad row {row}: {exc}") from exc
                bars.append(Bar(symbol, ts, o, h, lo, c, vol))
        bars.sort(key=lambda b: b.timestamp)
        validate_bars(bars)
        self._cache[symbol] = bars
        self._touch()
        return bars

    def get_bars(self, symbol: str, start: date | None = None, end: date | None = None, limit: int | None = None) -> list[Bar]:
        bars = self._load(symbol)
        if start:
            bars = [b for b in bars if b.timestamp.date() >= start]
        if end:
            bars = [b for b in bars if b.timestamp.date() <= end]
        return bars[-limit:] if limit else bars

    def get_quote(self, symbol: str) -> Quote | None:
        bars = self._load(symbol)
        if not bars:
            return None
        last = bars[-1]
        return Quote(symbol=symbol, price=last.close, timestamp=last.timestamp)

    def symbols(self) -> list[str]:
        return sorted(p.stem for p in self.data_dir.glob("*.csv"))
