from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date, datetime

from trading_agent.domain import Bar, Quote, utcnow


class MarketDataError(Exception):
    pass


class StaleDataError(MarketDataError):
    pass


class MalformedDataError(MarketDataError):
    pass


class MarketDataProvider(ABC):
    name: str = "abstract"

    @abstractmethod
    def get_quote(self, symbol: str) -> Quote | None:
        """Latest price. None if unavailable."""

    @abstractmethod
    def get_bars(self, symbol: str, start: date | None = None, end: date | None = None, limit: int | None = None) -> list[Bar]:
        """Daily OHLCV bars, oldest first. Must never include bars after ``end``."""

    def get_corporate_actions(self, symbol: str) -> list[dict]:
        """Corporate actions where the provider supports them (default: none)."""
        return []

    def healthcheck(self) -> bool:
        return True

    @property
    def last_update(self) -> datetime | None:
        return getattr(self, "_last_update", None)

    def _touch(self) -> None:
        self._last_update = utcnow()


def ensure_fresh(quote: Quote, max_age_seconds: int, now: datetime | None = None) -> Quote:
    age = quote.age_seconds(now)
    if age > max_age_seconds:
        raise StaleDataError(f"{quote.symbol}: quote is {age:.0f}s old (max {max_age_seconds}s)")
    if age < -60:
        raise MalformedDataError(f"{quote.symbol}: quote timestamp is in the future ({-age:.0f}s)")
    return quote
