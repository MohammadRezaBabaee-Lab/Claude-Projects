"""Independent risk manager. It can veto any order the strategy proposes.

Every check is recorded as a :class:`RiskCheck` so decisions are explainable; if
*any* check fails the order must not be placed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from trading_agent.config import Settings
from trading_agent.domain import Instrument, OrderRequest, RiskCheck, Side, money
from trading_agent.portfolio.manager import PortfolioState


@dataclass(slots=True)
class RiskContext:
    trading_enabled: bool
    circuit_breaker_tripped: bool
    market_open: bool
    data_age_seconds: float | None
    orders_today: int
    orders_today_symbol: int
    instrument: Instrument | None
    price_reference: Decimal | None = None  # e.g. previous close, for abnormal-move detection
    now: datetime | None = None


class RiskManager:
    def __init__(self, settings: Settings) -> None:
        self.s = settings

    def evaluate(self, request: OrderRequest, price: Decimal, state: PortfolioState, ctx: RiskContext) -> list[RiskCheck]:
        s = self.s
        checks: list[RiskCheck] = []

        def add(name: str, passed: bool, detail: str, observed=None, limit=None) -> None:
            checks.append(
                RiskCheck(
                    name, passed, detail, None if observed is None else str(observed), None if limit is None else str(limit)
                )
            )

        # --- global gates --------------------------------------------------
        add("kill_switch", ctx.trading_enabled, "TRADING_ENABLED must be true" if not ctx.trading_enabled else "trading enabled")
        add(
            "circuit_breaker",
            not ctx.circuit_breaker_tripped,
            "circuit breaker is tripped" if ctx.circuit_breaker_tripped else "circuit breaker closed",
        )
        add("market_open", ctx.market_open, "market is open" if ctx.market_open else "market is closed")
        if ctx.data_age_seconds is None:
            add("data_freshness", False, "no market data timestamp available")
        else:
            add(
                "data_freshness",
                ctx.data_age_seconds <= s.max_data_age_seconds,
                f"data age {ctx.data_age_seconds:.0f}s",
                f"{ctx.data_age_seconds:.0f}s",
                f"{s.max_data_age_seconds}s",
            )
        if ctx.price_reference is not None and ctx.price_reference > 0:
            move = abs(price - ctx.price_reference) / ctx.price_reference
            add(
                "abnormal_price",
                move <= s.abnormal_price_move_pct,
                f"price moved {move:.2%} vs reference",
                f"{move:.4f}",
                str(s.abnormal_price_move_pct),
            )

        # --- instrument --------------------------------------------------
        inst = ctx.instrument
        add(
            "symbol_tradable",
            bool(inst and inst.tradable),
            "instrument tradable" if inst and inst.tradable else "instrument not tradable/unknown",
        )
        if inst is not None:
            add(
                "no_derivatives",
                not inst.derivative or s.allow_derivatives,
                "derivative instruments are disabled" if inst.derivative else "not a derivative",
            )
            add(
                "no_leveraged_etfs",
                not inst.leveraged or s.allow_leveraged_etfs,
                "leveraged ETFs are disabled" if inst.leveraged else "not leveraged",
            )

        # --- order sanity ------------------------------------------------
        order_value = money(request.quantity * price)
        add("positive_quantity", request.quantity > 0, f"quantity {request.quantity}")
        add("max_order_value", order_value <= s.max_order_value, f"order value {order_value}", order_value, s.max_order_value)
        add(
            "max_orders_per_day",
            ctx.orders_today < s.max_orders_per_day,
            f"{ctx.orders_today} orders today",
            ctx.orders_today,
            s.max_orders_per_day,
        )
        add(
            "max_orders_per_symbol_per_day",
            ctx.orders_today_symbol < s.max_orders_per_symbol_per_day,
            f"{ctx.orders_today_symbol} orders for {request.symbol} today",
            ctx.orders_today_symbol,
            s.max_orders_per_symbol_per_day,
        )
        dup = state.open_orders_for(request.symbol, request.side)
        add(
            "no_equivalent_open_order",
            not dup,
            f"{len(dup)} open {request.side.value} order(s) for {request.symbol}" if dup else "no equivalent open order",
        )

        # --- portfolio-level limits ----------------------------------------
        pv = state.portfolio_value
        add(
            "max_daily_loss",
            state.daily_pnl >= -(pv * s.max_daily_loss) if pv > 0 else False,
            f"daily P&L {state.daily_pnl}",
            state.daily_pnl,
            money(-(pv * s.max_daily_loss)),
        )
        add("max_drawdown", state.drawdown <= s.max_drawdown, f"drawdown {state.drawdown:.2%}", state.drawdown, s.max_drawdown)

        if request.side == Side.BUY:
            existing = state.position_for(request.symbol)
            existing_value = existing.market_value if existing else Decimal("0")
            new_invested = state.invested_capital + order_value
            exposure_after = (new_invested / pv) if pv > 0 else Decimal("1")
            add(
                "max_portfolio_exposure",
                exposure_after <= s.max_portfolio_exposure,
                f"exposure after trade {exposure_after:.2%}",
                f"{exposure_after:.4f}",
                str(s.max_portfolio_exposure),
            )
            pos_after = (existing_value + order_value) / pv if pv > 0 else Decimal("1")
            add(
                "max_position_size",
                pos_after <= s.max_position_size,
                f"position weight after trade {pos_after:.2%}",
                f"{pos_after:.4f}",
                str(s.max_position_size),
            )
            risk_share = order_value / pv if pv > 0 else Decimal("1")
            add(
                "max_capital_per_trade",
                risk_share <= s.max_position_size,
                f"trade is {risk_share:.2%} of capital",
                f"{risk_share:.4f}",
                str(s.max_position_size),
            )
            n_positions = len([p for p in state.positions if p.quantity > 0]) + (0 if existing else 1)
            add(
                "max_open_positions",
                n_positions <= s.max_open_positions,
                f"{n_positions} positions after trade",
                n_positions,
                s.max_open_positions,
            )
            add(
                "buying_power",
                order_value <= state.buying_power,
                f"order value {order_value} vs buying power {state.buying_power}",
                order_value,
                state.buying_power,
            )
            cash_after = state.available_cash - order_value
            add(
                "cash_reserve",
                cash_after >= pv * s.cash_reserve,
                f"cash after trade {money(cash_after)}",
                money(cash_after),
                money(pv * s.cash_reserve),
            )
            add(
                "no_margin",
                s.allow_margin or order_value <= state.available_cash,
                "margin disabled: order must be fully cash funded",
            )
            sector = inst.sector if inst else None
            if sector:
                sector_after = state.sector_weights.get(sector, Decimal("0")) + (order_value / pv if pv > 0 else Decimal("1"))
                add(
                    "sector_concentration",
                    sector_after <= s.max_sector_concentration,
                    f"{sector} weight after trade {sector_after:.2%}",
                    f"{sector_after:.4f}",
                    str(s.max_sector_concentration),
                )
            else:
                add("sector_concentration", True, "no sector data; check skipped")
        else:
            existing = state.position_for(request.symbol)
            held = existing.quantity if existing else Decimal("0")
            committed = sum((o.remaining_quantity for o in state.open_orders_for(request.symbol, Side.SELL)), Decimal("0"))
            ok = s.allow_short_selling or request.quantity <= held - committed
            add(
                "no_short_selling",
                ok,
                f"selling {request.quantity} of {held} held ({committed} committed)",
                request.quantity,
                held - committed,
            )
        return checks

    @staticmethod
    def all_passed(checks: list[RiskCheck]) -> bool:
        return all(c.passed for c in checks)

    @staticmethod
    def failures(checks: list[RiskCheck]) -> list[RiskCheck]:
        return [c for c in checks if not c.passed]
