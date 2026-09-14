from datetime import timedelta
from decimal import Decimal

import pytest

from tests.conftest import OPEN_TIME
from trading_agent.db.repository import DuplicateOrderError
from trading_agent.domain import Account, Fill, Order, OrderStatus, OrderType, Position, Side


def _order(cid="c1", status=OrderStatus.PENDING_SUBMIT):
    return Order(
        cid, "AAA", Side.BUY, Decimal("10"), OrderType.MARKET, status, OPEN_TIME, OPEN_TIME, strategy="t", correlation_id="x"
    )


def test_order_idempotency_key_unique(repo):
    key = repo.idempotency_key("AAA", Side.BUY, Decimal("10"), "t", "2026-03-10")
    repo.create_order(_order("c1"), key, "paper", [])
    with pytest.raises(DuplicateOrderError):
        repo.create_order(_order("c2"), key, "paper", [])
    assert repo.find_order_by_idempotency_key(key).client_order_id == "c1"
    assert repo.get_order("c2") is None


def test_order_update_and_fills_idempotent(repo):
    o = _order("c1")
    repo.create_order(o, "k1", "paper", [{"name": "x", "passed": True}])
    o.status = OrderStatus.FILLED
    o.filled_quantity = Decimal("10")
    o.avg_fill_price = Decimal("100")
    o.fills.append(Fill("f1", "c1", "AAA", Side.BUY, Decimal("10"), Decimal("100"), Decimal("1"), OPEN_TIME))
    repo.update_order(o)
    repo.update_order(o)  # same fill again -> no duplicate row
    loaded = repo.get_order("c1")
    assert loaded.status == OrderStatus.FILLED and len(loaded.fills) == 1 and loaded.fills[0].timestamp.tzinfo is not None
    assert repo.open_orders() == []
    assert len(repo.fills()) == 1
    assert repo.count_orders_since(OPEN_TIME - timedelta(hours=1)) == 1
    assert repo.count_orders_since(OPEN_TIME - timedelta(hours=1), "BBB") == 0


def test_snapshots_positions_kv_and_config(repo, settings):
    snap = {
        "timestamp": OPEN_TIME,
        "available_cash": Decimal("90000"),
        "reserved_cash": Decimal("9000"),
        "invested_capital": Decimal("10000"),
        "portfolio_value": Decimal("100000"),
        "buying_power": Decimal("81000"),
        "exposure": Decimal("0.1"),
        "daily_pnl": Decimal("0"),
        "total_pnl": Decimal("0"),
        "drawdown": Decimal("0"),
        "peak_value": Decimal("100000"),
    }
    repo.record_snapshot(snap, [Position("AAA", Decimal("100"), Decimal("95"), Decimal("100"), OPEN_TIME)], "paper")
    assert repo.snapshots()[-1]["portfolio_value"].startswith("100000")
    pos = repo.latest_positions()
    assert pos[0]["symbol"] == "AAA" and pos[0]["weight"] == "0.1000"
    assert repo.get_state("nothing") is None
    repo.set_state("k", {"a": 1})
    repo.set_state("k", {"a": 2})
    assert repo.get_state("k") == {"a": 2}
    assert repo.record_configuration(settings.redacted()) is True
    assert repo.record_configuration(settings.redacted()) is False
    repo.record_account(
        Account("A", "USD", Decimal("1"), Decimal("1"), Decimal("1"), Decimal("1"), OPEN_TIME), "simulated", "paper"
    )
    repo.record_system_event("t", "e", "ERROR", "m")
    repo.record_risk_event("veto", "WARNING", "d", symbol="AAA")
    assert repo.recent_events(10, "system")[0]["event_type"] == "e"
    assert repo.recent_events(10, "risk")[0]["symbol"] == "AAA"
    assert repo.count_system_events_since(OPEN_TIME - timedelta(days=3650)) == 1


def test_runs(repo):
    repo.start_run("r1", "cid", "trend", "paper")
    repo.finish_run("r1", "ok", orders_submitted=2)
    last = repo.last_run()
    assert last["run_id"] == "r1" and last["orders_submitted"] == 2 and last["latency_ms"] is not None
