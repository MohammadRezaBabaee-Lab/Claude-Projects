"""One trading cycle:

 1. check market status          6. run risk checks (inside the decision engine)
 2. fetch current data           7. submit permitted orders
 3. update portfolio state       8. reconcile broker state
 4. run strategies               9. record results / snapshots
 5. generate signals            10. send notifications

Monitoring steps (1-3, 8-10) always run, even when the kill switch is off or the
market is closed; only order submission is gated.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from trading_agent.broker.base import Broker, BrokerAuthError, BrokerError
from trading_agent.config import Settings
from trading_agent.db.repository import Repository
from trading_agent.decision import DecisionEngine
from trading_agent.domain import Bar, Decision, Quote, SignalAction, new_id, utcnow
from trading_agent.execution.order_manager import OrderManager
from trading_agent.execution.reconciliation import Reconciler
from trading_agent.logging_config import EventLogger, new_correlation_id
from trading_agent.market_data.base import MalformedDataError, MarketDataError, MarketDataProvider, StaleDataError, ensure_fresh
from trading_agent.market_data.calendar import MarketCalendar
from trading_agent.market_data.validation import validate_bars, validate_quote
from trading_agent.metrics import Metrics
from trading_agent.notifications.base import Notifier
from trading_agent.portfolio.manager import PortfolioManager, PortfolioState
from trading_agent.risk.circuit_breaker import CircuitBreaker

log = EventLogger("scheduler.cycle")


@dataclass(slots=True)
class CycleReport:
    run_id: str
    started_at: datetime
    market_open: bool = False
    broker_connected: bool = False
    data_ok: bool = False
    symbols_evaluated: int = 0
    signals: int = 0
    decisions: list[Decision] = field(default_factory=list)
    orders_submitted: int = 0
    orders_vetoed: int = 0
    errors: list[str] = field(default_factory=list)
    portfolio: dict[str, Any] | None = None
    latency_ms: int = 0
    last_data_update: datetime | None = None
    reconciliation: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "started_at": self.started_at.isoformat(),
            "market_open": self.market_open,
            "broker_connected": self.broker_connected,
            "data_ok": self.data_ok,
            "symbols_evaluated": self.symbols_evaluated,
            "signals": self.signals,
            "orders_submitted": self.orders_submitted,
            "orders_vetoed": self.orders_vetoed,
            "errors": self.errors,
            "portfolio": self.portfolio,
            "latency_ms": self.latency_ms,
            "last_data_update": self.last_data_update.isoformat() if self.last_data_update else None,
            "reconciliation": self.reconciliation,
        }


class TradingCycle:
    def __init__(
        self,
        settings: Settings,
        broker: Broker,
        data: MarketDataProvider,
        calendar: MarketCalendar,
        repo: Repository,
        engine: DecisionEngine,
        oms: OrderManager,
        reconciler: Reconciler,
        portfolio: PortfolioManager,
        breaker: CircuitBreaker,
        notifier: Notifier,
        metrics: Metrics,
    ) -> None:
        self.s = settings
        self.broker = broker
        self.data = data
        self.cal = calendar
        self.repo = repo
        self.engine = engine
        self.oms = oms
        self.reconciler = reconciler
        self.portfolio = portfolio
        self.breaker = breaker
        self.notifier = notifier
        self.metrics = metrics
        self.mode = settings.trading_mode.value
        self._last_drawdown_alert: Decimal = Decimal("0")

    # ------------------------------------------------------------------
    def run_once(self, now: datetime | None = None) -> CycleReport:
        now = now or utcnow()
        cid = new_correlation_id()
        run_id = new_id("run_")
        report = CycleReport(run_id=run_id, started_at=now)
        started = time.perf_counter()
        self.repo.start_run(run_id, cid, self.engine.strategy.name, self.mode)
        self.metrics.inc("cycles")
        try:
            self._run(report, now)
            status = "ok" if not report.errors else "degraded"
        except Exception as exc:  # noqa: BLE001 - a cycle must never take the process down
            report.errors.append(f"{exc.__class__.__name__}: {exc}")
            log.exception("cycle_failed")
            self.metrics.inc("errors")
            self.breaker.record_api_error(now)
            self.repo.record_system_event("cycle", "cycle_failed", "ERROR", str(exc), cid)
            self.notifier.notify("strategy_error", "Trading cycle failed", f"{exc.__class__.__name__}: {exc}", "ERROR")
            status = "failed"
        report.latency_ms = int((time.perf_counter() - started) * 1000)
        self.metrics.observe("cycle_latency_ms", report.latency_ms)
        self.repo.finish_run(
            run_id,
            status,
            "; ".join(report.errors) or None,
            symbols_evaluated=report.symbols_evaluated,
            signals_generated=report.signals,
            orders_submitted=report.orders_submitted,
            orders_vetoed=report.orders_vetoed,
        )
        self._publish_status(report)
        return report

    # ------------------------------------------------------------------
    def _run(self, report: CycleReport, now: datetime) -> None:
        s = self.s
        # 1. market status (calendar is authoritative; broker clock must agree to trade)
        cal_open = self.cal.is_open(now)
        broker_open = cal_open
        try:
            broker_status = self.broker.get_market_status()
            broker_open = broker_status.is_open
            report.broker_connected = True
        except BrokerAuthError as exc:
            self.breaker.record_auth_failure(now)
            report.errors.append(f"broker auth: {exc}")
            self.notifier.notify("broker_connection_failure", "Broker authentication failed", str(exc), "ERROR")
        except BrokerError as exc:
            self.breaker.record_connectivity_failure(now)
            report.errors.append(f"broker: {exc}")
            self.notifier.notify("broker_connection_failure", "Broker unreachable", str(exc), "ERROR")
        report.market_open = cal_open and broker_open
        self.metrics.set("market_open", 1 if report.market_open else 0)
        if not report.broker_connected:
            return

        # 3. portfolio state (before data so monitoring works even if data fails)
        trading_day = self.cal.local_day(now)
        try:
            self.oms.track_open_orders()
            state = self._portfolio_state(trading_day, now)
        except BrokerAuthError as exc:
            self.breaker.record_auth_failure(now)
            report.broker_connected = False
            report.errors.append(f"broker auth: {exc}")
            self.notifier.notify("broker_connection_failure", "Broker authentication failed", str(exc), "ERROR")
            return
        except BrokerError as exc:
            self.breaker.record_connectivity_failure(now)
            report.broker_connected = False
            report.errors.append(f"broker: {exc}")
            self.notifier.notify("broker_connection_failure", "Broker unreachable", str(exc), "ERROR")
            return
        report.portfolio = state.summary()
        self._check_portfolio_health(state, now)

        # 2. market data
        bars_by_symbol: dict[str, list[Bar]] = {}
        quotes: dict[str, Quote] = {}
        for symbol in s.universe:
            try:
                bars = validate_bars(self.data.get_bars(symbol, limit=max(self.engine.strategy.warmup_bars, 60) * 2))
                quote = self.data.get_quote(symbol)
                if quote is None:
                    raise MarketDataError(f"{symbol}: no quote")
                validate_quote(quote)
                ensure_fresh(quote, s.max_data_age_seconds, now)
                if bars:
                    ref = bars[-1].close
                    move = abs(quote.price - ref) / ref if ref > 0 else Decimal("1")
                    if move > s.abnormal_price_move_pct:
                        self.breaker.record_abnormal_price(symbol, f"{move:.1%} vs last close {ref}")
                        raise MalformedDataError(f"{symbol}: abnormal move {move:.1%} vs last close")
                bars_by_symbol[symbol] = bars
                quotes[symbol] = quote
            except StaleDataError as exc:
                report.errors.append(str(exc))
                self.metrics.inc("stale_data")
                log.warning("stale_market_data", str(exc), symbol=symbol)
            except (MarketDataError, ValueError) as exc:
                report.errors.append(f"{symbol}: {exc}")
                self.metrics.inc("data_errors")
                log.warning("market_data_error", str(exc), symbol=symbol)
        report.data_ok = bool(quotes)
        report.last_data_update = self.data.last_update
        if self.data.last_update:
            self.metrics.set("data_age_seconds", (now - self.data.last_update).total_seconds())

        # 4-7. strategy -> decision -> submit
        day_start = self.repo.day_start(now)
        orders_today = self.repo.count_orders_since(day_start)
        for symbol, bars in bars_by_symbol.items():
            quote = quotes[symbol]
            instrument = None
            try:
                instrument = self.broker.get_instrument(symbol)
            except BrokerError as exc:
                report.errors.append(f"{symbol}: instrument lookup failed: {exc}")
            if instrument is not None:
                self.repo.upsert_instrument(instrument)
            t0 = time.perf_counter()
            decision = self.engine.decide(
                symbol,
                bars,
                quote,
                state,
                now,
                instrument,
                trading_enabled=s.trading_enabled,
                breaker_tripped=self.breaker.tripped,
                market_open=report.market_open,
                orders_today=orders_today,
                orders_today_symbol=self.repo.count_orders_since(day_start, symbol),
                price_reference=bars[-1].close if bars else None,
                position_entry_time=self._entry_time(symbol),
            )
            self.metrics.observe("strategy_latency_ms", (time.perf_counter() - t0) * 1000)
            report.symbols_evaluated += 1
            report.decisions.append(decision)
            self.repo.record_decision(decision, report.run_id)
            if decision.signal_action != SignalAction.HOLD:
                report.signals += 1
            if decision.decision == "VETOED":
                report.orders_vetoed += 1
                self.repo.record_risk_event("risk_veto", "WARNING", decision.reason, decision.correlation_id, symbol)
                continue
            if decision.approved:
                result = self.oms.submit(decision, state, report.market_open, trading_day)
                if result.submitted:
                    report.orders_submitted += 1
                    orders_today += 1
                    state = self._portfolio_state(trading_day, now)
                elif result.order is None:
                    report.orders_vetoed += 1

        # 8. reconcile
        report.reconciliation = self.reconciler.run()
        # 9. record results
        state = self._portfolio_state(trading_day, now)
        report.portfolio = state.summary()
        self.repo.record_snapshot(state.to_dict(), state.positions, self.mode)
        self.metrics.set("portfolio_value", float(state.portfolio_value))
        self.metrics.set("exposure", float(state.exposure))
        self.metrics.set("drawdown", float(state.drawdown))
        self.metrics.set("daily_pnl", float(state.daily_pnl))
        self.metrics.set("total_pnl", float(state.total_pnl))
        self.metrics.set("circuit_breaker_tripped", 1 if self.breaker.tripped else 0)

    # ------------------------------------------------------------------
    def _portfolio_state(self, trading_day, now: datetime) -> PortfolioState:
        account = self.broker.get_account()
        positions = self.broker.get_positions()
        self.repo.record_account(account, self.broker.name, self.mode)
        if account.multiplier > 1 and not self.s.allow_margin:
            log.warning(
                "margin_account_detected",
                "broker account has margin enabled; the agent will only use cash",
                multiplier=str(account.multiplier),
            )
        sectors = {p.symbol: sec for p in positions if (sec := self.repo.get_instrument_sector(p.symbol))}
        return self.portfolio.build(account, positions, self.repo.open_orders(), trading_day, now, sectors)

    def _entry_time(self, symbol: str) -> datetime | None:
        """Entry time of the current lot from recorded fills (None when flat or unknown)."""
        running = Decimal("0")
        entry: datetime | None = None
        for f in sorted(self.repo.fills(limit=2000), key=lambda x: x.timestamp):
            if f.symbol != symbol:
                continue
            if f.side.value == "buy":
                if running == 0:
                    entry = f.timestamp
                running += f.quantity
            else:
                running -= f.quantity
                if running <= 0:
                    running, entry = Decimal("0"), None
        return entry

    def _check_portfolio_health(self, state: PortfolioState, now: datetime) -> None:
        s = self.s
        pv = state.portfolio_value
        if pv > 0 and state.daily_pnl < -(pv * s.max_daily_loss):
            self.breaker.record_abnormal_loss(f"daily P&L {state.daily_pnl} exceeds {s.max_daily_loss:.1%} of {pv}")
        if state.drawdown > s.max_drawdown:
            self.breaker.record_abnormal_loss(f"drawdown {state.drawdown:.2%} exceeds {s.max_drawdown:.1%}")
        if self.breaker.tripped and self.breaker.tripped_at and (now - self.breaker.tripped_at).total_seconds() < 5:
            self.repo.record_risk_event(
                "circuit_breaker_tripped", "CRITICAL", self.breaker.trip_reason or "", payload=self.breaker.status()
            )
            self.notifier.notify("circuit_breaker", "Circuit breaker tripped", self.breaker.trip_reason or "", "CRITICAL")
        # significant drawdown alerts at every 5% step
        step = (state.drawdown // Decimal("0.05")) * Decimal("0.05")
        if step > self._last_drawdown_alert and step > 0:
            self.notifier.notify(
                "drawdown", "Significant drawdown", f"drawdown {state.drawdown:.2%} (peak {state.peak_value})", "WARNING"
            )
        self._last_drawdown_alert = step

    def _publish_status(self, report: CycleReport) -> None:
        last_order = self.repo.orders(limit=1)
        status = {
            "mode": self.mode,
            "paper": self.mode != "live",
            "trading_enabled": self.s.trading_enabled,
            "live_orders_permitted": self.s.live_orders_permitted,
            "broker": self.broker.name,
            "broker_connected": report.broker_connected,
            "market_open": report.market_open,
            "last_cycle": report.to_dict(),
            "last_market_data_update": report.last_data_update.isoformat() if report.last_data_update else None,
            "last_strategy_execution": report.started_at.isoformat(),
            "last_order": last_order[0].to_dict() if last_order else None,
            "circuit_breaker": self.breaker.status(),
            "errors": report.errors,
            "updated_at": utcnow().isoformat(),
        }
        self.repo.set_state("agent_status", status)
        self.repo.set_state("metrics", self.metrics.snapshot())
