"""Event-driven daily backtester.

Timeline for each session *d*:

 1. **Open**  – orders queued after the previous close are executed at ``open[d]``
                (plus slippage and commission) by the simulated broker.
 2. **Close** – positions are marked at ``close[d]``; the equity curve is recorded.
 3. **Signal** – strategies see bars up to and including *d* (never later), the
                decision engine sizes and risk-checks the trade, and approved orders
                are queued for the next open.

This ordering makes look-ahead impossible by construction: the price a signal is
computed on is never the price it is filled at. Survivorship bias is addressed by
allowing the history to include instruments whose data ends early (delisted); such
positions are force-liquidated at their last available price and flagged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

from trading_agent.broker.simulated import SimulatedBroker
from trading_agent.config import Settings
from trading_agent.decision import DecisionEngine
from trading_agent.domain import Bar, Decision, Fill, Instrument, MarketStatus, OrderRequest, OrderType, Side, money
from trading_agent.logging_config import EventLogger, new_correlation_id
from trading_agent.market_data.calendar import MarketCalendar
from trading_agent.market_data.replay import ReplayMarketDataProvider
from trading_agent.portfolio.manager import PortfolioManager
from trading_agent.portfolio.performance import compute_metrics
from trading_agent.risk.manager import RiskManager
from trading_agent.risk.sizing import PositionSizer
from trading_agent.strategies.base import Strategy

log = EventLogger("backtest")


@dataclass(slots=True)
class BacktestResult:
    equity_curve: list[tuple[date, Decimal]]
    fills: list[Fill]
    decisions: list[Decision]
    metrics: dict[str, Any]
    exposures: list[Decimal]
    concentrations: list[Decimal]
    forced_liquidations: list[dict[str, Any]] = field(default_factory=list)
    vetoes: int = 0
    orders: int = 0

    def summary(self) -> dict[str, Any]:
        return {
            **{k: v for k, v in self.metrics.items() if k not in ("monthly_returns", "drawdown_curve")},
            "decisions": len(self.decisions),
            "orders": self.orders,
            "vetoes": self.vetoes,
            "forced_liquidations": len(self.forced_liquidations),
        }


class BacktestEngine:
    def __init__(
        self,
        settings: Settings,
        strategy: Strategy,
        history: dict[str, list[Bar]],
        calendar: MarketCalendar | None = None,
        instruments: dict[str, Instrument] | None = None,
        initial_cash: Decimal | None = None,
    ) -> None:
        self.s = settings
        self.strategy = strategy
        self.history = history
        self.cal = calendar or MarketCalendar(settings.exchange_calendar)
        self.instruments = instruments or {s: Instrument(symbol=s) for s in history}
        self.data = ReplayMarketDataProvider(history)
        self.broker = SimulatedBroker(
            price_source=self.data.get_quote,
            clock=lambda: self.data.now,
            market_status_fn=lambda at: MarketStatus(is_open=True, timestamp=at, source="backtest"),
            initial_cash=initial_cash if initial_cash is not None else settings.sim_initial_cash,
            currency=settings.sim_currency,
            commission_per_order=settings.sim_commission_per_order,
            commission_pct=settings.sim_commission_pct,
            slippage_bps=settings.sim_slippage_bps,
            partial_fill_probability=settings.sim_partial_fill_probability,
            enforce_market_hours=False,
            instruments=self.instruments,
        )
        self.broker.auto_fill = False
        self.portfolio = PortfolioManager(settings.cash_reserve)
        self.engine = DecisionEngine(
            settings,
            strategy,
            RiskManager(settings),
            PositionSizer(
                settings.max_risk_per_trade,
                settings.max_position_size,
                settings.max_order_value,
                settings.max_portfolio_exposure,
                settings.stop_loss_pct,
                settings.strategy_atr_stop_multiplier,
            ),
        )

    def run(self, start: date | None = None, end: date | None = None) -> BacktestResult:
        start = start or self.data.first_date()
        end = end or self.data.last_date()
        sessions = self.cal.sessions_in_range(start, end)
        equity: list[tuple[date, Decimal]] = []
        fills: list[Fill] = []
        decisions: list[Decision] = []
        exposures: list[Decimal] = []
        concentrations: list[Decimal] = []
        forced: list[dict[str, Any]] = []
        vetoes = orders = 0
        orders_today: dict[str, int] = {}
        for d in sessions:
            new_correlation_id()
            open_t = self.cal.session_open(d)
            close_t = self.cal.session_close(d)
            todays = {s: b for s in self.history if (b := self.data.bar_on(s, d)) is not None}

            # 1. execute queued orders at today's open
            self.data.set_time(open_t, todays, include_today=False)
            fills.extend(self.broker.process_orders(open_t))

            # 2. mark to market at close
            self.data.set_time(close_t, include_today=True)
            forced.extend(self._liquidate_delisted(d, close_t))
            state = self._state(d, close_t)
            equity.append((d, state.portfolio_value))
            exposures.append(state.exposure)
            concentrations.append(max(state.weights.values()) if state.weights else Decimal("0"))

            # 3. generate signals on bars <= d, queue for next open
            orders_today = {}
            for symbol in self.history:
                bars = self.data.get_bars(symbol, limit=max(self.strategy.warmup_bars, 60) * 2)
                if not bars or bars[-1].timestamp.date() != d:
                    continue  # no bar today: halted/delisted -> no new decisions
                quote = self.data.get_quote(symbol)
                assert quote is not None
                decision = self.engine.decide(
                    symbol,
                    bars,
                    quote,
                    state,
                    close_t,
                    self.instruments.get(symbol),
                    trading_enabled=True,
                    breaker_tripped=False,
                    market_open=True,
                    orders_today=sum(orders_today.values()),
                    orders_today_symbol=orders_today.get(symbol, 0),
                    price_reference=bars[-2].close if len(bars) > 1 else None,
                    position_entry_time=self.broker.position_entry_time(symbol),
                )
                decisions.append(decision)
                if decision.decision == "VETOED":
                    vetoes += 1
                    continue
                if decision.approved and decision.order_request is not None:
                    try:
                        self.broker.submit_order(decision.order_request)
                        orders += 1
                        orders_today[symbol] = orders_today.get(symbol, 0) + 1
                        state = self._state(d, close_t)  # reserved cash changed
                    except Exception as exc:  # noqa: BLE001 - rejection is data, not a crash
                        log.warning("backtest_order_rejected", str(exc), symbol=symbol)
        metrics = compute_metrics(equity, fills, exposures, concentrations, self.broker.total_slippage_cost)
        metrics["commissions_total"] = float(self.broker.total_commission)
        metrics["strategy"] = self.strategy.describe()
        return BacktestResult(equity, fills, decisions, metrics, exposures, concentrations, forced, vetoes, orders)

    # ------------------------------------------------------------ helpers
    def _state(self, d: date, now: datetime):
        account = self.broker.get_account()
        positions = self.broker.get_positions()
        open_orders = self.broker.get_orders(open_only=True)
        return self.portfolio.build(
            account, positions, open_orders, d, now, {s: i.sector for s, i in self.instruments.items() if i.sector}
        )

    def _liquidate_delisted(self, d: date, now: datetime) -> list[dict[str, Any]]:
        out = []
        for pos in self.broker.get_positions():
            last_bar = self.history[pos.symbol][-1] if self.history.get(pos.symbol) else None
            if last_bar is not None and last_bar.timestamp.date() < d:
                # Instrument stopped trading: exit at last known close (no data after this point).
                qty = pos.quantity
                px = last_bar.close
                self.broker.cash += money(qty * px)
                del self.broker.positions[pos.symbol]
                for o in self.broker.get_orders(open_only=True):
                    if o.symbol == pos.symbol:
                        self.broker.cancel_order(o.client_order_id)
                out.append(
                    {
                        "date": d.isoformat(),
                        "symbol": pos.symbol,
                        "quantity": str(qty),
                        "price": str(px),
                        "reason": "no further data (delisted/halted)",
                    }
                )
                log.warning("forced_liquidation", symbol=pos.symbol, quantity=str(qty))
        return out


def session_close_utc(cal: MarketCalendar, d: date) -> datetime:
    return cal.session_close(d)


def bars_at_close(bars: list[Bar], close: time) -> list[Bar]:  # pragma: no cover - convenience
    return [
        Bar(b.symbol, b.timestamp.replace(hour=close.hour, minute=close.minute), b.open, b.high, b.low, b.close, b.volume)
        for b in bars
    ]


__all__ = ["BacktestEngine", "BacktestResult", "OrderRequest", "OrderType", "Side"]
