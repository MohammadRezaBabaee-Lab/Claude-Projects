"""Composition root and long-running loop.

Builds every component from validated settings, restores persisted state (simulated
broker, circuit breaker, portfolio tracking), runs cycles on the configured interval
and shuts down cleanly on SIGINT/SIGTERM. Crashes inside a cycle are contained; the
process itself restarting is safe because all order state lives in the database and
is reconciled with the broker before the first new order.
"""

from __future__ import annotations

import hashlib
import signal
import threading
import time
from typing import Any

from trading_agent import __version__
from trading_agent.broker.factory import build_broker
from trading_agent.broker.simulated import SimulatedBroker
from trading_agent.config import BrokerKind, Settings, TradingMode
from trading_agent.db import Database, Repository
from trading_agent.decision import DecisionEngine
from trading_agent.domain import utcnow
from trading_agent.execution.order_manager import OrderManager
from trading_agent.execution.reconciliation import Reconciler
from trading_agent.logging_config import EventLogger, new_correlation_id
from trading_agent.market_data.calendar import MarketCalendar
from trading_agent.market_data.factory import build_market_data
from trading_agent.metrics import metrics
from trading_agent.notifications.base import build_notifier
from trading_agent.portfolio.manager import PortfolioManager
from trading_agent.risk.circuit_breaker import CircuitBreaker
from trading_agent.risk.manager import RiskManager
from trading_agent.risk.sizing import PositionSizer
from trading_agent.scheduler.cycle import CycleReport, TradingCycle
from trading_agent.strategies.registry import strategy_from_settings

log = EventLogger("runner")


def ensure_schema(db: Database) -> None:
    """SQLite (development): create tables on the fly. Anything else must be migrated with Alembic."""
    from sqlalchemy import inspect

    from trading_agent.config import ConfigError

    if db.url.startswith("sqlite"):
        db.create_all()
        return
    if "orders" not in inspect(db.engine).get_table_names():
        raise ConfigError("database schema missing: run 'alembic upgrade head' before starting the agent")


SIM_STATE_KEY = "simulated_broker"


