"""ORM tables. Monetary columns use Numeric(20, 6) so PostgreSQL stores exact decimals."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from trading_agent.db.base import Base

MONEY = Numeric(20, 6)


class AccountRow(Base):
    __tablename__ = "accounts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(String(64), index=True)
    broker: Mapped[str] = mapped_column(String(32))
    mode: Mapped[str] = mapped_column(String(16))
    currency: Mapped[str] = mapped_column(String(8))
    cash: Mapped[Decimal] = mapped_column(MONEY)
    buying_power: Mapped[Decimal] = mapped_column(MONEY)
    portfolio_value: Mapped[Decimal] = mapped_column(MONEY)
    equity: Mapped[Decimal] = mapped_column(MONEY)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class InstrumentRow(Base):
    __tablename__ = "instruments"
    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), default="")
    exchange: Mapped[str] = mapped_column(String(32), default="")
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    asset_class: Mapped[str] = mapped_column(String(16), default="equity")
    sector: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tradable: Mapped[bool] = mapped_column(Boolean, default=True)
    leveraged: Mapped[bool] = mapped_column(Boolean, default=False)
    derivative: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PositionRow(Base):
    __tablename__ = "positions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("portfolio_snapshots.id"), nullable=True, index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    quantity: Mapped[Decimal] = mapped_column(MONEY)
    avg_cost: Mapped[Decimal] = mapped_column(MONEY)
    current_price: Mapped[Decimal] = mapped_column(MONEY)
    market_value: Mapped[Decimal] = mapped_column(MONEY)
    unrealized_pnl: Mapped[Decimal] = mapped_column(MONEY)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class MarketDataRow(Base):
    __tablename__ = "market_data"
    __table_args__ = (UniqueConstraint("symbol", "timestamp", "timeframe", name="uq_market_data_bar"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    timeframe: Mapped[str] = mapped_column(String(8), default="1D")
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    open: Mapped[Decimal] = mapped_column(MONEY)
    high: Mapped[Decimal] = mapped_column(MONEY)
    low: Mapped[Decimal] = mapped_column(MONEY)
    close: Mapped[Decimal] = mapped_column(MONEY)
    volume: Mapped[Decimal] = mapped_column(MONEY)
    source: Mapped[str] = mapped_column(String(32), default="")


class StrategyRunRow(Base):
    __tablename__ = "strategy_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(48), unique=True, index=True)
    correlation_id: Mapped[str] = mapped_column(String(48), index=True)
    strategy: Mapped[str] = mapped_column(String(64))
    mode: Mapped[str] = mapped_column(String(16))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="running")
    symbols_evaluated: Mapped[int] = mapped_column(Integer, default=0)
    signals_generated: Mapped[int] = mapped_column(Integer, default=0)
    orders_submitted: Mapped[int] = mapped_column(Integer, default=0)
    orders_vetoed: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class SignalRow(Base):
    __tablename__ = "signals"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str | None] = mapped_column(String(48), index=True, nullable=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    strategy: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(8))
    strength: Mapped[float] = mapped_column(Numeric(8, 4), default=0)
    reason: Mapped[str] = mapped_column(Text, default="")
    features: Mapped[dict] = mapped_column(JSON, default=dict)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class DecisionRow(Base):
    __tablename__ = "decisions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    decision_id: Mapped[str] = mapped_column(String(48), unique=True, index=True)
    run_id: Mapped[str | None] = mapped_column(String(48), index=True, nullable=True)
    correlation_id: Mapped[str] = mapped_column(String(48), index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    strategy: Mapped[str] = mapped_column(String(64))
    decision: Mapped[str] = mapped_column(String(16))
    signal_action: Mapped[str] = mapped_column(String(8))
    current_price: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    position_size: Mapped[Decimal] = mapped_column(MONEY, default=0)
    reason: Mapped[str] = mapped_column(Text, default="")
    explanation: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    client_order_id: Mapped[str | None] = mapped_column(String(48), nullable=True, index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class OrderRow(Base):
    __tablename__ = "orders"
    __table_args__ = (Index("ix_orders_symbol_status", "symbol", "status"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    client_order_id: Mapped[str] = mapped_column(String(48), unique=True, index=True)
    broker_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    correlation_id: Mapped[str] = mapped_column(String(48), index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    side: Mapped[str] = mapped_column(String(4))
    quantity: Mapped[Decimal] = mapped_column(MONEY)
    order_type: Mapped[str] = mapped_column(String(16))
    limit_price: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    stop_price: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    time_in_force: Mapped[str] = mapped_column(String(8), default="day")
    status: Mapped[str] = mapped_column(String(24), index=True)
    filled_quantity: Mapped[Decimal] = mapped_column(MONEY, default=0)
    avg_fill_price: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    commission: Mapped[Decimal] = mapped_column(MONEY, default=0)
    strategy: Mapped[str] = mapped_column(String(64), default="")
    reason: Mapped[str] = mapped_column(Text, default="")
    reject_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    risk_checks: Mapped[list] = mapped_column(JSON, default=list)
    mode: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class FillRow(Base):
    __tablename__ = "fills"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fill_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    client_order_id: Mapped[str] = mapped_column(ForeignKey("orders.client_order_id"), index=True)
    broker_fill_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    side: Mapped[str] = mapped_column(String(4))
    quantity: Mapped[Decimal] = mapped_column(MONEY)
    price: Mapped[Decimal] = mapped_column(MONEY)
    commission: Mapped[Decimal] = mapped_column(MONEY, default=0)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class PortfolioSnapshotRow(Base):
    __tablename__ = "portfolio_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    mode: Mapped[str] = mapped_column(String(16))
    cash: Mapped[Decimal] = mapped_column(MONEY)
    reserved_cash: Mapped[Decimal] = mapped_column(MONEY, default=0)
    invested_capital: Mapped[Decimal] = mapped_column(MONEY)
    portfolio_value: Mapped[Decimal] = mapped_column(MONEY)
    buying_power: Mapped[Decimal] = mapped_column(MONEY)
    exposure: Mapped[Decimal] = mapped_column(Numeric(10, 6), default=0)
    daily_pnl: Mapped[Decimal] = mapped_column(MONEY, default=0)
    total_pnl: Mapped[Decimal] = mapped_column(MONEY, default=0)
    drawdown: Mapped[Decimal] = mapped_column(Numeric(10, 6), default=0)
    peak_value: Mapped[Decimal] = mapped_column(MONEY)
    n_positions: Mapped[int] = mapped_column(Integer, default=0)


class RiskEventRow(Base):
    __tablename__ = "risk_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    correlation_id: Mapped[str] = mapped_column(String(48), index=True)
    event_type: Mapped[str] = mapped_column(String(48), index=True)
    severity: Mapped[str] = mapped_column(String(16))
    symbol: Mapped[str | None] = mapped_column(String(32), nullable=True)
    client_order_id: Mapped[str | None] = mapped_column(String(48), nullable=True)
    detail: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)


class SystemEventRow(Base):
    __tablename__ = "system_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    correlation_id: Mapped[str] = mapped_column(String(48), index=True)
    component: Mapped[str] = mapped_column(String(48), index=True)
    event_type: Mapped[str] = mapped_column(String(48), index=True)
    severity: Mapped[str] = mapped_column(String(16))
    message: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)


class ConfigurationChangeRow(Base):
    __tablename__ = "configuration_changes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    config_hash: Mapped[str] = mapped_column(String(64), index=True)
    mode: Mapped[str] = mapped_column(String(16))
    trading_enabled: Mapped[bool] = mapped_column(Boolean)
    live_confirmed: Mapped[bool] = mapped_column(Boolean)
    snapshot: Mapped[dict] = mapped_column(JSON, default=dict)  # redacted settings
    diff: Mapped[dict] = mapped_column(JSON, default=dict)


class KeyValueRow(Base):
    """Small durable key/value store for runtime state (simulated broker state, breaker state)."""

    __tablename__ = "kv_state"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
