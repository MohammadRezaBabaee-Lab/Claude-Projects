from datetime import UTC, datetime
from decimal import Decimal

from tests.conftest import OPEN_TIME, make_bars
from trading_agent.domain import SignalAction
from trading_agent.features import atr, compute_features, sma, sma_series
from trading_agent.strategies import available_strategies, build_strategy
from trading_agent.strategies.base import StrategyContext


def test_sma_and_series():
    vals = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert sma(vals, 3) == 4.0
    assert sma(vals, 6) is None
    assert sma_series(vals, 2) == [None, 1.5, 2.5, 3.5, 4.5]


def test_atr_positive_and_requires_history():
    bars = make_bars("X", 30)
    assert atr(bars, 14) is not None and atr(bars, 14) > 0
    assert atr(bars[:5], 14) is None


def test_features_are_json_friendly():
    feats = compute_features(make_bars("X", 60), 20, 50, 14)
    assert feats["sma_20"] is not None and feats["sma_50"] is not None
    assert isinstance(feats["close"], float)


def test_registry_lists_baseline():
    assert "trend_following" in available_strategies()


def _ctx(bars, qty=0, avg=None, entry=None):
    return StrategyContext(
        symbol="X", bars=bars, now=OPEN_TIME, position_quantity=Decimal(qty), position_avg_cost=avg, position_entry_time=entry
    )


def test_insufficient_history_holds():
    strat = build_strategy("trend_following", fast_window=5, slow_window=10, atr_window=3)
    sig = strat.evaluate(_ctx(make_bars("X", 5)))
    assert sig.action == SignalAction.HOLD and "insufficient" in sig.reason


def test_uptrend_produces_buy_with_stop():
    strat = build_strategy("trend_following", fast_window=5, slow_window=10, atr_window=3)
    sig = strat.evaluate(_ctx(make_bars("X", 40, start=50, step=1.0)))
    assert sig.action == SignalAction.BUY
    assert sig.stop_price is not None and sig.stop_price < sig.reference_price
    assert "SMA" in sig.reason


def test_downtrend_holds_when_flat():
    strat = build_strategy("trend_following", fast_window=5, slow_window=10, atr_window=3)
    sig = strat.evaluate(_ctx(make_bars("X", 40, start=100, step=-1.0)))
    assert sig.action == SignalAction.HOLD


def test_cross_down_sells_when_holding():
    strat = build_strategy("trend_following", fast_window=3, slow_window=6, atr_window=3)
    bars = make_bars("X", 30, start=50, step=1.0) + make_bars("X", 6, start=80, step=-4.0, vol=0.1)
    # re-stamp so timestamps are increasing
    from trading_agent.domain import Bar

    bars = [
        Bar(
            "X",
            datetime(2026, 1, 1, 21, tzinfo=UTC).replace(day=1) + __import__("datetime").timedelta(days=i),
            b.open,
            b.high,
            b.low,
            b.close,
            b.volume,
        )
        for i, b in enumerate(bars)
    ]
    sig = strat.evaluate(_ctx(bars, qty=10, avg=Decimal("70")))
    assert sig.action == SignalAction.SELL


def test_trailing_stop_sells_when_holding():
    strat = build_strategy("trend_following", fast_window=5, slow_window=10, atr_window=3, atr_stop_multiplier=Decimal("1"))
    bars = make_bars("X", 40, start=50, step=1.0)
    # sharp drop on the last bar but SMAs still in uptrend
    last = bars[-1]
    from trading_agent.domain import Bar

    bars[-1] = Bar("X", last.timestamp, last.open, last.high, last.low - 20, last.close - 20, last.volume)
    sig = strat.evaluate(_ctx(bars, qty=10, avg=Decimal("60"), entry=bars[0].timestamp))
    assert sig.action == SignalAction.SELL and "trailing stop" in sig.reason


def test_strategy_never_touches_broker():
    import inspect

    import trading_agent.strategies.trend_following as mod

    assert "broker" not in inspect.getsource(mod).lower()
