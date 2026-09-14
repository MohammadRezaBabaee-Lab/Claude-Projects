"""Failure-mode tests. The invariant under test everywhere: a failure never creates a duplicate trade."""

from datetime import timedelta
from decimal import Decimal

import pytest

from tests.conftest import OPEN_TIME, StaticData, make_bars
from trading_agent.broker.base import BrokerConnectionError, OrderOutcomeUnknownError, OrderRejectedError
from trading_agent.domain import Bar, OrderStatus, Quote
from trading_agent.execution.order_manager import OrderManager


def _orders(repo):
    return repo.orders()


def test_broker_unavailable_is_contained(cycle, broker, repo, breaker, notifier):
    broker.connected = False
    report = cycle.run_once()
    assert not report.broker_connected and report.errors and report.orders_submitted == 0
    assert repo.last_run()["status"] == "degraded"
    assert any(n["event_type"] == "broker_connection_failure" for n in notifier.sent)
    broker.connected = True
    assert cycle.run_once().broker_connected


def test_repeated_outages_trip_breaker(cycle, broker, breaker, settings):
    broker.connected = False
    for _ in range(settings.circuit_breaker_max_api_errors):
        cycle.run_once()
    assert breaker.tripped
    broker.connected = True
    report = cycle.run_once()
    assert report.orders_submitted == 0  # tripped breaker blocks orders


def test_stale_market_data_blocks_trading(cycle, prices, repo):
    prices.stale_by = timedelta(hours=2)
    report = cycle.run_once()
    assert any("old" in e for e in report.errors) and report.orders_submitted == 0 and _orders(repo) == []


def test_malformed_market_data_blocks_trading(cycle, data, repo):
    good = data.bars["AAA"]
    data.bars["AAA"] = good[:-1] + [
        Bar("AAA", good[-1].timestamp, good[-1].open, Decimal("1"), good[-1].low, good[-1].close, good[-1].volume)
    ]  # high < low
    report = cycle.run_once()
    assert any("AAA" in e for e in report.errors) and _orders(repo) == []


def test_abnormal_price_trips_breaker(cycle, prices, breaker, repo):
    prices.set("AAA", 400)  # +300% vs last close
    cycle.run_once()
    assert breaker.tripped and "abnormal" in breaker.trip_reason and _orders(repo) == []


def test_insufficient_cash_is_rejected_not_crashed(settings, cycle, broker, repo):
    broker.cash = Decimal("50")
    report = cycle.run_once()
    assert report.orders_submitted == 0 and _orders(repo) == [] and repo.last_run()["status"] == "ok"


def test_rejected_order_recorded_and_counted(settings, broker, repo, oms, breaker, metrics, notifier):
    from tests.integration.test_order_lifecycle import _decision, _state

    original = broker.submit_order

    def reject(req):
        raise OrderRejectedError("simulated rejection")

    broker.submit_order = reject
    res = oms.submit(_decision(), _state(settings, broker, repo), True, OPEN_TIME.date())
    assert not res.submitted and res.order.status == OrderStatus.REJECTED
    assert repo.get_order(res.order.client_order_id).reject_reason == "simulated rejection"
    assert metrics.snapshot()["derived"]["order_rejection_rate"] == 1.0
    assert any(n["event_type"] == "order_rejected" for n in notifier.sent)
    broker.submit_order = original
    # a rejected order is not retried the same day (idempotency key already used)
    res2 = oms.submit(_decision(), _state(settings, broker, repo), True, OPEN_TIME.date())
    assert not res2.submitted and "idempotent" in res2.reason


def test_timeout_after_broker_accepted_does_not_duplicate(settings, broker, repo, oms):
    """Network drops after the broker accepted the order: the OMS must find it, not resend it."""
    from tests.integration.test_order_lifecycle import _decision, _state

    real_submit = broker.submit_order

    def flaky(req):
        real_submit(req)  # broker got it
        raise BrokerConnectionError("timeout reading response")

    broker.submit_order = flaky
    res = oms.submit(_decision(cid="cid-flaky"), _state(settings, broker, repo), True, OPEN_TIME.date())
    broker.submit_order = real_submit
    assert res.submitted and res.order.status == OrderStatus.FILLED
    assert len(broker.get_orders(open_only=False)) == 1 and len(_orders(repo)) == 1


def test_timeout_before_broker_received_marks_failed_and_allows_one_retry(settings, broker, repo, oms):
    from tests.integration.test_order_lifecycle import _decision, _state

    real_submit = broker.submit_order

    def dead(req):
        raise BrokerConnectionError("connection refused")

    broker.submit_order = dead
    res = oms.submit(_decision(), _state(settings, broker, repo), True, OPEN_TIME.date())
    assert not res.submitted and res.order.status == OrderStatus.FAILED
    broker.submit_order = real_submit
    res2 = oms.submit(_decision(), _state(settings, broker, repo), True, OPEN_TIME.date())
    assert res2.submitted and res2.order.status == OrderStatus.FILLED
    assert len(broker.get_orders(open_only=False)) == 1


