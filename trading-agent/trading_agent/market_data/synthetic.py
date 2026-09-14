"""Deterministic synthetic price generator for examples and tests. Not for real trading."""

from __future__ import annotations

import math
import random
from datetime import UTC, date, datetime, time
from decimal import Decimal

from trading_agent.domain import Bar, Quote, price, utcnow
from trading_agent.market_data.base import MarketDataProvider
from trading_agent.market_data.calendar import MarketCalendar


def generate_bars(
    symbol: str,
    sessions: list[date],
    start_price: float = 100.0,
    drift: float = 0.0004,
    vol: float = 0.015,
    seed: int = 7,
    close_time_utc: time = time(21, 0),
) -> list[Bar]:
    rng = random.Random(f"{symbol}:{seed}")  # noqa: S311 - synthetic data
    px = start_price
    bars = []
    for d in sessions:
        ret = rng.gauss(drift, vol)
        o = px * (1 + rng.gauss(0, vol / 4))
        c = px * math.exp(ret)
        hi = max(o, c) * (1 + abs(rng.gauss(0, vol / 3)))
        lo = min(o, c) * (1 - abs(rng.gauss(0, vol / 3)))
        vol_shares = int(abs(rng.gauss(1_000_000, 200_000)))
        ts = datetime.combine(d, close_time_utc, tzinfo=UTC)
        bars.append(Bar(symbol, ts, price(o), price(hi), price(lo), price(c), Decimal(vol_shares)))
        px = c
    return bars


class SyntheticMarketDataProvider(MarketDataProvider):
    name = "synthetic"

    def __init__(self, symbols: list[str], start: date, end: date, calendar: MarketCalendar | None = None, seed: int = 7) -> None:
        cal = calendar or MarketCalendar("XNYS", start=start.isoformat(), end=end.isoformat())
        sessions = cal.sessions_in_range(start, end)
        self._bars = {s: generate_bars(s, sessions, start_price=50 + 20 * i, seed=seed + i) for i, s in enumerate(symbols)}
        self._touch()

    def get_bars(self, symbol: str, start: date | None = None, end: date | None = None, limit: int | None = None) -> list[Bar]:
        bars = self._bars.get(symbol, [])
        if start:
            bars = [b for b in bars if b.timestamp.date() >= start]
        if end:
            bars = [b for b in bars if b.timestamp.date() <= end]
        return bars[-limit:] if limit else bars

    def get_quote(self, symbol: str) -> Quote | None:
        bars = self._bars.get(symbol)
        if not bars:
            return None
        # Quote is "now" so paper-trading examples pass the staleness check.
        return Quote(symbol=symbol, price=bars[-1].close, timestamp=utcnow())
