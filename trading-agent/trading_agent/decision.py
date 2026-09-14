"""Decision engine: strategy signal -> risk checks -> position sizing -> explainable Decision.

Shared by the backtester and the live/paper cycle so both paths behave identically.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from trading_agent.config import Settings
from trading_agent.domain import (
    Bar,
    Decision,
    Instrument,
    OrderRequest,
    OrderType,
    Quote,
    RiskCheck,
    Side,
    SignalAction,
    new_id,
)
from trading_agent.features import to_decimal
from trading_agent.logging_config import get_correlation_id
from trading_agent.portfolio.manager import PortfolioState
from trading_agent.risk.manager import RiskContext, RiskManager
from trading_agent.risk.sizing import PositionSizer
from trading_agent.strategies.base import Strategy, StrategyContext


class DecisionEngine:
    def __init__(self, settings: Settings, strategy: Strategy, risk: RiskManager, sizer: PositionSizer) -> None:
        self.s = settings
        self.strategy = strategy
        self.risk = risk
        self.sizer = sizer

    def decide(
        self,
        symbol: str,
        bars: list[Bar],
        quote: Quote,
        state: PortfolioState,
        now: datetime,
        instrument: Instrument | None,
        trading_enabled: bool,
        breaker_tripped: bool,
        market_open: bool,
        orders_today: int,
        orders_today_symbol: int,
        price_reference: Decimal | None = None,
        position_entry_time: datetime | None = None,
    ) -> Decision:
        pos = state.position_for(symbol)
        ctx = StrategyContext(
            symbol=symbol,
            bars=bars,
            now=now,
            position_quantity=pos.quantity if pos else Decimal("0"),
            position_avg_cost=pos.avg_cost if pos else None,
            position_entry_time=position_entry_time,
            params=self.strategy.params,
        )
        signal = self.strategy.evaluate(ctx)
        base: dict[str, Any] = {
            "decision_id": new_id("dec_"),
            "timestamp": now,
            "symbol": symbol,
            "current_price": quote.price,
            "strategy": self.strategy.name,
            "signal_action": signal.action,
            "signal_reason": signal.reason,
            "features": signal.features,
            "portfolio_state": state.summary(),
            "correlation_id": get_correlation_id(),
        }
        if signal.action == SignalAction.HOLD:
            return Decision(
                **base, risk_checks=[], position_size=Decimal("0"), sizing_detail={}, decision="HOLD", reason=signal.reason
            )

        risk_ctx = RiskContext(
            trading_enabled=trading_enabled,
            circuit_breaker_tripped=breaker_tripped,
            market_open=market_open,
            data_age_seconds=quote.age_seconds(now),
            orders_today=orders_today,
            orders_today_symbol=orders_today_symbol,
            instrument=instrument,
            price_reference=price_reference,
            now=now,
        )
        if signal.action == SignalAction.SELL:
            qty = pos.quantity if pos else Decimal("0")
            sizing_detail = {"quantity": qty, "note": "full exit of existing position"}
            if qty <= 0:
                return Decision(
                    **base,
                    risk_checks=[],
                    position_size=Decimal("0"),
                    sizing_detail=sizing_detail,
                    decision="HOLD",
                    reason="sell signal but no position held",
                )
        else:
            atr_key = f"atr_{self.s.strategy_atr_window}"
            sized = self.sizer.size(
                quote.price,
                state,
                to_decimal(signal.features.get(atr_key)),
                existing_quantity=pos.quantity if pos else Decimal("0"),
            )
            qty, sizing_detail = sized.quantity, sized.detail
            if qty <= 0:
                checks = [RiskCheck("position_sizing", False, "sizer produced zero quantity (limits or buying power exhausted)")]
                return Decision(
                    **base,
                    risk_checks=checks,
                    position_size=Decimal("0"),
                    sizing_detail=sizing_detail,
                    decision="VETOED",
                    reason="position size is zero after applying limits",
                )
            if pos and pos.quantity > 0:
                return Decision(
                    **base,
                    risk_checks=[],
                    position_size=Decimal("0"),
                    sizing_detail=sizing_detail,
                    decision="HOLD",
                    reason="already holding position; no pyramiding",
                )

        request = OrderRequest(
            symbol=symbol,
            side=Side.BUY if signal.action == SignalAction.BUY else Side.SELL,
            quantity=qty,
            order_type=OrderType.MARKET,
            strategy=self.strategy.name,
            reason=signal.reason,
            correlation_id=get_correlation_id(),
        )
        checks = self.risk.evaluate(request, quote.price, state, risk_ctx)
        if not RiskManager.all_passed(checks):
            failed = ", ".join(f"{c.name} ({c.detail})" for c in RiskManager.failures(checks))
            return Decision(
                **base,
                risk_checks=checks,
                position_size=qty,
                sizing_detail=sizing_detail,
                decision="VETOED",
                reason=f"risk manager veto: {failed}",
            )
        return Decision(
            **base,
            risk_checks=checks,
            position_size=qty,
            sizing_detail=sizing_detail,
            decision=request.side.value.upper(),
            reason=f"{signal.reason}; all {len(checks)} risk checks passed",
            order_request=request,
        )
