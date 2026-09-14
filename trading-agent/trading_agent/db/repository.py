"""Repository: all reads/writes go through here so the rest of the code never touches ORM rows.

Order and fill writes are idempotent (unique ``idempotency_key`` / ``fill_id``) which is
the last line of defence against duplicate trades after retries or restarts.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.exc import IntegrityError

from trading_agent.db.base import Database
from trading_agent.db.models import (
    AccountRow,
    ConfigurationChangeRow,
    DecisionRow,
    FillRow,
    InstrumentRow,
    KeyValueRow,
    MarketDataRow,
    OrderRow,
    PortfolioSnapshotRow,
    PositionRow,
    RiskEventRow,
    SignalRow,
    StrategyRunRow,
    SystemEventRow,
)
from trading_agent.domain import (
    Account,
    Bar,
    Decision,
    Fill,
    Instrument,
    Order,
    OrderStatus,
    OrderType,
    Position,
    Side,
    Signal,
    TimeInForce,
    utcnow,
)


def _aware(dt: datetime) -> datetime:
    """SQLite drops tzinfo; normalise everything read back to UTC-aware."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


class DuplicateOrderError(RuntimeError):
    pass


class Repository:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------------ accounts
    def record_account(self, account: Account, broker: str, mode: str) -> None:
        with self.db.session() as s:
            s.add(
                AccountRow(
                    account_id=account.account_id,
                    broker=broker,
                    mode=mode,
                    currency=account.currency,
                    cash=account.cash,
                    buying_power=account.buying_power,
                    portfolio_value=account.portfolio_value,
                    equity=account.equity,
                    timestamp=account.timestamp,
                )
            )

    # --------------------------------------------------------------- instruments
    def upsert_instrument(self, inst: Instrument) -> None:
        with self.db.session() as s:
            row = s.get(InstrumentRow, inst.symbol)
            if row is None:
                row = InstrumentRow(symbol=inst.symbol)
                s.add(row)
            row.name = inst.name
            row.exchange = inst.exchange
            row.currency = inst.currency
            row.asset_class = inst.asset_class.value
            row.sector = inst.sector
            row.tradable = inst.tradable
            row.leveraged = inst.leveraged
            row.derivative = inst.derivative
            row.updated_at = utcnow()

    def get_instrument_sector(self, symbol: str) -> str | None:
        with self.db.session() as s:
            row = s.get(InstrumentRow, symbol)
            return row.sector if row else None

    # --------------------------------------------------------------- market data
    def store_bars(self, bars: list[Bar], timeframe: str = "1D", source: str = "") -> int:
        n = 0
        with self.db.session() as s:
            for b in bars:
                exists = s.execute(
                    select(MarketDataRow.id).where(
                        MarketDataRow.symbol == b.symbol,
                        MarketDataRow.timestamp == b.timestamp,
                        MarketDataRow.timeframe == timeframe,
                    )
                ).first()
                if exists:
                    continue
                s.add(
                    MarketDataRow(
                        symbol=b.symbol,
                        timeframe=timeframe,
                        timestamp=b.timestamp,
                        open=b.open,
                        high=b.high,
                        low=b.low,
                        close=b.close,
                        volume=b.volume,
                        source=source,
                    )
                )
                n += 1
        return n

    def load_bars(self, symbol: str, timeframe: str = "1D", limit: int | None = None) -> list[Bar]:
        with self.db.session() as s:
            q = (
                select(MarketDataRow)
                .where(MarketDataRow.symbol == symbol, MarketDataRow.timeframe == timeframe)
                .order_by(MarketDataRow.timestamp)
            )
            rows = s.execute(q).scalars().all()
        bars = [Bar(r.symbol, _aware(r.timestamp), r.open, r.high, r.low, r.close, r.volume) for r in rows]
        return bars[-limit:] if limit else bars

    # ------------------------------------------------------------ strategy runs
    def start_run(self, run_id: str, correlation_id: str, strategy: str, mode: str) -> None:
        with self.db.session() as s:
            s.add(
                StrategyRunRow(
                    run_id=run_id,
                    correlation_id=correlation_id,
                    strategy=strategy,
                    mode=mode,
                    started_at=utcnow(),
                )
            )

    def finish_run(self, run_id: str, status: str, error: str | None = None, **counts: int) -> None:
        with self.db.session() as s:
            row = s.execute(select(StrategyRunRow).where(StrategyRunRow.run_id == run_id)).scalar_one_or_none()
            if row is None:
                return
            row.finished_at = utcnow()
            row.status = status
            row.error = error
            row.latency_ms = int((row.finished_at - _aware(row.started_at)).total_seconds() * 1000)
            for k, v in counts.items():
                if hasattr(row, k):
                    setattr(row, k, v)

    def last_run(self) -> dict[str, Any] | None:
        with self.db.session() as s:
            row = s.execute(select(StrategyRunRow).order_by(desc(StrategyRunRow.id))).scalars().first()
            if row is None:
                return None
            return {
                "run_id": row.run_id,
                "strategy": row.strategy,
                "started_at": _aware(row.started_at).isoformat(),
                "finished_at": _aware(row.finished_at).isoformat() if row.finished_at else None,
                "status": row.status,
                "latency_ms": row.latency_ms,
                "signals_generated": row.signals_generated,
                "orders_submitted": row.orders_submitted,
                "orders_vetoed": row.orders_vetoed,
                "error": row.error,
            }

    # ------------------------------------------------------------------ signals
    def record_signal(self, sig: Signal, run_id: str | None = None) -> None:
        with self.db.session() as s:
            s.add(
                SignalRow(
                    run_id=run_id,
                    symbol=sig.symbol,
                    strategy=sig.strategy,
                    action=sig.action.value,
                    strength=sig.strength,
                    reason=sig.reason,
                    features={k: (str(v) if isinstance(v, Decimal) else v) for k, v in sig.features.items()},
                    timestamp=sig.timestamp,
                )
            )

    # ---------------------------------------------------------------- decisions
    def record_decision(self, d: Decision, run_id: str | None = None) -> None:
        with self.db.session() as s:
            s.add(
                DecisionRow(
                    decision_id=d.decision_id,
                    run_id=run_id,
                    correlation_id=d.correlation_id,
                    symbol=d.symbol,
                    strategy=d.strategy,
                    decision=d.decision,
                    signal_action=d.signal_action.value,
                    current_price=d.current_price,
                    position_size=d.position_size,
                    reason=d.reason,
                    explanation=d.explanation(),
                    payload=d.to_dict(),
                    client_order_id=d.order_request.client_order_id if d.order_request else None,
                    timestamp=d.timestamp,
                )
            )

    def recent_decisions(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.db.session() as s:
            rows = s.execute(select(DecisionRow).order_by(desc(DecisionRow.id)).limit(limit)).scalars().all()
            return [
                {
                    "decision_id": r.decision_id,
                    "timestamp": _aware(r.timestamp).isoformat(),
                    "symbol": r.symbol,
                    "strategy": r.strategy,
                    "decision": r.decision,
                    "signal_action": r.signal_action,
                    "current_price": str(r.current_price) if r.current_price is not None else None,
                    "position_size": str(r.position_size),
                    "reason": r.reason,
                    "explanation": r.explanation,
                    "client_order_id": r.client_order_id,
                }
                for r in rows
            ]

    # ------------------------------------------------------------------- orders
    @staticmethod
    def idempotency_key(symbol: str, side: Side, quantity: Decimal, strategy: str, scope: str) -> str:
        raw = f"{symbol}|{side.value}|{quantity}|{strategy}|{scope}"
        return hashlib.sha256(raw.encode()).hexdigest()[:48]

    def create_order(self, order: Order, idempotency_key: str, mode: str, risk_checks: list[dict]) -> None:
        """Persist a new order. Raises DuplicateOrderError if the idempotency key exists."""
        row = OrderRow(
            client_order_id=order.client_order_id,
            broker_order_id=order.broker_order_id,
            idempotency_key=idempotency_key,
            correlation_id=order.correlation_id,
            symbol=order.symbol,
            side=order.side.value,
            quantity=order.quantity,
            order_type=order.order_type.value,
            limit_price=order.limit_price,
            stop_price=order.stop_price,
            time_in_force=order.time_in_force.value,
            status=order.status.value,
            filled_quantity=order.filled_quantity,
            avg_fill_price=order.avg_fill_price,
            commission=order.commission,
            strategy=order.strategy,
            reason=order.reason,
            reject_reason=order.reject_reason,
            risk_checks=risk_checks,
            mode=mode,
            created_at=order.created_at,
            updated_at=order.updated_at,
        )
        try:
            with self.db.session() as s:
                s.add(row)
        except IntegrityError as exc:
            raise DuplicateOrderError(f"order with idempotency key {idempotency_key} already exists") from exc

    def find_order_by_idempotency_key(self, key: str) -> Order | None:
        with self.db.session() as s:
            row = s.execute(select(OrderRow).where(OrderRow.idempotency_key == key)).scalar_one_or_none()
            return self._order_from_row(s, row) if row else None

    def update_order(self, order: Order) -> None:
        with self.db.session() as s:
            row = s.execute(select(OrderRow).where(OrderRow.client_order_id == order.client_order_id)).scalar_one_or_none()
            if row is None:
                raise KeyError(order.client_order_id)
            row.broker_order_id = order.broker_order_id
            row.status = order.status.value
            row.filled_quantity = order.filled_quantity
            row.avg_fill_price = order.avg_fill_price
            row.commission = order.commission
            row.reject_reason = order.reject_reason
            row.updated_at = order.updated_at
            if order.status in (OrderStatus.SUBMITTED, OrderStatus.ACCEPTED) and row.submitted_at is None:
                row.submitted_at = order.updated_at
            for f in order.fills:
                if s.execute(select(FillRow.id).where(FillRow.fill_id == f.fill_id)).first():
                    continue
                s.add(
                    FillRow(
                        fill_id=f.fill_id,
                        client_order_id=f.client_order_id,
                        broker_fill_id=f.broker_fill_id,
                        symbol=f.symbol,
                        side=f.side.value,
                        quantity=f.quantity,
                        price=f.price,
                        commission=f.commission,
                        timestamp=f.timestamp,
                    )
                )

    def get_order(self, client_order_id: str) -> Order | None:
        with self.db.session() as s:
            row = s.execute(select(OrderRow).where(OrderRow.client_order_id == client_order_id)).scalar_one_or_none()
            return self._order_from_row(s, row) if row else None

    def open_orders(self) -> list[Order]:
        open_statuses = [st.value for st in OrderStatus if st.is_open]
        with self.db.session() as s:
            rows = s.execute(select(OrderRow).where(OrderRow.status.in_(open_statuses))).scalars().all()
            return [self._order_from_row(s, r) for r in rows]

    def orders(self, limit: int = 200, status: str | None = None) -> list[Order]:
        with self.db.session() as s:
            q = select(OrderRow).order_by(desc(OrderRow.id)).limit(limit)
            if status:
                q = q.where(OrderRow.status == status)
            rows = s.execute(q).scalars().all()
            return [self._order_from_row(s, r) for r in rows]

    def count_orders_since(self, since: datetime, symbol: str | None = None) -> int:
        with self.db.session() as s:
            q = select(func.count(OrderRow.id)).where(
                OrderRow.created_at >= since,
                OrderRow.status.notin_([OrderStatus.FAILED.value]),
            )
            if symbol:
                q = q.where(OrderRow.symbol == symbol)
            return int(s.execute(q).scalar_one())

    def count_rejected_since(self, since: datetime) -> int:
        with self.db.session() as s:
            q = select(func.count(OrderRow.id)).where(OrderRow.updated_at >= since, OrderRow.status == OrderStatus.REJECTED.value)
            return int(s.execute(q).scalar_one())

    def _order_from_row(self, s, row: OrderRow) -> Order:
        fills = s.execute(select(FillRow).where(FillRow.client_order_id == row.client_order_id)).scalars().all()
        return Order(
            client_order_id=row.client_order_id,
            symbol=row.symbol,
            side=Side(row.side),
            quantity=Decimal(row.quantity),
            order_type=OrderType(row.order_type),
            status=OrderStatus(row.status),
            created_at=_aware(row.created_at),
            updated_at=_aware(row.updated_at),
            limit_price=Decimal(row.limit_price) if row.limit_price is not None else None,
            stop_price=Decimal(row.stop_price) if row.stop_price is not None else None,
            time_in_force=TimeInForce(row.time_in_force),
            broker_order_id=row.broker_order_id,
            filled_quantity=Decimal(row.filled_quantity),
            avg_fill_price=Decimal(row.avg_fill_price) if row.avg_fill_price is not None else None,
            commission=Decimal(row.commission),
            strategy=row.strategy,
            reason=row.reason,
            reject_reason=row.reject_reason,
            correlation_id=row.correlation_id,
            fills=[
                Fill(
                    fill_id=f.fill_id,
                    client_order_id=f.client_order_id,
                    symbol=f.symbol,
                    side=Side(f.side),
                    quantity=Decimal(f.quantity),
                    price=Decimal(f.price),
                    commission=Decimal(f.commission),
                    timestamp=_aware(f.timestamp),
                    broker_fill_id=f.broker_fill_id,
                )
                for f in fills
            ],
        )

    def fills(self, limit: int = 500) -> list[Fill]:
        with self.db.session() as s:
            rows = s.execute(select(FillRow).order_by(desc(FillRow.id)).limit(limit)).scalars().all()
            return [
                Fill(
                    fill_id=f.fill_id,
                    client_order_id=f.client_order_id,
                    symbol=f.symbol,
                    side=Side(f.side),
                    quantity=Decimal(f.quantity),
                    price=Decimal(f.price),
                    commission=Decimal(f.commission),
                    timestamp=_aware(f.timestamp),
                    broker_fill_id=f.broker_fill_id,
                )
                for f in rows
            ]

    # ---------------------------------------------------------------- snapshots
    def record_snapshot(self, snap: dict[str, Any], positions: list[Position], mode: str) -> int:
        with self.db.session() as s:
            row = PortfolioSnapshotRow(
                timestamp=snap["timestamp"],
                mode=mode,
                cash=snap["available_cash"],
                reserved_cash=snap["reserved_cash"],
                invested_capital=snap["invested_capital"],
                portfolio_value=snap["portfolio_value"],
                buying_power=snap["buying_power"],
                exposure=snap["exposure"],
                daily_pnl=snap["daily_pnl"],
                total_pnl=snap["total_pnl"],
                drawdown=snap["drawdown"],
                peak_value=snap["peak_value"],
                n_positions=len(positions),
            )
            s.add(row)
            s.flush()
            for p in positions:
                s.add(
                    PositionRow(
                        snapshot_id=row.id,
                        symbol=p.symbol,
                        quantity=p.quantity,
                        avg_cost=p.avg_cost,
                        current_price=p.current_price,
                        market_value=p.market_value,
                        unrealized_pnl=p.unrealized_pnl,
                        timestamp=p.timestamp,
                    )
                )
            return int(row.id)

    def snapshots(self, limit: int = 2000) -> list[dict[str, Any]]:
        with self.db.session() as s:
            rows = s.execute(select(PortfolioSnapshotRow).order_by(desc(PortfolioSnapshotRow.id)).limit(limit)).scalars().all()
        rows = list(reversed(rows))
        return [
            {
                "timestamp": _aware(r.timestamp).isoformat(),
                "cash": str(r.cash),
                "reserved_cash": str(r.reserved_cash),
                "invested_capital": str(r.invested_capital),
                "portfolio_value": str(r.portfolio_value),
                "buying_power": str(r.buying_power),
                "exposure": str(r.exposure),
                "daily_pnl": str(r.daily_pnl),
                "total_pnl": str(r.total_pnl),
                "drawdown": str(r.drawdown),
                "peak_value": str(r.peak_value),
                "n_positions": r.n_positions,
            }
            for r in rows
        ]

    def latest_positions(self) -> list[dict[str, Any]]:
        with self.db.session() as s:
            snap = s.execute(select(PortfolioSnapshotRow).order_by(desc(PortfolioSnapshotRow.id))).scalars().first()
            if snap is None:
                return []
            rows = s.execute(select(PositionRow).where(PositionRow.snapshot_id == snap.id)).scalars().all()
            total = Decimal(snap.portfolio_value) or Decimal("1")
            return [
                {
                    "symbol": r.symbol,
                    "quantity": str(r.quantity),
                    "avg_cost": str(r.avg_cost),
                    "current_price": str(r.current_price),
                    "market_value": str(r.market_value),
                    "unrealized_pnl": str(r.unrealized_pnl),
                    "weight": str((Decimal(r.market_value) / total).quantize(Decimal("0.0001"))),
                }
                for r in rows
            ]

    # ------------------------------------------------------------------- events
    def record_risk_event(
        self,
        event_type: str,
        severity: str,
        detail: str,
        correlation_id: str = "-",
        symbol: str | None = None,
        client_order_id: str | None = None,
        payload: dict | None = None,
    ) -> None:
        with self.db.session() as s:
            s.add(
                RiskEventRow(
                    timestamp=utcnow(),
                    correlation_id=correlation_id,
                    event_type=event_type,
                    severity=severity,
                    symbol=symbol,
                    client_order_id=client_order_id,
                    detail=detail,
                    payload=payload or {},
                )
            )

    def record_system_event(
        self,
        component: str,
        event_type: str,
        severity: str,
        message: str,
        correlation_id: str = "-",
        payload: dict | None = None,
    ) -> None:
        with self.db.session() as s:
            s.add(
                SystemEventRow(
                    timestamp=utcnow(),
                    correlation_id=correlation_id,
                    component=component,
                    event_type=event_type,
                    severity=severity,
                    message=message,
                    payload=payload or {},
                )
            )

    def recent_events(self, limit: int = 50, table: str = "system") -> list[dict[str, Any]]:
        model = SystemEventRow if table == "system" else RiskEventRow
        with self.db.session() as s:
            rows = s.execute(select(model).order_by(desc(model.id)).limit(limit)).scalars().all()
            out = []
            for r in rows:
                d = {
                    "timestamp": _aware(r.timestamp).isoformat(),
                    "event_type": r.event_type,
                    "severity": r.severity,
                    "correlation_id": r.correlation_id,
                }
                if table == "system":
                    d["component"] = r.component
                    d["message"] = r.message
                else:
                    d["symbol"] = r.symbol
                    d["detail"] = r.detail
                out.append(d)
            return out

    def count_system_events_since(self, since: datetime, severity: str = "ERROR") -> int:
        with self.db.session() as s:
            q = select(func.count(SystemEventRow.id)).where(
                SystemEventRow.timestamp >= since, SystemEventRow.severity == severity
            )
            return int(s.execute(q).scalar_one())

    # ------------------------------------------------------------ configuration
    def record_configuration(self, redacted: dict[str, Any]) -> bool:
        """Store the redacted config if it differs from the last stored one. Returns True if changed."""
        blob = json.dumps(redacted, sort_keys=True, default=str)
        digest = hashlib.sha256(blob.encode()).hexdigest()
        with self.db.session() as s:
            last = s.execute(select(ConfigurationChangeRow).order_by(desc(ConfigurationChangeRow.id))).scalars().first()
            if last is not None and last.config_hash == digest:
                return False
            diff: dict[str, Any] = {}
            if last is not None:
                for k, v in redacted.items():
                    if last.snapshot.get(k) != v:
                        diff[k] = {"old": last.snapshot.get(k), "new": v}
            s.add(
                ConfigurationChangeRow(
                    timestamp=utcnow(),
                    config_hash=digest,
                    mode=str(redacted.get("trading_mode")),
                    trading_enabled=bool(redacted.get("trading_enabled")),
                    live_confirmed=bool(redacted.get("live_trading_confirmed")),
                    snapshot=redacted,
                    diff=diff,
                )
            )
            return True

    # --------------------------------------------------------------------- kv
    def get_state(self, key: str) -> dict[str, Any] | None:
        with self.db.session() as s:
            row = s.get(KeyValueRow, key)
            return dict(row.value) if row else None

    def set_state(self, key: str, value: dict[str, Any]) -> None:
        with self.db.session() as s:
            row = s.get(KeyValueRow, key)
            if row is None:
                row = KeyValueRow(key=key)
                s.add(row)
            row.value = value
            row.updated_at = utcnow()

    # ------------------------------------------------------------- helpers
    @staticmethod
    def day_start(now: datetime | None = None) -> datetime:
        now = now or utcnow()
        return now.replace(hour=0, minute=0, second=0, microsecond=0)

    @staticmethod
    def window_start(seconds: int, now: datetime | None = None) -> datetime:
        return (now or utcnow()) - timedelta(seconds=seconds)
