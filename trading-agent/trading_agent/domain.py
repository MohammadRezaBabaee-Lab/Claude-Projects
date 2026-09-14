"""Core domain types shared by every layer.

All monetary values are :class:`decimal.Decimal`. Indicator values computed by the
feature layer may be floats (they are statistics, not money) but anything that
becomes a quantity, price or cash amount is converted to Decimal before it is used.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Any

MONEY_Q = Decimal("0.01")
PRICE_Q = Decimal("0.0001")
QTY_Q = Decimal("1")


def money(value: Decimal | int | float | str) -> Decimal:
    return Decimal(str(value)).quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def price(value: Decimal | int | float | str) -> Decimal:
    return Decimal(str(value)).quantize(PRICE_Q, rounding=ROUND_HALF_UP)


def whole_shares(value: Decimal | int | float | str) -> Decimal:
    return Decimal(str(value)).quantize(QTY_Q, rounding=ROUND_DOWN)


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex}"


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"


class TimeInForce(str, Enum):
    DAY = "day"
    GTC = "gtc"


class OrderStatus(str, Enum):
    NEW = "new"  # created internally, not yet sent
    PENDING_SUBMIT = "pending_submit"  # persisted, submission in flight
    SUBMITTED = "submitted"  # broker acknowledged receipt
    ACCEPTED = "accepted"  # broker accepted/working
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    FAILED = "failed"  # submission failed (network etc.) and confirmed not at broker
    EXPIRED = "expired"
    UNKNOWN = "unknown"  # submission outcome unknown; must reconcile before retry

    @property
    def is_terminal(self) -> bool:
        return self in {
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.FAILED,
            OrderStatus.EXPIRED,
        }

    @property
    def is_open(self) -> bool:
        return self in {
            OrderStatus.PENDING_SUBMIT,
            OrderStatus.SUBMITTED,
            OrderStatus.ACCEPTED,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.UNKNOWN,
        }


class SignalAction(str, Enum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


class AssetClass(str, Enum):
    EQUITY = "equity"
    ETF = "etf"
    OTHER = "other"


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Bar:
    symbol: str
    timestamp: datetime  # bar *close* time, timezone-aware UTC
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    def validate(self) -> None:
        if self.timestamp.tzinfo is None:
            raise ValueError(f"{self.symbol}: bar timestamp must be timezone-aware")
        for name in ("open", "high", "low", "close"):
            v = getattr(self, name)
            if not isinstance(v, Decimal) or not v.is_finite() or v <= 0:
                raise ValueError(f"{self.symbol}: invalid {name}={v!r}")
        if self.volume < 0:
            raise ValueError(f"{self.symbol}: negative volume")
        if not (self.low <= self.open <= self.high and self.low <= self.close <= self.high):
            raise ValueError(f"{self.symbol}: OHLC inconsistent at {self.timestamp}")


@dataclass(frozen=True, slots=True)
class Quote:
    symbol: str
    price: Decimal
    timestamp: datetime
    bid: Decimal | None = None
    ask: Decimal | None = None

    def age_seconds(self, now: datetime | None = None) -> float:
        return ((now or utcnow()) - self.timestamp).total_seconds()


@dataclass(frozen=True, slots=True)
class MarketStatus:
    is_open: bool
    timestamp: datetime
    next_open: datetime | None = None
    next_close: datetime | None = None
    source: str = ""


@dataclass(frozen=True, slots=True)
class Instrument:
    symbol: str
    name: str = ""
    exchange: str = ""
    currency: str = "USD"
    asset_class: AssetClass = AssetClass.EQUITY
    sector: str | None = None
    tradable: bool = True
    fractionable: bool = False
    leveraged: bool = False
    derivative: bool = False


# ---------------------------------------------------------------------------
# Account / portfolio
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Account:
    account_id: str
    currency: str
    cash: Decimal
    buying_power: Decimal
    portfolio_value: Decimal
    equity: Decimal
    timestamp: datetime
    multiplier: Decimal = Decimal("1")  # >1 means margin enabled at broker
    status: str = "active"
    is_paper: bool = True


@dataclass(slots=True)
class Position:
    symbol: str
    quantity: Decimal
    avg_cost: Decimal
    current_price: Decimal
    timestamp: datetime
    sector: str | None = None

    @property
    def market_value(self) -> Decimal:
        return money(self.quantity * self.current_price)

    @property
    def cost_basis(self) -> Decimal:
        return money(self.quantity * self.avg_cost)

    @property
    def unrealized_pnl(self) -> Decimal:
        return money(self.market_value - self.cost_basis)


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class OrderRequest:
    symbol: str
    side: Side
    quantity: Decimal
    order_type: OrderType = OrderType.MARKET
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    time_in_force: TimeInForce = TimeInForce.DAY
    client_order_id: str = field(default_factory=lambda: new_id("ord_"))
    strategy: str = ""
    reason: str = ""
    correlation_id: str = ""

    def validate(self) -> None:
        if not self.symbol or not self.symbol.isascii():
            raise ValueError("invalid symbol")
        if self.quantity <= 0 or self.quantity != self.quantity.to_integral_value():
            raise ValueError("quantity must be a positive whole number of shares")
        if self.order_type in (OrderType.LIMIT, OrderType.STOP_LIMIT) and (self.limit_price is None or self.limit_price <= 0):
            raise ValueError("limit orders require a positive limit price")
        if self.order_type in (OrderType.STOP, OrderType.STOP_LIMIT) and (self.stop_price is None or self.stop_price <= 0):
            raise ValueError("stop orders require a positive stop price")
        if len(self.client_order_id) > 48:
            raise ValueError("client_order_id too long")


@dataclass(frozen=True, slots=True)
class Fill:
    fill_id: str
    client_order_id: str
    symbol: str
    side: Side
    quantity: Decimal
    price: Decimal
    commission: Decimal
    timestamp: datetime
    broker_fill_id: str | None = None


@dataclass(slots=True)
class Order:
    client_order_id: str
    symbol: str
    side: Side
    quantity: Decimal
    order_type: OrderType
    status: OrderStatus
    created_at: datetime
    updated_at: datetime
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    time_in_force: TimeInForce = TimeInForce.DAY
    broker_order_id: str | None = None
    filled_quantity: Decimal = Decimal("0")
    avg_fill_price: Decimal | None = None
    commission: Decimal = Decimal("0")
    strategy: str = ""
    reason: str = ""
    reject_reason: str | None = None
    fills: list[Fill] = field(default_factory=list)
    correlation_id: str = ""

    @property
    def remaining_quantity(self) -> Decimal:
        return self.quantity - self.filled_quantity

    @property
    def is_open(self) -> bool:
        return self.status.is_open

    def to_dict(self) -> dict[str, Any]:
        return {
            "client_order_id": self.client_order_id,
            "broker_order_id": self.broker_order_id,
            "symbol": self.symbol,
            "side": self.side.value,
            "quantity": str(self.quantity),
            "order_type": self.order_type.value,
            "limit_price": str(self.limit_price) if self.limit_price is not None else None,
            "stop_price": str(self.stop_price) if self.stop_price is not None else None,
            "time_in_force": self.time_in_force.value,
            "status": self.status.value,
            "filled_quantity": str(self.filled_quantity),
            "avg_fill_price": str(self.avg_fill_price) if self.avg_fill_price is not None else None,
            "commission": str(self.commission),
            "strategy": self.strategy,
            "reason": self.reason,
            "reject_reason": self.reject_reason,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


# ---------------------------------------------------------------------------
# Signals / decisions
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Signal:
    symbol: str
    action: SignalAction
    timestamp: datetime
    strategy: str
    strength: float = 0.0  # 0..1 conviction, informational
    features: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    stop_price: Decimal | None = None  # strategy-suggested protective stop
    reference_price: Decimal | None = None


@dataclass(frozen=True, slots=True)
class RiskCheck:
    name: str
    passed: bool
    detail: str
    observed: str | None = None
    limit: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "detail": self.detail,
            "observed": self.observed,
            "limit": self.limit,
        }


@dataclass(slots=True)
class Decision:
    """A fully explainable BUY/SELL/HOLD decision."""

    decision_id: str
    timestamp: datetime
    symbol: str
    current_price: Decimal | None
    strategy: str
    signal_action: SignalAction
    signal_reason: str
    features: dict[str, Any]
    portfolio_state: dict[str, Any]
    risk_checks: list[RiskCheck]
    position_size: Decimal
    sizing_detail: dict[str, Any]
    decision: str  # BUY / SELL / HOLD / VETOED
    reason: str
    order_request: OrderRequest | None = None
    correlation_id: str = ""

    @property
    def approved(self) -> bool:
        return self.decision in ("BUY", "SELL") and self.order_request is not None

    def explanation(self) -> str:
        lines = [f"{self.decision} {self.symbol}", "", "Signal:", f"  {self.signal_reason}", "", "Risk:"]
        for chk in self.risk_checks:
            status = "PASS" if chk.passed else "FAIL"
            lines.append(f"  [{status}] {chk.name}: {chk.detail}")
        lines += ["", "Position sizing:"]
        for k, v in self.sizing_detail.items():
            lines.append(f"  {k} = {v}")
        lines += [
            "",
            f"Decision: {self.decision} {self.position_size} shares" if self.position_size else f"Decision: {self.decision}",
            "",
            f"Reason: {self.reason}",
        ]
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "timestamp": self.timestamp.isoformat(),
            "symbol": self.symbol,
            "current_price": str(self.current_price) if self.current_price is not None else None,
            "strategy": self.strategy,
            "signal_action": self.signal_action.value,
            "signal_reason": self.signal_reason,
            "features": {k: (str(v) if isinstance(v, Decimal) else v) for k, v in self.features.items()},
            "portfolio_state": self.portfolio_state,
            "risk_checks": [c.to_dict() for c in self.risk_checks],
            "position_size": str(self.position_size),
            "sizing_detail": {k: str(v) for k, v in self.sizing_detail.items()},
            "decision": self.decision,
            "reason": self.reason,
            "client_order_id": self.order_request.client_order_id if self.order_request else None,
            "correlation_id": self.correlation_id,
        }
