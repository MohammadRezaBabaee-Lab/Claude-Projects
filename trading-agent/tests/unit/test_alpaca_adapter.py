"""Alpaca adapter tests against a mock transport (no network)."""

import json
from decimal import Decimal

import httpx
import pytest

from trading_agent.broker.alpaca import AlpacaBroker
from trading_agent.broker.base import BrokerAuthError, BrokerConnectionError, OrderOutcomeUnknownError, OrderRejectedError
from trading_agent.domain import OrderRequest, OrderStatus, Side

ACCOUNT = {
    "id": "acc",
    "account_number": "PA123",
    "cash": "50000",
    "buying_power": "100000",
    "non_marginable_buying_power": "50000",
    "portfolio_value": "60000",
    "equity": "60000",
    "currency": "USD",
    "multiplier": "2",
    "status": "ACTIVE",
}
ORDER = {
    "id": "b1",
    "client_order_id": "cid1",
    "symbol": "AAA",
    "qty": "10",
    "filled_qty": "10",
    "filled_avg_price": "100.5",
    "side": "buy",
    "type": "market",
    "time_in_force": "day",
    "status": "filled",
    "created_at": "2026-03-10T15:00:00.123456789Z",
    "updated_at": "2026-03-10T15:00:01Z",
}


class Fake:
    def __init__(self):
        self.orders = {}
        self.posts = []
        self.fail_post = None  # exception class to raise on POST
        self.fail_lookup = False
        self.lookups = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        p, m = request.url.path, request.method
        if m == "GET" and p == "/v2/account":
            return httpx.Response(200, json=ACCOUNT)
        if m == "GET" and p == "/v2/positions":
            return httpx.Response(200, json=[{"symbol": "AAA", "qty": "10", "avg_entry_price": "100", "current_price": "110"}])
        if m == "GET" and p == "/v2/clock":
            return httpx.Response(
                200,
                json={
                    "timestamp": "2026-03-10T11:00:00-04:00",
                    "is_open": True,
                    "next_open": "2026-03-11T09:30:00-04:00",
                    "next_close": "2026-03-10T16:00:00-04:00",
                },
            )
        if m == "GET" and p.startswith("/v2/assets/"):
            sym = p.rsplit("/", 1)[1]
            if sym == "NOPE":
                return httpx.Response(404, json={"message": "not found"})
            return httpx.Response(
                200,
                json={
                    "symbol": sym,
                    "name": "Triple A ETF" if sym == "AAA" else "ProShares Ultra 3X",
                    "exchange": "NYSE",
                    "class": "us_equity",
                    "status": "active",
                    "tradable": True,
                    "fractionable": True,
                },
            )
        if m == "GET" and p == "/v2/orders:by_client_order_id":
            self.lookups += 1
            if self.fail_lookup and self.lookups > 1:  # pre-submit check succeeds, post-failure lookup fails
                raise httpx.ConnectError("boom")
            cid = request.url.params.get("client_order_id")
            o = self.orders.get(cid)
            return httpx.Response(200, json=o) if o else httpx.Response(404, json={"message": "order not found"})
        if m == "GET" and p == "/v2/orders":
            return httpx.Response(200, json=list(self.orders.values()))
        if m == "POST" and p == "/v2/orders":
            body = json.loads(request.content)
            self.posts.append(body)
            if self.fail_post is not None:
                exc = self.fail_post
                # simulate "request reached broker but response lost"
                self.orders[body["client_order_id"]] = {
                    **ORDER,
                    "client_order_id": body["client_order_id"],
                    "status": "accepted",
                    "filled_qty": "0",
                    "filled_avg_price": None,
                }
                raise exc("lost")
            if body["symbol"] == "POOR":
                return httpx.Response(403, json={"message": "insufficient buying power"})
            if body["symbol"] == "BAD":
                return httpx.Response(422, json={"message": "invalid qty"})
            o = {
                **ORDER,
                "client_order_id": body["client_order_id"],
                "symbol": body["symbol"],
                "qty": body["qty"],
                "status": "accepted",
                "filled_qty": "0",
                "filled_avg_price": None,
            }
            self.orders[body["client_order_id"]] = o
            return httpx.Response(200, json=o)
        if m == "DELETE":
            return httpx.Response(204)
        return httpx.Response(500, json={"message": "unexpected"})


@pytest.fixture
def fake():
    return Fake()


@pytest.fixture
def broker(fake):
    return AlpacaBroker("key", "secret", live_orders_permitted=False, transport=httpx.MockTransport(fake.handler), max_retries=2)


