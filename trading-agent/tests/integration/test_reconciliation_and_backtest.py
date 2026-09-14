from datetime import date
from decimal import Decimal

from tests.conftest import OPEN_TIME
from trading_agent.backtesting import BacktestEngine
from trading_agent.domain import Bar, Order, OrderRequest, OrderStatus, OrderType, Side, TimeInForce
from trading_agent.execution.reconciliation import Reconciler
from trading_agent.market_data.calendar import MarketCalendar
from trading_agent.market_data.synthetic import generate_bars
from trading_agent.strategies import build_strategy


def test_reconciliation_marks_lost_pending_order_failed(broker, repo, breaker):
    o = Order(
        "lost",
        "AAA",
        Side.BUY,
        Decimal("1"),
        OrderType.MARKET,
        OrderStatus.PENDING_SUBMIT,
        OPEN_TIME,
        OPEN_TIME,
        correlation_id="x",
    )
    repo.create_order(o, "k-lost", "paper", [])
    report = Reconciler(broker, repo, breaker, strict=True).run()
    assert "lost" in report["unknown_at_broker"]
    assert repo.get_order("lost").status == OrderStatus.FAILED
    assert not breaker.tripped  # a pending order that never reached the broker is not an inconsistency


def test_reconciliation_detects_foreign_orders_and_position_drift(broker, repo, breaker):
    rec = Reconciler(broker, repo, breaker, strict=True)
    rec.run()  # baseline: flat
    broker.submit_order(
        OrderRequest("AAA", Side.BUY, Decimal("10"), OrderType.LIMIT, limit_price=Decimal("1"), time_in_force=TimeInForce.GTC)
    )  # placed outside the agent
    report = rec.run()
    assert len(report["unknown_locally"]) == 1 and breaker.tripped
    breaker.reset("t")
    broker.positions["AAA"] = {"qty": Decimal("7"), "avg_cost": Decimal("100")}  # manual trade outside the agent
    report = rec.run()
    assert report["position_mismatches"] == [{"symbol": "AAA", "expected": "0", "actual": "7"}] and breaker.tripped


def test_reconciliation_skips_when_broker_down(broker, repo, breaker):
    broker.connected = False
    report = Reconciler(broker, repo, breaker).run()
    assert "error" in report


def _history(symbols, start=date(2023, 1, 3), end=date(2024, 6, 28), cal=None):
    cal = cal or MarketCalendar("XNYS", start="2022-12-01", end="2024-12-31")
    sessions = cal.sessions_in_range(start, end)
    return cal, {s: generate_bars(s, sessions, start_price=100, drift=0.0008, vol=0.012, seed=i) for i, s in enumerate(symbols)}


def test_backtest_produces_all_metrics(settings):
    cal, hist = _history(["AAA", "BBB"])
    res = BacktestEngine(settings, build_strategy("trend_following"), hist, calendar=cal).run()
    m = res.metrics
    for key in (
        "cagr",
        "total_return",
        "annual_volatility",
        "sharpe_ratio",
        "sortino_ratio",
        "max_drawdown",
        "win_rate",
        "profit_factor",
        "n_round_trips",
        "transaction_costs",
        "slippage_cost",
        "avg_exposure",
        "peak_max_position_weight",
    ):
        assert key in m
    assert len(res.equity_curve) == len(cal.sessions_in_range(date(2023, 1, 3), date(2024, 6, 28)))
    assert res.orders > 0 and len(res.fills) > 0
    assert m["transaction_costs"] > 0 and m["slippage_cost"] > 0
    assert all(d.decision != "BUY" or d.risk_checks for d in res.decisions)  # every trade has recorded checks
    assert all(d.reason for d in res.decisions)


def test_backtest_fills_at_next_open_never_at_signal_close(settings):
    cal, hist = _history(["AAA"])
    res = BacktestEngine(settings, build_strategy("trend_following"), hist, calendar=cal).run()
    by_date = {b.timestamp.date(): b for b in hist["AAA"]}
    slip = Decimal("1") + settings.sim_slippage_bps / Decimal("10000")
    for f in res.fills:
        bar = by_date[f.timestamp.date()]
        expected = (bar.open * slip if f.side == Side.BUY else bar.open * (2 - slip)).quantize(Decimal("0.0001"))
        assert f.price == expected, "fill must be at the next session's open (+/- slippage)"


def test_backtest_has_no_look_ahead(settings):
    """Changing prices after a cutoff must not change any decision made before it."""
    cal, hist = _history(["AAA", "BBB"])
    cutoff = date(2024, 1, 15)
    base = BacktestEngine(settings, build_strategy("trend_following"), hist, calendar=cal).run()
    tampered = {}
    for s, bars in hist.items():
        tampered[s] = [
            b
            if b.timestamp.date() <= cutoff
            else Bar(b.symbol, b.timestamp, b.open * 3, b.high * 3, b.low * 3, b.close * 3, b.volume)
            for b in bars
        ]
    alt = BacktestEngine(settings, build_strategy("trend_following"), tampered, calendar=cal).run()

    def before(res):
        return [(d.timestamp, d.symbol, d.decision, str(d.position_size)) for d in res.decisions if d.timestamp.date() <= cutoff]

    assert before(base) == before(alt)
    assert [e for e in base.equity_curve if e[0] <= cutoff] == [e for e in alt.equity_curve if e[0] <= cutoff]


def test_backtest_liquidates_delisted_symbol(settings):
    cal, hist = _history(["AAA", "DEAD"])
    hist["DEAD"] = hist["DEAD"][:120]
    res = BacktestEngine(settings, build_strategy("trend_following"), hist, calendar=cal).run()
    last_day = hist["DEAD"][-1].timestamp.date()
    assert all(d.symbol != "DEAD" or d.timestamp.date() <= last_day for d in res.decisions)
    if res.forced_liquidations:
        assert res.forced_liquidations[0]["symbol"] == "DEAD"