def test_unknown_outcome_blocks_until_reconciled(settings, broker, repo, oms):
    from tests.integration.test_order_lifecycle import _decision, _state

    real_submit = broker.submit_order

    def unknown(req):
        raise OrderOutcomeUnknownError("lost response and lookup failed")

    broker.submit_order = unknown
    res = oms.submit(_decision(), _state(settings, broker, repo), True, OPEN_TIME.date())
    assert res.order.status == OrderStatus.UNKNOWN
    broker.submit_order = real_submit
    # while UNKNOWN the order is 'open' locally: an equivalent order is vetoed, never duplicated
    res2 = oms.submit(_decision(), _state(settings, broker, repo), True, OPEN_TIME.date())
    assert not res2.submitted and len(broker.get_orders(open_only=False)) == 0
    # reconciliation resolves it: not at broker -> FAILED
    from trading_agent.execution.reconciliation import Reconciler

    Reconciler(broker, repo, oms.breaker, strict=False).run()
    assert repo.get_order(res.order.client_order_id).status == OrderStatus.FAILED


def test_duplicate_order_request_same_client_id(broker):
    from trading_agent.domain import OrderRequest, Side

    req = OrderRequest("AAA", Side.BUY, Decimal("10"), client_order_id="dup-1")
    a = broker.submit_order(req)
    b = broker.submit_order(OrderRequest("AAA", Side.BUY, Decimal("10"), client_order_id="dup-1"))
    assert a.broker_order_id == b.broker_order_id and len(broker.get_orders(open_only=False)) == 1


def test_partial_fill_tracked_to_completion(settings, clock, calendar, prices, instruments, repo, breaker, notifier, metrics):
    from tests.integration.test_order_lifecycle import _decision, _state
    from trading_agent.broker.simulated import SimulatedBroker

    broker = SimulatedBroker(prices, clock, calendar.status, partial_fill_probability=1.0, instruments=instruments, seed=3)
    oms = OrderManager(broker, repo, breaker, notifier, metrics, "paper", True)
    res = oms.submit(_decision(qty="100"), _state(settings, broker, repo), True, OPEN_TIME.date())
    assert res.order.status == OrderStatus.PARTIALLY_FILLED
    for _ in range(30):
        broker.process_orders()
        oms.track_open_orders()
        if repo.get_order(res.order.client_order_id).status == OrderStatus.FILLED:
            break
    stored = repo.get_order(res.order.client_order_id)
    assert stored.status == OrderStatus.FILLED and stored.filled_quantity == 100 and len(stored.fills) > 1
    assert sum(f.quantity for f in stored.fills) == 100


def test_database_unavailable_is_contained(cycle, db, repo, broker):
    db.engine.dispose()
    import os

    os.remove(db.url.replace("sqlite:///", ""))
    os.makedirs(db.url.replace("sqlite:///", ""))  # a directory where the db file should be -> unwritable
    try:
        with pytest.raises(Exception):  # noqa: B017 - any DB driver error is acceptable here
            cycle.run_once()
        assert len(broker.get_orders(open_only=False)) == 0  # DB failure before any submission
    finally:
        os.rmdir(db.url.replace("sqlite:///", ""))


def test_process_restart_with_pending_order_recovers(settings, broker, repo, oms):
    """Order persisted as PENDING_SUBMIT, process dies before the broker call, restart reconciles."""
    from trading_agent.domain import Order, OrderType, Side

    o = Order(
        "pending-x",
        "AAA",
        Side.BUY,
        OrderStatus.PENDING_SUBMIT and Decimal("10"),
        OrderType.MARKET,
        OrderStatus.PENDING_SUBMIT,
        OPEN_TIME - timedelta(minutes=10),
        OPEN_TIME - timedelta(minutes=10),
        correlation_id="x",
    )
    repo.create_order(o, "k-pending", "paper", [])
    changed = oms.track_open_orders()  # what startup() does
    assert changed and repo.get_order("pending-x").status == OrderStatus.FAILED
    assert broker.get_orders(open_only=False) == []


def test_network_interruption_mid_cycle_no_orphans(cycle, broker, repo):
    original = broker.get_orders
    calls = {"n": 0}

    def flaky(open_only=True):
        calls["n"] += 1
        if calls["n"] == 1:
            raise BrokerConnectionError("blip")
        return original(open_only)

    broker.get_orders = flaky
    report = cycle.run_once()
    broker.get_orders = original
    # first attempt failed while checking open orders -> no order this cycle; state consistent
    assert report.orders_submitted == 0 and repo.last_run()["status"] in ("ok", "degraded")
    assert all(not o.is_open for o in repo.orders())
    report2 = cycle.run_once()
    assert report2.orders_submitted == 1 and len(repo.orders()) == 1


def test_quote_missing_for_symbol(cycle, prices, repo):
    del prices.prices["AAA"]
    report = cycle.run_once()
    assert any("AAA" in e for e in report.errors) and report.symbols_evaluated == 1


def test_static_data_helper(prices):
    d = StaticData({"AAA": make_bars("AAA", 3)}, prices)
    assert isinstance(d.get_quote("AAA"), Quote) and len(d.get_bars("AAA", limit=2)) == 2
