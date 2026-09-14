from datetime import timedelta
from decimal import Decimal

import pytest

from trading_agent.broker.base import BrokerConnectionError, InsufficientFundsError, OrderRejectedError
from trading_agent.broker.simulated import SimulatedBroker
from trading_agent.domain import OrderRequest, OrderStatus, OrderType, Side, TimeInForce


def test_market_order_fills_with_slippage_and_commission(broker, prices):
    o = broker.submit_order(OrderRequest("AAA", Side.BUY, Decimal("10")))
    assert o.status == OrderStatus.FILLED
    assert o.avg_fill_price == Decimal("100.1000")  # 10 bps slippage
    assert o.commission == Decimal("1.00")  # 0.1% of 1001
    acct = broker.get_account()
    assert acct.cash == Decimal("100000") - Decimal("1001") - Decimal("1.00")
    pos = broker.get_positions()[0]
    assert pos.quantity == 10 and pos.avg_cost == Decimal("100.1000")
    prices.set("AAA", 110)
    assert broker.get_positions()[0].unrealized_pnl == Decimal("99.00")
    assert broker.get_account().portfolio_value == acct.cash + Decimal("1100")


def test_sell_realises_pnl_and_no_short_selling(broker, prices):
    broker.submit_order(OrderRequest("AAA", Side.BUY, Decimal("10")))
    prices.set("AAA", 120)
    with pytest.raises(OrderRejectedError, match="short"):
        broker.submit_order(OrderRequest("AAA", Side.SELL, Decimal("11")))
    o = broker.submit_order(OrderRequest("AAA", Side.SELL, Decimal("10")))
    assert o.status == OrderStatus.FILLED and o.avg_fill_price == Decimal("119.8800")
    assert broker.realized_pnl > 0 and broker.get_positions() == []


def test_limit_order_waits_then_fills_at_limit(broker, prices, clock):
    o = broker.submit_order(
        OrderRequest("AAA", Side.BUY, Decimal("10"), OrderType.LIMIT, limit_price=Decimal("95"), time_in_force=TimeInForce.GTC)
    )
    assert o.status == OrderStatus.ACCEPTED
    assert broker.get_account().buying_power < Decimal("100000")  # cash reserved for the open order
    prices.set("AAA", 96)
    broker.process_orders()
    assert o.status == OrderStatus.ACCEPTED
    prices.set("AAA", 94)
    broker.process_orders()
    assert o.status == OrderStatus.FILLED and o.avg_fill_price == Decimal("94.0000")


def test_stop_and_stop_limit_orders(broker, prices):
    broker.submit_order(OrderRequest("AAA", Side.BUY, Decimal("10")))
    stop = broker.submit_order(
        OrderRequest("AAA", Side.SELL, Decimal("5"), OrderType.STOP, stop_price=Decimal("90"), time_in_force=TimeInForce.GTC)
    )
    stop_limit = broker.submit_order(
        OrderRequest(
            "AAA",
            Side.SELL,
            Decimal("5"),
            OrderType.STOP_LIMIT,
            stop_price=Decimal("90"),
            limit_price=Decimal("85"),
            time_in_force=TimeInForce.GTC,
        )
    )
    prices.set("AAA", 92)
    broker.process_orders()
    assert stop.status == OrderStatus.ACCEPTED
    prices.set("AAA", 89)
    broker.process_orders()
    assert stop.status == OrderStatus.FILLED and stop.avg_fill_price < Decimal("89")
    assert stop_limit.status == OrderStatus.FILLED and stop_limit.avg_fill_price == Decimal("89.0000")


