"""Point-in-time replay provider used by the backtester.

It wraps a full historical dataset and only ever exposes bars up to the current
simulation time, which is how look-ahead bias is prevented structurally rather
than by convention.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from trading_agent.domain import Bar, Quote
from trading_agent.market_data.base import MarketDataProvider


class ReplayMarketDataProvider(MarketDataProvider):
    name = "replay"

    def __init__(self, history: dict[str, list[Bar]]) -> None:
        self._history = {s: sorted(bars, key=lambda b: b.timestamp) for s, bars in history.items()}
        self._now: datetime | None = None
        self._open_prices: dict[str, Bar] = {}  # bar whose *open* is the executable price right now

    # -- simulation clock ------------------------------------------------
    def set_time(self, now: datetime, executing_bars: dict[str, Bar] | None = None, include_today: bool = True) -> None:
        """Advance the clock. ``include_today=False`` (the open phase) hides bars dated today
        so a strategy can never see the close of the bar it is being filled on."""
        self._now = now
        self._open_prices = executing_bars or {}
        self._include_today = include_today
        self._touch()

    def _visible(self, b: Bar) -> bool:
        d = b.timestamp.date()
        today = self.now.date()
        return d < today or (d == today and getattr(self, "_include_today", True))

    @property
    def now(self) -> datetime:
        if self._now is None:
            raise RuntimeError("replay time not set")
        return self._now

    # -- provider API ----------------------------------------------------
    def get_bars(self, symbol: str, start: date | None = None, end: date | None = None, limit: int | None = None) -> list[Bar]:
        bars = [b for b in self._history.get(symbol, []) if self._visible(b)]
        if start:
            bars = [b for b in bars if b.timestamp.date() >= start]
        if end:
            bars = [b for b in bars if b.timestamp.date() <= end]
        return bars[-limit:] if limit else bars

    def get_quote(self, symbol: str) -> Quote | None:
        # During the execution phase the tradable price is the *next* bar's open.
        if symbol in self._open_prices:
            b = self._open_prices[symbol]
            return Quote(symbol=symbol, price=b.open, timestamp=self.now)
        bars = self.get_bars(symbol, limit=1)
        if not bars:
            return None
        # Latest visible close, stamped "now" (the close phase evaluates at the session close).
        return Quote(symbol=symbol, price=bars[-1].close, timestamp=self.now)

    def symbols(self) -> list[str]:
        return list(self._history)

    def first_date(self) -> date:
        return min(b[0].timestamp.date() for b in self._history.values() if b)

    def last_date(self) -> date:
        return max(b[-1].timestamp.date() for b in self._history.values() if b)

    def bar_on(self, symbol: str, d: date) -> Bar | None:
        for b in self._history.get(symbol, []):
            if b.timestamp.date() == d:
                return b
        return None

    def last_price_before(self, symbol: str, now: datetime) -> Decimal | None:
        bars = [b for b in self._history.get(symbol, []) if b.timestamp.date() < now.date()]
        return bars[-1].close if bars else None