def test_paper_url_unless_live_gate(fake):
    b = AlpacaBroker("k", "s", live_orders_permitted=False, transport=httpx.MockTransport(fake.handler))
    assert b.is_paper and b.base_url == "https://paper-api.alpaca.markets"
    b = AlpacaBroker("k", "s", live_orders_permitted=True, transport=httpx.MockTransport(fake.handler))
    assert not b.is_paper and b.base_url == "https://api.alpaca.markets"
    with pytest.raises(BrokerAuthError):
        AlpacaBroker("", "", transport=httpx.MockTransport(fake.handler))


def test_account_caps_buying_power_to_cash_without_margin(broker):
    acct = broker.get_account()
    assert acct.cash == Decimal("50000.00")
    assert acct.buying_power == Decimal("50000.00")  # not 100000
    assert acct.multiplier == Decimal("2") and acct.is_paper


def test_positions_orders_clock_assets(broker):
    pos = broker.get_positions()
    assert pos[0].symbol == "AAA" and pos[0].unrealized_pnl == Decimal("100.00")
    st = broker.get_market_status()
    assert st.is_open and st.next_close is not None
    inst = broker.get_instrument("AAA")
    assert inst.tradable and inst.asset_class.value == "etf" and not inst.leveraged
    assert broker.get_instrument("LEV").leveraged
    assert broker.get_instrument("NOPE") is None


def test_submit_order_payload_and_idempotency(broker, fake):
    req = OrderRequest("AAA", Side.BUY, Decimal("10"), client_order_id="cid1")
    o = broker.submit_order(req)
    assert o.status == OrderStatus.ACCEPTED and o.broker_order_id == "b1"
    assert fake.posts[0] == {
        "symbol": "AAA",
        "qty": "10",
        "side": "buy",
        "type": "market",
        "time_in_force": "day",
        "client_order_id": "cid1",
    }
    o2 = broker.submit_order(req)  # second call: found by client id, no new POST
    assert o2.client_order_id == "cid1" and len(fake.posts) == 1


def test_rejections_map_to_exceptions(broker):
    with pytest.raises(OrderRejectedError):
        broker.submit_order(OrderRequest("POOR", Side.BUY, Decimal("10")))
    with pytest.raises(OrderRejectedError):
        broker.submit_order(OrderRequest("BAD", Side.BUY, Decimal("10")))


def test_network_failure_after_submit_finds_order_no_duplicate(broker, fake):
    fake.fail_post = httpx.ReadTimeout
    o = broker.submit_order(OrderRequest("AAA", Side.BUY, Decimal("10"), client_order_id="cid9"))
    assert o.client_order_id == "cid9" and len(fake.posts) == 1


def test_network_failure_and_lookup_failure_is_unknown(broker, fake):
    fake.fail_post = httpx.ConnectError
    fake.fail_lookup = True
    with pytest.raises(OrderOutcomeUnknownError):
        broker.submit_order(OrderRequest("AAA", Side.BUY, Decimal("10"), client_order_id="cid10"))
    assert len(fake.posts) == 1  # POST is never retried blindly


def test_lookup_failure_before_submit_is_not_submitted(fake):
    from trading_agent.broker.base import OrderNotSubmittedError

    def h(request):
        raise httpx.ConnectError("down")

    b = AlpacaBroker("k", "s", transport=httpx.MockTransport(h), max_retries=1)
    with pytest.raises(OrderNotSubmittedError):
        b.submit_order(OrderRequest("AAA", Side.BUY, Decimal("1")))


def test_auth_error():
    def h(request):
        return httpx.Response(401, json={"message": "unauthorized"})

    b = AlpacaBroker("k", "s", transport=httpx.MockTransport(h))
    with pytest.raises(BrokerAuthError):
        b.get_account()


def test_timeout_on_get_retries_then_raises():
    calls = []

    def h(request):
        calls.append(1)
        raise httpx.ReadTimeout("slow")

    b = AlpacaBroker("k", "s", transport=httpx.MockTransport(h), max_retries=2)
    with pytest.raises(BrokerConnectionError):
        b.get_account()
    assert len(calls) == 2


def test_status_mapping_and_fill_synthesis(broker, fake):
    fake.orders["cid1"] = ORDER
    o = broker.get_order("cid1")
    assert o.status == OrderStatus.FILLED and o.filled_quantity == Decimal("10") and o.avg_fill_price == Decimal("100.5")
    assert len(o.fills) == 1 and o.created_at.microsecond == 123456