def test_rejections(broker, calendar):
    with pytest.raises(OrderRejectedError, match="not tradable"):
        broker.submit_order(OrderRequest("ZZZ", Side.BUY, Decimal("1")))
    with pytest.raises(InsufficientFundsError):
        broker.submit_order(OrderRequest("AAA", Side.BUY, Decimal("5000")))
    calendar.open = False
    with pytest.raises(OrderRejectedError, match="closed"):
        broker.submit_order(OrderRequest("AAA", Side.BUY, Decimal("1")))
    rejected = [o for o in broker.get_orders(open_only=False) if o.status == OrderStatus.REJECTED]
    assert len(rejected) == 3 and all(o.reject_reason for o in rejected)


def test_market_hours_block_fills_and_day_orders_expire(broker, prices, clock, calendar):
    calendar.open = False
    o = broker.submit_order(OrderRequest("AAA", Side.BUY, Decimal("10"), OrderType.LIMIT, limit_price=Decimal("100")))
    broker.process_orders()
    assert o.status == OrderStatus.ACCEPTED  # market closed: no fill
    calendar.open = True
    clock.advance(days=1)
    broker.process_orders()  # next session: order placed after close is good for this session
    assert o.status == OrderStatus.FILLED
    o2 = broker.submit_order(OrderRequest("AAA", Side.BUY, Decimal("10"), OrderType.LIMIT, limit_price=Decimal("50")))
    clock.advance(days=1)
    broker.process_orders()
    clock.advance(days=1)
    broker.process_orders()
    assert o2.status == OrderStatus.EXPIRED


def test_partial_fills(clock, calendar, prices, instruments):
    b = SimulatedBroker(prices, clock, calendar.status, partial_fill_probability=1.0, instruments=instruments, seed=1)
    o = b.submit_order(OrderRequest("AAA", Side.BUY, Decimal("100")))
    assert o.status == OrderStatus.PARTIALLY_FILLED and 0 < o.filled_quantity < 100
    for _ in range(20):
        b.process_orders()
        if o.status == OrderStatus.FILLED:
            break
    assert o.status == OrderStatus.FILLED and o.filled_quantity == 100 and len(o.fills) > 1


def test_idempotent_submit_and_cancel(broker):
    req = OrderRequest("AAA", Side.BUY, Decimal("10"), OrderType.LIMIT, limit_price=Decimal("1"), time_in_force=TimeInForce.GTC)
    a = broker.submit_order(req)
    b = broker.submit_order(req)
    assert a is b and len(broker.get_orders(open_only=False)) == 1
    c = broker.cancel_order(a.client_order_id)
    assert c.status == OrderStatus.CANCELLED and broker.get_orders() == []


def test_state_roundtrip(broker, prices, clock, calendar, instruments):
    broker.submit_order(OrderRequest("AAA", Side.BUY, Decimal("10")))
    broker.submit_order(
        OrderRequest("BBB", Side.BUY, Decimal("10"), OrderType.LIMIT, limit_price=Decimal("40"), time_in_force=TimeInForce.GTC)
    )
    state = broker.to_dict()
    restored = SimulatedBroker(prices, clock, calendar.status, instruments=instruments)
    restored.load_dict(state)
    assert restored.cash == broker.cash and restored.positions == broker.positions
    assert len(restored.get_orders(open_only=False)) == 2 and len(restored.get_orders()) == 1
    assert restored.get_orders(open_only=False)[0].fills


def test_connection_failure(broker):
    broker.connected = False
    with pytest.raises(BrokerConnectionError):
        broker.get_account()
    assert not broker.healthcheck()
    broker.connected = True
    assert broker.healthcheck()


def test_position_entry_time(broker, clock):
    assert broker.position_entry_time("AAA") is None
    broker.submit_order(OrderRequest("AAA", Side.BUY, Decimal("10")))
    t0 = clock.now
    clock.advance(days=1)
    broker.submit_order(OrderRequest("AAA", Side.SELL, Decimal("10")))
    assert broker.position_entry_time("AAA") is None
    clock.advance(days=1)
    broker.submit_order(OrderRequest("AAA", Side.BUY, Decimal("5")))
    assert broker.position_entry_time("AAA") == t0 + timedelta(days=2)
