"""Shared fixtures: controllable clock, calendar, prices and a fully wired paper stack."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from trading_agent.broker.simulated import SimulatedBroker
from trading_agent.config import Settings, load_settings
from trading_agent.db import Database, Repository
from trading_agent.decision import DecisionEngine
from trading_agent.domain import Bar, Instrument, MarketStatus, Quote
from trading_agent.execution.order_manager import OrderManager
from trading_agent.execution.reconciliation import Reconciler
from trading_agent.market_data.base import MarketDataProvider
from trading_agent.metrics import Metrics
from trading_agent.notifications.base import LogNotifier
from trading_agent.portfolio.manager import PortfolioManager
from trading_agent.risk.circuit_breaker import CircuitBreaker
from trading_agent.risk.manager import RiskManager
from trading_agent.risk.sizing import PositionSizer
from trading_agent.scheduler.cycle import TradingCycle
from trading_agent.strategies import build_strategy

# "Now" at import time; the FakeCalendar decides whether the market is open, not the clock.
OPEN_TIME = datetime.now(UTC).replace(microsecond=0)


class FakeClock:
    def __init__(self, now: datetime = OPEN_TIME) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> datetime:
        self.now += timedelta(**kwargs)
        return self.now


class FakeCalendar:
    """Duck-typed stand-in for MarketCalendar with a switchable open flag."""

    def __init__(self, is_open: bool = True) -> None:
        self.open = is_open

    def is_open(self, at: datetime) -> bool:
        return self.open

    def status(self, at: datetime) -> MarketStatus:
        return MarketStatus(is_open=self.open, timestamp=at, source="fake")

    def local_day(self, at: datetime) -> date:
        return at.date()

    def next_open(self, at: datetime) -> datetime:
        return at + timedelta(hours=1)


class Prices:
    """Mutable price source: symbol -> price, quotes stamped with the clock's time."""

    def __init__(self, clock: FakeClock, **prices) -> None:
        self.clock = clock
        self.prices = {k: Decimal(str(v)) for k, v in prices.items()}
        self.stale_by = timedelta(0)

    def set(self, symbol: str, value) -> None:
        self.prices[symbol] = Decimal(str(value))

    def __call__(self, symbol: str) -> Quote | None:
        if symbol not in self.prices:
            return None
        return Quote(symbol=symbol, price=self.prices[symbol], timestamp=self.clock.now - self.stale_by)


def make_bars(
    symbol: str, n: int, start: float = 100.0, step: float = 0.5, end_at: datetime = OPEN_TIME, vol: float = 0.5
) -> list[Bar]:
    """Deterministic bars ending the day before ``end_at``; positive step = uptrend."""
    bars = []
    day = end_at.date() - timedelta(days=n)
    px = start
    for i in range(n):
        px += step
        o, c = px - step / 2, px
        hi, lo = max(o, c) + vol, min(o, c) - vol
        bars.append(
            Bar(
                symbol,
                datetime.combine(day + timedelta(days=i), datetime.min.time(), tzinfo=UTC).replace(hour=21),
                Decimal(str(round(o, 4))),
                Decimal(str(round(hi, 4))),
                Decimal(str(round(lo, 4))),
                Decimal(str(round(c, 4))),
                Decimal(1_000_000),
            )
        )
    return bars


class StaticData(MarketDataProvider):
    name = "static"

    def __init__(self, bars: dict[str, list[Bar]], prices: Prices) -> None:
        self.bars = bars
        self.prices = prices
        self._touch()

    def get_quote(self, symbol: str) -> Quote | None:
        return self.prices(symbol)

    def get_bars(self, symbol: str, start=None, end=None, limit=None) -> list[Bar]:
        b = self.bars.get(symbol, [])
        return b[-limit:] if limit else b


@pytest.fixture
def settings(tmp_path) -> Settings:
    return load_settings(
        _env_file=None,
        trading_enabled=True,
        universe="AAA,BBB",
        database_url=f"sqlite:///{tmp_path}/test.db",
        sim_initial_cash="100000",
        sim_commission_pct="0.001",
        sim_slippage_bps="10",
        max_order_value="20000",
        max_data_age_seconds=900,
    )


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def calendar() -> FakeCalendar:
    return FakeCalendar(True)


@pytest.fixture
def prices(clock) -> Prices:
    return Prices(clock, AAA=100, BBB=50)


@pytest.fixture
def instruments() -> dict[str, Instrument]:
    return {
        "AAA": Instrument("AAA", "Triple A", sector="Technology"),
        "BBB": Instrument("BBB", "Double B", sector="Technology"),
        "LEV": Instrument("LEV", "3X Leveraged", leveraged=True),
    }


@pytest.fixture
def broker(settings, clock, calendar, prices, instruments) -> SimulatedBroker:
    return SimulatedBroker(
        price_source=prices,
        clock=clock,
        market_status_fn=calendar.status,
        initial_cash=settings.sim_initial_cash,
        commission_pct=settings.sim_commission_pct,
        slippage_bps=settings.sim_slippage_bps,
        enforce_market_hours=True,
        instruments=instruments,
    )


@pytest.fixture
def db(settings) -> Database:
    d = Database(settings.database_url)
    d.create_all()
    yield d
    d.dispose()


@pytest.fixture
def repo(db) -> Repository:
    return Repository(db)


@pytest.fixture
def breaker(settings) -> CircuitBreaker:
    return CircuitBreaker(
        settings.circuit_breaker_max_api_errors,
        settings.circuit_breaker_max_rejected_orders,
        settings.circuit_breaker_max_auth_failures,
        settings.circuit_breaker_window_seconds,
    )


@pytest.fixture
def notifier() -> LogNotifier:
    return LogNotifier()


@pytest.fixture
def metrics() -> Metrics:
    return Metrics()


@pytest.fixture
def engine(settings) -> DecisionEngine:
    strategy = build_strategy(
        "trend_following",
        fast_window=settings.strategy_fast_window,
        slow_window=settings.strategy_slow_window,
        atr_window=settings.strategy_atr_window,
        atr_stop_multiplier=settings.strategy_atr_stop_multiplier,
    )
    sizer = PositionSizer(
        settings.max_risk_per_trade,
        settings.max_position_size,
        settings.max_order_value,
        settings.max_portfolio_exposure,
        settings.stop_loss_pct,
        settings.strategy_atr_stop_multiplier,
    )
    return DecisionEngine(settings, strategy, RiskManager(settings), sizer)


@pytest.fixture
def oms(settings, broker, repo, breaker, notifier, metrics) -> OrderManager:
    return OrderManager(broker, repo, breaker, notifier, metrics, mode="paper", trading_enabled=True)


@pytest.fixture
def portfolio(settings) -> PortfolioManager:
    return PortfolioManager(settings.cash_reserve)


@pytest.fixture
def uptrend_bars() -> dict[str, list[Bar]]:
    return {"AAA": make_bars("AAA", 80, start=60, step=0.5), "BBB": make_bars("BBB", 80, start=80, step=-0.4)}


@pytest.fixture
def data(uptrend_bars, prices) -> StaticData:
    return StaticData(uptrend_bars, prices)


@pytest.fixture
def cycle(settings, broker, data, calendar, repo, engine, oms, breaker, notifier, metrics, portfolio) -> TradingCycle:
    reconciler = Reconciler(broker, repo, breaker, strict=False)
    return TradingCycle(settings, broker, data, calendar, repo, engine, oms, reconciler, portfolio, breaker, notifier, metrics)
