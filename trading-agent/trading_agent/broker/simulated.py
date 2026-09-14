"""Simulated broker used for backtesting and paper trading.

It behaves like a real broker: cash and positions, market/limit/stop/stop-limit
orders, commissions, slippage, market hours, rejections, partial fills, order
status transitions, DAY-order expiry and portfolio valuation. Its full state is
serialisable so paper-trading sessions survive restarts.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from trading_agent.broker.base import (
    Broker,
    InsufficientFundsError,
    OrderRejectedError,
)
from trading_agent.domain import (
    Account,
    AssetClass,
    Fill,
    Instrument,
    MarketStatus,
    Order,
    OrderRequest,
    OrderStatus,
    OrderType,
    Position,
    Quote,
    Side,
    TimeInForce,
    money,
    new_id,
    price,
    utcnow,
    whole_shares,
)

PriceSource = Callable[[str], Quote | None]
Clock = Callable[[], datetime]
MarketStatusFn = Callable[[datetime], MarketStatus]


class SimulatedBroker(Broker):
    name = "simulated"
    is_paper = True

    def __init__(
        self,
        price_source: PriceSource,
        clock: Clock | None = None,
        market_status_fn: MarketStatusFn | None = None,
        initial_cash: Decimal = Decimal("100000"),
        currency: str = "USD",
        commission_per_order: Decimal = Decimal("0"),
        commission_pct: Decimal = Decimal("0"),
        slippage_bps: Decimal = Decimal("0"),
        partial_fill_probability: float = 0.0,
        enforce_market_hours: bool = True,
        instruments: dict[str, Instrument] | None = None,
        seed: int = 42,
        account_id: str = "SIM-0001",
    ) -> None:
        self._price_source = price_source
        self._clock = clock or utcnow
        self._market_status_fn = market_status_fn
        self.currency = currency
        self.initial_cash = Decimal(initial_cash)
        self.cash = Decimal(initial_cash)
        self.commission_per_order = Decimal(commission_per_order)
        self.commission_pct = Decimal(commission_pct)
        self.slippage_bps = Decimal(slippage_bps)
        self.partial_fill_probability = partial_fill_probability
        self.enforce_market_hours = enforce_market_hours
        self.instruments = instruments  # None -> every symbol with a price is tradable
        self.account_id = account_id
        self._rng = random.Random(seed)  # noqa: S311 - simulation only
        self.positions: dict[str, dict[str, Decimal]] = {}  # symbol -> {qty, avg_cost}
        self.orders: dict[str, Order] = {}
        self.realized_pnl = Decimal("0")
        self.total_commission = Decimal("0")
        self.total_slippage_cost = Decimal("0")
        self._last_session: date | None = None
        self.connected = True  # tests flip this to simulate outages
        self.auto_fill = True  # backtests set False so fills only happen on explicit process_orders()

    # ------------------------------------------------------------- helpers
    def now(self) -> datetime:
        return self._clock()

    def _require_connection(self) -> None:
        if not self.connected:
            from trading_agent.broker.base import BrokerConnectionError

            raise BrokerConnectionError("simulated broker unreachable")

    def _quote(self, symbol: str) -> Quote | None:
        return self._price_source(symbol)

    def _commission(self, notional: Decimal) -> Decimal:
        return money(self.commission_per_order + notional * self.commission_pct)

    def _slip(self, px: Decimal, side: Side) -> Decimal:
        factor = self.slippage_bps / Decimal("10000")
        return price(px * (1 + factor)) if side == Side.BUY else price(px * (1 - factor))

    def _reserved_cash(self) -> Decimal:
        total = Decimal("0")
        for o in self.orders.values():
            if o.is_open and o.side == Side.BUY:
                ref = o.limit_price or self._reference_price(o.symbol) or Decimal("0")
                est = o.remaining_quantity * self._slip(ref, Side.BUY)
                total += est + self._commission(est)
        return money(total)

    def _committed_shares(self, symbol: str) -> Decimal:
        return sum(
            (o.remaining_quantity for o in self.orders.values() if o.is_open and o.side == Side.SELL and o.symbol == symbol),
            Decimal("0"),
        )

    def _reference_price(self, symbol: str) -> Decimal | None:
        q = self._quote(symbol)
        return q.price if q else None

    # ----------------------------------------------------------- interface
    def get_market_status(self) -> MarketStatus:
        self._require_connection()
        now = self.now()
        if self._market_status_fn is None:
            return MarketStatus(is_open=True, timestamp=now, source="simulated:always-open")
        return self._market_status_fn(now)

    def get_instrument(self, symbol: str) -> Instrument | None:
        if self.instruments is not None:
            return self.instruments.get(symbol)
        if self._quote(symbol) is None:
            return None
        return Instrument(symbol=symbol, currency=self.currency, asset_class=AssetClass.EQUITY, tradable=True)

    def get_account(self) -> Account:
        self._require_connection()
        pv = self.portfolio_value()
        reserved = self._reserved_cash()
        return Account(
            account_id=self.account_id,
            currency=self.currency,
            cash=money(self.cash),
            buying_power=money(max(self.cash - reserved, Decimal("0"))),
            portfolio_value=pv,
            equity=pv,
            timestamp=self.now(),
            multiplier=Decimal("1"),
            is_paper=True,
        )

    def portfolio_value(self) -> Decimal:
        total = self.cash
        for sym, p in self.positions.items():
            px = self._reference_price(sym) or p["avg_cost"]
            total += p["qty"] * px
        return money(total)

    def get_positions(self) -> list[Position]:
        self._require_connection()
        out = []
        for sym, p in self.positions.items():
            if p["qty"] == 0:
                continue
            px = self._reference_price(sym) or p["avg_cost"]
            inst = self.instruments.get(sym) if self.instruments else None
            out.append(
                Position(
                    symbol=sym,
                    quantity=p["qty"],
                    avg_cost=price(p["avg_cost"]),
                    current_price=price(px),
                    timestamp=self.now(),
                    sector=inst.sector if inst else None,
                )
            )
        return out

    def get_orders(self, open_only: bool = True) -> list[Order]:
        self._require_connection()
        return [o for o in self.orders.values() if (o.is_open or not open_only)]

    def get_order(self, client_order_id: str) -> Order | None:
        self._require_connection()
        return self.orders.get(client_order_id)

    def submit_order(self, request: OrderRequest) -> Order:
        self._require_connection()
        # Idempotency: same client order id -> same order, never a duplicate.
        if request.client_order_id in self.orders:
            return self.orders[request.client_order_id]
        request.validate()
        now = self.now()
        order = Order(
            client_order_id=request.client_order_id,
            symbol=request.symbol,
            side=request.side,
            quantity=whole_shares(request.quantity),
            order_type=request.order_type,
            status=OrderStatus.SUBMITTED,
            created_at=now,
            updated_at=now,
            limit_price=request.limit_price,
            stop_price=request.stop_price,
            time_in_force=request.time_in_force,
            broker_order_id=new_id("sim_"),
            strategy=request.strategy,
            reason=request.reason,
            correlation_id=request.correlation_id,
        )
        inst = self.get_instrument(request.symbol)
        try:
            if inst is None or not inst.tradable:
                raise OrderRejectedError(f"{request.symbol} is not tradable")
            if self.enforce_market_hours and not self.get_market_status().is_open and request.order_type == OrderType.MARKET:
                raise OrderRejectedError("market is closed: market orders are not accepted outside trading hours")
            ref = request.limit_price or self._reference_price(request.symbol)
            if ref is None:
                raise OrderRejectedError(f"no price available for {request.symbol}")
            if request.side == Side.BUY:
                est_px = self._slip(ref, Side.BUY)
                est_cost = order.quantity * est_px
                est_cost += self._commission(est_cost)
                available = self.cash - self._reserved_cash()
                if est_cost > available:
                    raise InsufficientFundsError(
                        f"insufficient buying power: need {money(est_cost)} {self.currency}, available {money(available)}"
                    )
            else:
                held = self.positions.get(request.symbol, {}).get("qty", Decimal("0"))
                free = held - self._committed_shares(request.symbol)
                if order.quantity > free:
                    raise OrderRejectedError(
                        f"cannot sell {order.quantity} {request.symbol}: only {free} uncommitted shares held (no short selling)"
                    )
        except OrderRejectedError as exc:
            order.status = OrderStatus.REJECTED
            order.reject_reason = str(exc)
            self.orders[order.client_order_id] = order
            raise
        order.status = OrderStatus.ACCEPTED
        self.orders[order.client_order_id] = order
        # Try to fill immediately if the market is open - realistic for liquid names.
        if self.auto_fill:
            self.process_orders()
        return order

    def cancel_order(self, client_order_id: str) -> Order:
        self._require_connection()
        order = self.orders.get(client_order_id)
        if order is None:
            raise OrderRejectedError(f"unknown order {client_order_id}")
        if order.status.is_terminal:
            return order
        order.status = OrderStatus.CANCELLED
        order.updated_at = self.now()
        return order

    # ----------------------------------------------------------- matching
    def process_orders(self, at: datetime | None = None) -> list[Fill]:
        """Attempt to fill open orders against current prices. Call on every tick/bar."""
        now = at or self.now()
        fills: list[Fill] = []
        session = now.date()
        if self._last_session is not None and session != self._last_session:
            self._expire_day_orders(now)
        self._last_session = session
        if self.enforce_market_hours and not self.get_market_status().is_open:
            return fills
        for order in list(self.orders.values()):
            if not order.is_open:
                continue
            quote = self._quote(order.symbol)
            if quote is None:
                continue
            fill_px = self._match(order, quote.price)
            if fill_px is None:
                continue
            fills.extend(self._execute(order, fill_px, quote.price, now))
        return fills

    def _expire_day_orders(self, now: datetime) -> None:
        # A DAY order is good for the session in which it was placed; an order placed after
        # the close is good for the *next* session (what real brokers do with queued orders).
        assert self._last_session is not None
        for o in self.orders.values():
            if o.is_open and o.time_in_force == TimeInForce.DAY and o.created_at.date() < self._last_session:
                o.status = OrderStatus.EXPIRED if o.filled_quantity == 0 else OrderStatus.CANCELLED
                o.updated_at = now

    def _match(self, order: Order, px: Decimal) -> Decimal | None:
        """Return the execution price (before slippage) or None if the order does not trigger."""
        t = order.order_type
        if t == OrderType.MARKET:
            return px
        if t == OrderType.LIMIT:
            assert order.limit_price is not None
            if order.side == Side.BUY and px <= order.limit_price:
                return min(px, order.limit_price)
            if order.side == Side.SELL and px >= order.limit_price:
                return max(px, order.limit_price)
            return None
        if t == OrderType.STOP:
            assert order.stop_price is not None
            if order.side == Side.BUY and px >= order.stop_price:
                return px
            if order.side == Side.SELL and px <= order.stop_price:
                return px
            return None
        if t == OrderType.STOP_LIMIT:
            assert order.stop_price is not None and order.limit_price is not None
            triggered = (order.side == Side.BUY and px >= order.stop_price) or (
                order.side == Side.SELL and px <= order.stop_price
            )
            if not triggered:
                return None
            if order.side == Side.BUY and px <= order.limit_price:
                return px
            if order.side == Side.SELL and px >= order.limit_price:
                return px
            return None
        return None

    def _execute(self, order: Order, ref_px: Decimal, mkt_px: Decimal, now: datetime) -> list[Fill]:
        remaining = order.remaining_quantity
        qty = remaining
        if self.partial_fill_probability > 0 and remaining > 1 and self._rng.random() < self.partial_fill_probability:
            qty = max(Decimal("1"), whole_shares(remaining * Decimal(str(self._rng.uniform(0.3, 0.9)))))
        # Limit orders never fill worse than the limit; market/stop orders pay slippage.
        if order.order_type in (OrderType.LIMIT, OrderType.STOP_LIMIT):
            px = price(ref_px)
        else:
            px = self._slip(ref_px, order.side)
            self.total_slippage_cost += money(abs(px - mkt_px) * qty)
        notional = qty * px
        commission = self._commission(notional)
        if order.side == Side.BUY:
            if notional + commission > self.cash:
                order.status = OrderStatus.REJECTED
                order.reject_reason = "insufficient cash at execution time"
                order.updated_at = now
                return []
            self.cash -= notional + commission
            pos = self.positions.setdefault(order.symbol, {"qty": Decimal("0"), "avg_cost": Decimal("0")})
            new_qty = pos["qty"] + qty
            pos["avg_cost"] = (pos["qty"] * pos["avg_cost"] + notional) / new_qty
            pos["qty"] = new_qty
        else:
            pos = self.positions.get(order.symbol)
            if pos is None or pos["qty"] < qty:
                order.status = OrderStatus.REJECTED
                order.reject_reason = "insufficient shares at execution time"
                order.updated_at = now
                return []
            self.cash += notional - commission
            self.realized_pnl += money((px - pos["avg_cost"]) * qty) - commission
            pos["qty"] -= qty
            if pos["qty"] == 0:
                del self.positions[order.symbol]
        self.total_commission += commission
        fill = Fill(
            fill_id=new_id("fill_"),
            client_order_id=order.client_order_id,
            symbol=order.symbol,
            side=order.side,
            quantity=qty,
            price=px,
            commission=commission,
            timestamp=now,
            broker_fill_id=new_id("simfill_"),
        )
        prev_notional = (order.avg_fill_price or Decimal("0")) * order.filled_quantity
        order.filled_quantity += qty
        order.avg_fill_price = price((prev_notional + notional) / order.filled_quantity)
        order.commission += commission
        order.fills.append(fill)
        order.status = OrderStatus.FILLED if order.remaining_quantity == 0 else OrderStatus.PARTIALLY_FILLED
        order.updated_at = now
        return [fill]

    def position_entry_time(self, symbol: str) -> datetime | None:
        """Timestamp of the first fill of the currently held lot (None if flat)."""
        if symbol not in self.positions:
            return None
        running = Decimal("0")
        entry: datetime | None = None
        for o in sorted(self.orders.values(), key=lambda x: x.created_at):
            for f in o.fills:
                if f.symbol != symbol:
                    continue
                if f.side == Side.BUY:
                    if running == 0:
                        entry = f.timestamp
                    running += f.quantity
                else:
                    running -= f.quantity
                    if running <= 0:
                        running, entry = Decimal("0"), None
        return entry

    # ------------------------------------------------------------ state io
    def to_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "currency": self.currency,
            "initial_cash": str(self.initial_cash),
            "cash": str(self.cash),
            "realized_pnl": str(self.realized_pnl),
            "total_commission": str(self.total_commission),
            "total_slippage_cost": str(self.total_slippage_cost),
            "positions": {s: {"qty": str(p["qty"]), "avg_cost": str(p["avg_cost"])} for s, p in self.positions.items()},
            "orders": {
                cid: {
                    **o.to_dict(),
                    "correlation_id": o.correlation_id,
                    "fills": [
                        {
                            "fill_id": f.fill_id,
                            "quantity": str(f.quantity),
                            "price": str(f.price),
                            "commission": str(f.commission),
                            "timestamp": f.timestamp.isoformat(),
                            "broker_fill_id": f.broker_fill_id,
                        }
                        for f in o.fills
                    ],
                }
                for cid, o in self.orders.items()
            },
            "last_session": self._last_session.isoformat() if self._last_session else None,
        }

    def load_dict(self, state: dict[str, Any]) -> None:
        self.account_id = state.get("account_id", self.account_id)
        self.currency = state.get("currency", self.currency)
        self.initial_cash = Decimal(state.get("initial_cash", str(self.initial_cash)))
        self.cash = Decimal(state["cash"])
        self.realized_pnl = Decimal(state.get("realized_pnl", "0"))
        self.total_commission = Decimal(state.get("total_commission", "0"))
        self.total_slippage_cost = Decimal(state.get("total_slippage_cost", "0"))
        self.positions = {
            s: {"qty": Decimal(p["qty"]), "avg_cost": Decimal(p["avg_cost"])} for s, p in state.get("positions", {}).items()
        }
        self.orders = {}
        for cid, od in state.get("orders", {}).items():
            order = Order(
                client_order_id=cid,
                symbol=od["symbol"],
                side=Side(od["side"]),
                quantity=Decimal(od["quantity"]),
                order_type=OrderType(od["order_type"]),
                status=OrderStatus(od["status"]),
                created_at=datetime.fromisoformat(od["created_at"]),
                updated_at=datetime.fromisoformat(od["updated_at"]),
                limit_price=Decimal(od["limit_price"]) if od.get("limit_price") else None,
                stop_price=Decimal(od["stop_price"]) if od.get("stop_price") else None,
                time_in_force=TimeInForce(od.get("time_in_force", "day")),
                broker_order_id=od.get("broker_order_id"),
                filled_quantity=Decimal(od.get("filled_quantity", "0")),
                avg_fill_price=Decimal(od["avg_fill_price"]) if od.get("avg_fill_price") else None,
                commission=Decimal(od.get("commission", "0")),
                strategy=od.get("strategy", ""),
                reason=od.get("reason", ""),
                reject_reason=od.get("reject_reason"),
                correlation_id=od.get("correlation_id", ""),
            )
            for fd in od.get("fills", []):
                order.fills.append(
                    Fill(
                        fill_id=fd["fill_id"],
                        client_order_id=cid,
                        symbol=order.symbol,
                        side=order.side,
                        quantity=Decimal(fd["quantity"]),
                        price=Decimal(fd["price"]),
                        commission=Decimal(fd["commission"]),
                        timestamp=datetime.fromisoformat(fd["timestamp"]),
                        broker_fill_id=fd.get("broker_fill_id"),
                    )
                )
            self.orders[cid] = order
        ls = state.get("last_session")
        self._last_session = date.fromisoformat(ls) if ls else None
