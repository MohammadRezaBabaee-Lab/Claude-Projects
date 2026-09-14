"""Volatility-based position sizing.

quantity = floor( risk_budget / stop_distance ) where
    risk_budget   = portfolio_value * MAX_RISK_PER_TRADE
    stop_distance = max(k * ATR, STOP_LOSS_PCT * price)   (per share)

and then capped by MAX_POSITION_SIZE, MAX_ORDER_VALUE, exposure headroom and
buying power. The sizer never returns more than the account can pay for.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from trading_agent.domain import money, whole_shares
from trading_agent.portfolio.manager import PortfolioState


@dataclass(slots=True)
class SizingResult:
    quantity: Decimal
    detail: dict[str, Any] = field(default_factory=dict)


class PositionSizer:
    def __init__(
        self,
        max_risk_per_trade: Decimal,
        max_position_size: Decimal,
        max_order_value: Decimal,
        max_portfolio_exposure: Decimal,
        stop_loss_pct: Decimal,
        atr_stop_multiplier: Decimal,
    ) -> None:
        self.max_risk_per_trade = Decimal(max_risk_per_trade)
        self.max_position_size = Decimal(max_position_size)
        self.max_order_value = Decimal(max_order_value)
        self.max_portfolio_exposure = Decimal(max_portfolio_exposure)
        self.stop_loss_pct = Decimal(stop_loss_pct)
        self.k = Decimal(atr_stop_multiplier)

    def size(
        self, price: Decimal, state: PortfolioState, atr: Decimal | None, existing_quantity: Decimal = Decimal("0")
    ) -> SizingResult:
        if price <= 0:
            return SizingResult(Decimal("0"), {"error": "non-positive price"})
        pv = state.portfolio_value
        risk_budget = money(pv * self.max_risk_per_trade)
        pct_stop = price * self.stop_loss_pct
        atr_stop = self.k * atr if atr is not None and atr > 0 else Decimal("0")
        stop_distance = max(atr_stop, pct_stop)
        raw_qty = whole_shares(risk_budget / stop_distance) if stop_distance > 0 else Decimal("0")

        max_pos_value = money(pv * self.max_position_size) - money(existing_quantity * price)
        cap_position = whole_shares(max(max_pos_value, Decimal("0")) / price)
        cap_order_value = whole_shares(self.max_order_value / price)
        headroom = money(pv * self.max_portfolio_exposure - state.invested_capital)
        cap_exposure = whole_shares(max(headroom, Decimal("0")) / price)
        cap_buying_power = whole_shares(max(state.buying_power, Decimal("0")) / price)
        qty = min(raw_qty, cap_position, cap_order_value, cap_exposure, cap_buying_power)
        qty = max(qty, Decimal("0"))
        detail = {
            "portfolio_value": pv,
            "risk_budget": risk_budget,
            "atr": atr,
            "atr_stop_distance": money(atr_stop),
            "pct_stop_distance": money(pct_stop),
            "stop_distance_used": money(stop_distance),
            "raw_quantity": raw_qty,
            "cap_position_size": cap_position,
            "cap_order_value": cap_order_value,
            "cap_exposure_headroom": cap_exposure,
            "cap_buying_power": cap_buying_power,
            "quantity": qty,
            "order_value": money(qty * price),
        }
        return SizingResult(qty, detail)
