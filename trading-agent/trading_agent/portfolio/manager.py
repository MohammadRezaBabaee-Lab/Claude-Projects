"""Portfolio state and capital accounting.

Capital buckets (all Decimal):

    available_cash   cash reported by the broker
    reserved_cash    cash committed to open buy orders + the configured cash reserve
    invested_capital market value of open positions
    portfolio_value  available_cash + invested_capital
    buying_power     min(broker buying power, available_cash - reserved_cash), never negative

The agent never submits an order larger than ``buying_power``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from trading_agent.domain import Account, Order, Position, Side, money, utcnow


@dataclass(slots=True)
class PortfolioState:
    timestamp: datetime
    currency: str
    available_cash: Decimal
    reserved_cash: Decimal
    invested_capital: Decimal
    portfolio_value: Decimal
    buying_power: Decimal
    positions: list[Position]
    open_orders: list[Order]
    peak_value: Decimal
    day_start_value: Decimal
    initial_value: Decimal
    exposure: Decimal = Decimal("0")
    drawdown: Decimal = Decimal("0")
    daily_pnl: Decimal = Decimal("0")
    total_pnl: Decimal = Decimal("0")
    weights: dict[str, Decimal] = field(default_factory=dict)
    sector_weights: dict[str, Decimal] = field(default_factory=dict)

    def position_for(self, symbol: str) -> Position | None:
        return next((p for p in self.positions if p.symbol == symbol), None)

    def open_orders_for(self, symbol: str, side: Side | None = None) -> list[Order]:
        return [o for o in self.open_orders if o.symbol == symbol and (side is None or o.side == side)]

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "currency": self.currency,
            "available_cash": self.available_cash,
            "reserved_cash": self.reserved_cash,
            "invested_capital": self.invested_capital,
            "portfolio_value": self.portfolio_value,
            "buying_power": self.buying_power,
            "exposure": self.exposure,
            "drawdown": self.drawdown,
            "daily_pnl": self.daily_pnl,
            "total_pnl": self.total_pnl,
            "peak_value": self.peak_value,
            "n_positions": len(self.positions),
            "n_open_orders": len(self.open_orders),
        }

    def summary(self) -> dict[str, Any]:
        d = self.to_dict()
        return {k: (str(v) if isinstance(v, Decimal) else v.isoformat() if isinstance(v, datetime) else v) for k, v in d.items()}


class PortfolioManager:
    """Builds :class:`PortfolioState` from broker data and tracks peak / day-start values."""

    STATE_KEY = "portfolio_tracking"

    def __init__(self, cash_reserve_fraction: Decimal, state: dict[str, Any] | None = None) -> None:
        self.cash_reserve_fraction = Decimal(cash_reserve_fraction)
        self.peak_value: Decimal | None = None
        self.initial_value: Decimal | None = None
        self.day_start_value: Decimal | None = None
        self.day: date | None = None
        if state:
            self.load(state)

    # -- persistence ------------------------------------------------------
    def dump(self) -> dict[str, Any]:
        return {
            "peak_value": str(self.peak_value) if self.peak_value is not None else None,
            "initial_value": str(self.initial_value) if self.initial_value is not None else None,
            "day_start_value": str(self.day_start_value) if self.day_start_value is not None else None,
            "day": self.day.isoformat() if self.day else None,
        }

    def load(self, state: dict[str, Any]) -> None:
        self.peak_value = Decimal(state["peak_value"]) if state.get("peak_value") else None
        self.initial_value = Decimal(state["initial_value"]) if state.get("initial_value") else None
        self.day_start_value = Decimal(state["day_start_value"]) if state.get("day_start_value") else None
        self.day = date.fromisoformat(state["day"]) if state.get("day") else None

    # -- computation -------------------------------------------------------
    def build(
        self,
        account: Account,
        positions: list[Position],
        open_orders: list[Order],
        trading_day: date,
        now: datetime | None = None,
        sectors: dict[str, str] | None = None,
    ) -> PortfolioState:
        now = now or utcnow()
        invested = money(sum((p.market_value for p in positions), Decimal("0")))
        portfolio_value = money(account.cash + invested)
        committed = Decimal("0")
        for o in open_orders:
            if o.side == Side.BUY and o.is_open:
                ref = o.limit_price or o.avg_fill_price
                if ref is None:
                    pos = next((p for p in positions if p.symbol == o.symbol), None)
                    ref = pos.current_price if pos else Decimal("0")
                committed += o.remaining_quantity * ref
        reserve = money(portfolio_value * self.cash_reserve_fraction)
        reserved_cash = money(committed + reserve)
        buying_power = money(max(min(account.buying_power, account.cash - reserved_cash), Decimal("0")))

        if self.initial_value is None:
            self.initial_value = portfolio_value
        if self.peak_value is None or portfolio_value > self.peak_value:
            self.peak_value = portfolio_value
        if self.day != trading_day or self.day_start_value is None:
            self.day = trading_day
            self.day_start_value = portfolio_value

        exposure = (invested / portfolio_value).quantize(Decimal("0.0001")) if portfolio_value > 0 else Decimal("0")
        drawdown = (
            ((self.peak_value - portfolio_value) / self.peak_value).quantize(Decimal("0.0001"))
            if self.peak_value > 0
            else Decimal("0")
        )
        weights = {
            p.symbol: (p.market_value / portfolio_value).quantize(Decimal("0.0001")) if portfolio_value > 0 else Decimal("0")
            for p in positions
        }
        sector_weights: dict[str, Decimal] = {}
        for p in positions:
            sector = (sectors or {}).get(p.symbol) or p.sector
            if sector:
                sector_weights[sector] = sector_weights.get(sector, Decimal("0")) + weights[p.symbol]
        return PortfolioState(
            timestamp=now,
            currency=account.currency,
            available_cash=money(account.cash),
            reserved_cash=reserved_cash,
            invested_capital=invested,
            portfolio_value=portfolio_value,
            buying_power=buying_power,
            positions=positions,
            open_orders=[o for o in open_orders if o.is_open],
            peak_value=self.peak_value,
            day_start_value=self.day_start_value,
            initial_value=self.initial_value,
            exposure=exposure,
            drawdown=drawdown,
            daily_pnl=money(portfolio_value - self.day_start_value),
            total_pnl=money(portfolio_value - self.initial_value),
            weights=weights,
            sector_weights=sector_weights,
        )
