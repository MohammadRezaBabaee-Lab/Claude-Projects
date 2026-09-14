"""Sanity checks that guard the strategy against malformed or abnormal price data."""

from __future__ import annotations

from decimal import Decimal

from trading_agent.domain import Bar, Quote
from trading_agent.market_data.base import MalformedDataError


def validate_bars(bars: list[Bar]) -> list[Bar]:
    """Validate OHLC consistency, ordering and duplicates. Raises MalformedDataError."""
    prev = None
    for b in bars:
        try:
            b.validate()
        except ValueError as exc:
            raise MalformedDataError(str(exc)) from exc
        if prev is not None:
            if b.timestamp <= prev.timestamp:
                raise MalformedDataError(f"{b.symbol}: bars not strictly increasing at {b.timestamp}")
        prev = b
    return bars


def abnormal_move(quote: Quote, reference: Decimal, max_move_pct: Decimal) -> bool:
    """True if the quote deviates from a reference (e.g. last close) by more than max_move_pct."""
    if reference <= 0:
        return True
    move = abs(quote.price - reference) / reference
    return move > max_move_pct


def validate_quote(quote: Quote) -> Quote:
    if not quote.price.is_finite() or quote.price <= 0:
        raise MalformedDataError(f"{quote.symbol}: invalid price {quote.price}")
    if quote.timestamp.tzinfo is None:
        raise MalformedDataError(f"{quote.symbol}: naive timestamp")
    return quote
