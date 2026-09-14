import json
import logging
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from trading_agent.domain import Bar, OrderRequest, OrderType, Quote, Side
from trading_agent.logging_config import JsonFormatter, SecretRedactingFilter, redact
from trading_agent.market_data.base import MalformedDataError, StaleDataError, ensure_fresh
from trading_agent.market_data.calendar import MarketCalendar
from trading_agent.market_data.validation import abnormal_move, validate_bars, validate_quote
from trading_agent.risk.circuit_breaker import CircuitBreaker

NOW = datetime(2026, 3, 10, 15, 0, tzinfo=UTC)


def test_order_request_validation():
    OrderRequest("AAA", Side.BUY, Decimal("10")).validate()
    with pytest.raises(ValueError):
        OrderRequest("AAA", Side.BUY, Decimal("0")).validate()
    with pytest.raises(ValueError):
        OrderRequest("AAA", Side.BUY, Decimal("1.5")).validate()
    with pytest.raises(ValueError):
        OrderRequest("AAA", Side.BUY, Decimal("1"), OrderType.LIMIT).validate()
    with pytest.raises(ValueError):
        OrderRequest("AAA", Side.BUY, Decimal("1"), OrderType.STOP, stop_price=Decimal("-1")).validate()
    with pytest.raises(ValueError):
        OrderRequest("", Side.BUY, Decimal("1")).validate()


def test_bar_validation_and_malformed_data():
    good = Bar("A", NOW, Decimal("10"), Decimal("11"), Decimal("9"), Decimal("10.5"), Decimal("1"))
    good.validate()
    bad = Bar("A", NOW, Decimal("10"), Decimal("9"), Decimal("9"), Decimal("10.5"), Decimal("1"))  # high < open
    with pytest.raises(ValueError):
        bad.validate()
    with pytest.raises(MalformedDataError):
        validate_bars([good, bad])
    with pytest.raises(MalformedDataError):
        validate_bars([good, good])  # not strictly increasing
    with pytest.raises(MalformedDataError):
        validate_quote(Quote("A", Decimal("-1"), NOW))
    with pytest.raises(MalformedDataError):
        validate_quote(Quote("A", Decimal("1"), NOW.replace(tzinfo=None)))


def test_staleness_and_abnormal_move():
    q = Quote("A", Decimal("100"), NOW - timedelta(seconds=1000))
    with pytest.raises(StaleDataError):
        ensure_fresh(q, 900, NOW)
    assert ensure_fresh(Quote("A", Decimal("100"), NOW - timedelta(seconds=10)), 900, NOW)
    with pytest.raises(MalformedDataError):
        ensure_fresh(Quote("A", Decimal("100"), NOW + timedelta(hours=1)), 900, NOW)
    assert abnormal_move(Quote("A", Decimal("150"), NOW), Decimal("100"), Decimal("0.25"))
    assert not abnormal_move(Quote("A", Decimal("101"), NOW), Decimal("100"), Decimal("0.25"))


def test_nyse_calendar_holidays_and_hours():
    cal = MarketCalendar("XNYS", start="2025-01-01", end="2026-12-31")
    assert not cal.is_session(date(2025, 12, 25))  # Christmas
    assert not cal.is_session(date(2025, 7, 4))  # Independence Day
    assert not cal.is_session(date(2026, 3, 14))  # Saturday
    assert cal.is_session(date(2026, 3, 10))
    assert cal.is_open(datetime(2026, 3, 10, 15, 0, tzinfo=UTC))  # 11:00 New York (EDT)
    assert not cal.is_open(datetime(2026, 3, 10, 13, 0, tzinfo=UTC))  # 09:00 New York, pre-open
    assert not cal.is_open(datetime(2026, 3, 10, 21, 0, tzinfo=UTC))
    st = cal.status(datetime(2026, 3, 14, 12, 0, tzinfo=UTC))
    assert not st.is_open and st.next_open.date() == date(2026, 3, 16)
    # early close (day after Thanksgiving 2025)
    assert cal.session_close(date(2025, 11, 28)).astimezone(cal.tz).hour == 13


def test_stockholm_calendar_and_timezone():
    cal = MarketCalendar("XSTO", start="2025-01-01", end="2026-12-31")
    assert not cal.is_session(date(2025, 6, 20))  # Midsommarafton
    assert not cal.is_session(date(2026, 1, 6))  # Epiphany
    assert cal.is_session(date(2026, 3, 10))
    assert cal.is_open(datetime(2026, 3, 10, 10, 0, tzinfo=UTC))  # 11:00 Stockholm
    assert not cal.is_open(datetime(2026, 3, 10, 17, 0, tzinfo=UTC))  # 18:00 Stockholm, closed at 17:30
    assert cal.local_day(datetime(2026, 3, 10, 23, 30, tzinfo=UTC)) == date(2026, 3, 11)


def test_circuit_breaker_trips_and_resets():
    cb = CircuitBreaker(max_api_errors=3, max_rejected_orders=2, max_auth_failures=1, window_seconds=60)
    cb.record_api_error(NOW)
    cb.record_api_error(NOW + timedelta(seconds=10))
    assert not cb.tripped
    cb.record_api_error(NOW + timedelta(seconds=20))
    assert cb.tripped and "api_error" in cb.trip_reason
    cb.reset("test")
    assert not cb.tripped
    # window expiry: old errors do not count
    cb.record_api_error(NOW)
    cb.record_api_error(NOW + timedelta(seconds=1))
    cb.record_api_error(NOW + timedelta(seconds=120))
    assert not cb.tripped
    cb.record_auth_failure()
    assert cb.tripped
    restored = CircuitBreaker(3, 2, 1, 60, state=cb.dump())
    assert restored.tripped and restored.trip_reason == cb.trip_reason


def test_breaker_specific_triggers():
    cb = CircuitBreaker(5, 5, 5, 60)
    cb.record_abnormal_price("AAA", "+80%")
    assert cb.tripped and "AAA" in cb.trip_reason
    cb2 = CircuitBreaker(5, 5, 5, 60)
    cb2.record_inconsistent_state("position drift")
    assert cb2.tripped


def test_secret_redaction():
    assert "***" in redact("APCA-API-SECRET-KEY: abc123")
    assert "abc123" not in redact("api_key=abc123")
    assert "tok" not in redact("Authorization: Bearer tokXYZ")
    rec = logging.LogRecord("x", logging.INFO, "", 0, "password=hunter2 ok", None, None)
    SecretRedactingFilter().filter(rec)
    assert "hunter2" not in rec.msg
    rec = logging.LogRecord("comp", logging.INFO, "", 0, "hello", None, None)
    rec.event = "test_event"
    rec.symbol = "AAA"
    payload = json.loads(JsonFormatter().format(rec))
    assert payload["component"] == "comp" and payload["event"] == "test_event" and payload["symbol"] == "AAA"
    assert {"timestamp", "severity", "correlation_id"} <= set(payload)
