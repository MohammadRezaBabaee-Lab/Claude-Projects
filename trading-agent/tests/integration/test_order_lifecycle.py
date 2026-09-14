from decimal import Decimal

from tests.conftest import OPEN_TIME
from trading_agent.domain import Decision, OrderRequest, OrderStatus, RiskCheck, Side, SignalAction
from trading_agent.execution.order_manager import OrderManager
from trading_agent.portfolio.manager import PortfolioManager


def _decision(symbol="AAA", qty="10", side=Side.BUY, price="100", checks_pass=True, cid=None):
    req = OrderRequest(symbol, side, Decimal(qty), strategy="t", reason="test", **({"client_order_id": cid} if cid else {}))
    return Decision(
        decision_id="d1",
        timestamp=OPEN_TIME,
        symbol=symbol,
        current_price=Decimal(price),
        strategy="t",
        signal_action=SignalAction.BUY if side == Side.BUY else SignalAction.SELL,
        signal_reason="test",
        features={},
        portfolio_state={},
        risk_checks=[RiskCheck("x", checks_pass, "d")],
        position_size=Decimal(qty),
        sizing_detail={},
        decision=side.value.upper(),
        reason="r",
        order_request=req,
    )


def _state(settings, broker, repo):
    pm = PortfolioManager(settings.cash_reserve)
    return pm.build(broker.get_account(), broker.get_positions(), repo.open_orders(), OPEN_TIME.date(), OPEN_TIME)


def test_full_lifecycle_buy_then_sell(settings, broker, repo, oms, notifier, metrics):
    res = oms.submit(_decision(), _state(settings, broker, repo), True, OPEN_TIME.date())
    assert res.submitted and res.order.status == OrderStatus.FILLED
    stored = repo.get_order(res.order.client_order_id)
    assert stored.status == OrderStatus.FILLED and stored.broker_order_id and len(stored.fills) == 1
    assert {n["event_type"] for n in notifier.sent} >= {"order_submitted", "order_filled"}
    assert metrics.snapshot()["counters"]["orders_submitted"] == 1
    res = oms.submit(_decision(side=Side.SELL), _state(settings, broker, repo), True, OPEN_TIME.date())
    assert res.submitted and res.order.side == Side.SELL and broker.get_positions() == []


def test_idempotent_retry_does_not_duplicate(settings, broker, repo, oms):
    d = _decision()
    first = oms.submit(d, _state(settings, broker, repo), True, OPEN_TIME.date())
    second = oms.submit(_decision(), _state(settings, broker, repo), True, OPEN_TIME.date())  # same day/symbol/side/qty
    assert first.submitted and not second.submitted and "idempotent" in second.reason
    assert len(repo.orders()) == 1 and len(broker.get_orders(open_only=False)) == 1


def test_vetoes(settings, broker, repo, oms, breaker):
    st = _state(settings, broker, repo)
    d = _decision(checks_pass=False)
    assert "risk checks failed" in oms.submit(d, st, True, OPEN_TIME.date()).reason
    assert "market closed" in oms.submit(_decision(), st, False, OPEN_TIME.date()).reason
    oms.trading_enabled = False
    assert "kill switch" in oms.submit(_decision(), st, True, OPEN_TIME.date()).reason
    oms.trading_enabled = True
    breaker.trip("test")
    assert "circuit breaker" in oms.submit(_decision(), st, True, OPEN_TIME.date()).reason
    breaker.reset("test")
    assert "not tradable" in oms.submit(_decision(symbol="ZZZ"), st, True, OPEN_TIME.date()).reason
    assert "buying power" in oms.submit(_decision(qty="5000"), st, True, OPEN_TIME.date()).reason
    assert "only 0 held" in oms.submit(_decision(side=Side.SELL), st, True, OPEN_TIME.date()).reason
    assert repo.orders() == []  # nothing persisted for vetoes
    assert len(repo.recent_events(50, "risk")) >= 6


def test_live_gate_inside_oms(settings, broker, repo, breaker, notifier, metrics):
    live_oms = OrderManager(
        broker, repo, breaker, notifier, metrics, mode="live", trading_enabled=True, live_orders_permitted=False
    )
    res = live_oms.submit(_decision(), _state(settings, broker, repo), True, OPEN_TIME.date())
    assert not res.submitted and "live gate" in res.reason


def test_equivalent_open_order_veto(settings, broker, repo, oms):
    from trading_agent.domain import OrderType

    req = OrderRequest("AAA", Side.BUY, Decimal("5"), OrderType.LIMIT, limit_price=Decimal("50"), strategy="t")
    d = _decision(qty="5")
    d.order_request = req
    d.position_size = Decimal("5")
    res = oms.submit(d, _state(settings, broker, repo), True, OPEN_TIME.date())
    assert res.submitted and res.order.status == OrderStatus.ACCEPTED
    res2 = oms.submit(_decision(qty="7"), _state(settings, broker, repo), True, OPEN_TIME.date())
    assert "equivalent open order" in res2.reason


def test_track_open_orders_updates_fills(settings, broker, repo, oms, prices):
    from trading_agent.domain import OrderType

    d = _decision(qty="5")
    d.order_request = OrderRequest("AAA", Side.BUY, Decimal("5"), OrderType.LIMIT, limit_price=Decimal("90"), strategy="t")
    d.position_size = Decimal("5")
    oms.submit(d, _state(settings, broker, repo), True, OPEN_TIME.date())
    assert oms.track_open_orders() == []
    prices.set("AAA", 89)
    broker.process_orders()
    changed = oms.track_open_orders()
    assert len(changed) == 1 and changed[0].status == OrderStatus.FILLED
    assert repo.get_order(changed[0].client_order_id).filled_quantity == 5
    assert oms.cancel_all_open("test") == 0
