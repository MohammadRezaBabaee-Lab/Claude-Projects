"""Exchange trading calendar and market-hours logic.

Uses the ``exchange_calendars`` package (real holiday calendars, early closes) so the
agent never assumes "weekday == trading day". All timestamps are timezone-aware.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

import pandas as pd

from trading_agent.domain import MarketStatus


@lru_cache(maxsize=8)
def _load(code: str, start: str, end: str):
    import exchange_calendars as xcals

    return xcals.get_calendar(code, start=start, end=end)


class MarketCalendar:
    """Thin wrapper around exchange_calendars with a small API used by the rest of the code."""

    def __init__(self, code: str = "XNYS", start: str | None = None, end: str | None = None) -> None:
        self.code = code
        today = datetime.now(UTC).date()
        self._start = start or (today - timedelta(days=365 * 12)).isoformat()
        self._end = end or (today + timedelta(days=365)).isoformat()
        self._cal = _load(code, self._start, self._end)
        self.tz = ZoneInfo(str(self._cal.tz))

    # -- sessions ---------------------------------------------------------
    def is_session(self, d: date) -> bool:
        return bool(self._cal.is_session(pd.Timestamp(d)))

    def sessions_in_range(self, start: date, end: date) -> list[date]:
        lo = max(pd.Timestamp(start), self._cal.first_session)
        hi = min(pd.Timestamp(end), self._cal.last_session)
        if lo > hi:
            return []
        idx = self._cal.sessions_in_range(lo, hi)
        return [ts.date() for ts in idx]

    def next_session(self, d: date) -> date:
        ts = pd.Timestamp(d)
        if self._cal.is_session(ts):
            return self._cal.next_session(ts).date()
        return self._cal.date_to_session(ts, direction="next").date()

    def previous_session(self, d: date) -> date:
        ts = pd.Timestamp(d)
        if self._cal.is_session(ts):
            return self._cal.previous_session(ts).date()
        return self._cal.date_to_session(ts, direction="previous").date()

    def session_open(self, d: date) -> datetime:
        return self._cal.session_open(pd.Timestamp(d)).to_pydatetime().astimezone(UTC)

    def session_close(self, d: date) -> datetime:
        return self._cal.session_close(pd.Timestamp(d)).to_pydatetime().astimezone(UTC)

    # -- market hours -----------------------------------------------------
    def is_open(self, at: datetime) -> bool:
        if at.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
        return bool(self._cal.is_open_on_minute(pd.Timestamp(at).tz_convert("UTC"), ignore_breaks=True))

    def next_open(self, at: datetime) -> datetime:
        return self._cal.next_open(pd.Timestamp(at).tz_convert("UTC")).to_pydatetime().astimezone(UTC)

    def next_close(self, at: datetime) -> datetime:
        return self._cal.next_close(pd.Timestamp(at).tz_convert("UTC")).to_pydatetime().astimezone(UTC)

    def status(self, at: datetime) -> MarketStatus:
        is_open = self.is_open(at)
        return MarketStatus(
            is_open=is_open,
            timestamp=at,
            next_open=None if is_open else self.next_open(at),
            next_close=self.next_close(at) if is_open else None,
            source=f"calendar:{self.code}",
        )

    def local_day(self, at: datetime) -> date:
        """Trading-day date in the exchange's own timezone."""
        return at.astimezone(self.tz).date()

    def close_time_local(self, d: date) -> time:
        return self.session_close(d).astimezone(self.tz).time()