class AgentRunner:
    def __init__(self, settings: Settings, db: Database | None = None) -> None:
        self.s = settings
        self.db = db or Database(settings.database_url)
        ensure_schema(self.db)
        self.repo = Repository(self.db)
        self.notifier = build_notifier(settings)
        self.calendar = MarketCalendar(settings.exchange_calendar)
        self.data = build_market_data(settings)
        self.breaker = CircuitBreaker(
            settings.circuit_breaker_max_api_errors,
            settings.circuit_breaker_max_rejected_orders,
            settings.circuit_breaker_max_auth_failures,
            settings.circuit_breaker_window_seconds,
            state=self.repo.get_state(CircuitBreaker.STATE_KEY),
        )
        self.portfolio = PortfolioManager(settings.cash_reserve, state=self.repo.get_state(PortfolioManager.STATE_KEY))
        self.broker = build_broker(
            settings,
            price_source=self.data.get_quote,
            market_status_fn=self.calendar.status,
        )
        if isinstance(self.broker, SimulatedBroker):
            saved = self.repo.get_state(SIM_STATE_KEY)
            if saved:
                self.broker.load_dict(saved)
                log.info("simulated_broker_state_restored", cash=saved.get("cash"), positions=len(saved.get("positions", {})))
        self.strategy = strategy_from_settings(settings)
        self.engine = DecisionEngine(
            settings,
            self.strategy,
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
        self.oms = OrderManager(
            self.broker,
            self.repo,
            self.breaker,
            self.notifier,
            metrics,
            mode=settings.trading_mode.value,
            trading_enabled=settings.trading_enabled,
            live_orders_permitted=settings.live_orders_permitted,
        )
        self.reconciler = Reconciler(self.broker, self.repo, self.breaker, strict=settings.broker != BrokerKind.SIMULATED)
        self.cycle = TradingCycle(
            settings,
            self.broker,
            self.data,
            self.calendar,
            self.repo,
            self.engine,
            self.oms,
            self.reconciler,
            self.portfolio,
            self.breaker,
            self.notifier,
            metrics,
        )
        self._stop = threading.Event()

    # ------------------------------------------------------------------
    def startup(self) -> None:
        new_correlation_id()
        redacted = self.s.redacted()
        changed = self.repo.record_configuration(redacted)
        banner = "LIVE TRADING" if self.s.live_orders_permitted else "PAPER TRADING"
        log.info(
            "agent_starting",
            f"trading agent v{__version__} starting in {banner} mode",
            mode=self.s.trading_mode.value,
            broker=self.broker.name,
            trading_enabled=self.s.trading_enabled,
            config_changed=changed,
            config_hash=hashlib.sha256(str(sorted(redacted.items())).encode()).hexdigest()[:12],
        )
        if self.s.trading_mode == TradingMode.LIVE:
            log.warning("live_mode", "*** LIVE MODE: real orders will be submitted if all gates pass ***")
        self.repo.record_system_event(
            "runner",
            "startup",
            "INFO",
            f"agent started ({banner})",
            payload={"mode": self.s.trading_mode.value, "broker": self.broker.name},
        )
        self.notifier.notify(
            "system_restart",
            f"Trading agent started ({banner})",
            f"broker={self.broker.name} strategy={self.strategy.name} trading_enabled={self.s.trading_enabled}",
            "INFO",
        )
        # Recover: reconcile any in-flight orders before anything else happens.
        self.oms.track_open_orders()
        report = self.reconciler.run()
        log.info("startup_reconciliation", data=report)

    def run_once(self) -> CycleReport:
        report = self.cycle.run_once()
        self.persist_state()
        return report

    def persist_state(self) -> None:
        if isinstance(self.broker, SimulatedBroker):
            self.repo.set_state(SIM_STATE_KEY, self.broker.to_dict())
        self.repo.set_state(CircuitBreaker.STATE_KEY, self.breaker.dump())
        self.repo.set_state(PortfolioManager.STATE_KEY, self.portfolio.dump())

    def run_forever(self, interval_seconds: int | None = None, max_cycles: int | None = None) -> None:
        interval = interval_seconds or self.s.cycle_interval_seconds
        self._install_signal_handlers()
        self.startup()
        cycles = 0
        while not self._stop.is_set():
            report = self.run_once()
            cycles += 1
            if max_cycles and cycles >= max_cycles:
                break
            # Sleep longer while the market is closed but keep monitoring.
            sleep_for = interval if report.market_open else max(interval, 900)
            now = utcnow()
            if not report.market_open:
                try:
                    next_open = self.calendar.next_open(now)
                    sleep_for = min(sleep_for, max(60, int((next_open - now).total_seconds())))
                except Exception:  # noqa: BLE001
                    pass
            self._stop.wait(sleep_for)
        self.shutdown()

    def shutdown(self) -> None:
        self.persist_state()
        self.repo.record_system_event("runner", "shutdown", "INFO", "agent stopped")
        log.info("agent_stopped")

    def stop(self) -> None:
        self._stop.set()

    def _install_signal_handlers(self) -> None:
        def handler(signum, _frame):  # pragma: no cover - signal path
            log.warning("signal_received", signal=signum)
            self.stop()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, handler)
            except ValueError:  # not main thread
                pass

    def status(self) -> dict[str, Any]:
        return {
            "mode": self.s.trading_mode.value,
            "trading_enabled": self.s.trading_enabled,
            "live_orders_permitted": self.s.live_orders_permitted,
            "broker": self.broker.name,
            "strategy": self.strategy.describe(),
            "circuit_breaker": self.breaker.status(),
            "universe": self.s.universe,
        }


def _unused() -> None:  # keep time import referenced for future scheduling extensions
    time.monotonic()
