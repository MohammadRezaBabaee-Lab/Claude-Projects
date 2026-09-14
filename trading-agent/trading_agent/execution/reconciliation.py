"""Reconcile local state with the broker (the broker is the source of truth).

Detects: orders open locally but unknown at the broker, orders at the broker we did
not place, and position changes not explained by recorded fills. Unexplained
differences trip the circuit breaker because an automated system must not keep
trading on top of state it does not understand.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from trading_agent.broker.base import Broker, BrokerConnectionError
from trading_agent.db.repository import Repository
from trading_agent.domain import OrderStatus, Position, Side, utcnow
from trading_agent.logging_config import EventLogger, get_correlation_id
from trading_agent.risk.circuit_breaker import CircuitBreaker

log = EventLogger("execution.reconciliation")


class Reconciler:
    STATE_KEY = "reconciliation"

    def __init__(self, broker: Broker, repo: Repository, breaker: CircuitBreaker, strict: bool = True) -> None:
        self.broker = broker
        self.repo = repo
        self.breaker = breaker
        self.strict = strict

    def run(self, positions: list[Position] | None = None) -> dict[str, Any]:
        cid = get_correlation_id()
        report: dict[str, Any] = {"unknown_at_broker": [], "unknown_locally": [], "position_mismatches": []}
        try:
            broker_open = {o.client_order_id: o for o in self.broker.get_orders(open_only=True)}
            positions = positions if positions is not None else self.broker.get_positions()
        except BrokerConnectionError as exc:
            self.breaker.record_connectivity_failure()
            log.warning("reconciliation_skipped", str(exc))
            report["error"] = str(exc)
            return report

        # -- orders ---------------------------------------------------------
        local_open = {o.client_order_id: o for o in self.repo.open_orders()}
        for cid_, order in local_open.items():
            if cid_ in broker_open:
                continue
            remote = None
            try:
                remote = self.broker.get_order(cid_)
            except BrokerConnectionError:
                continue
            if remote is None:
                if order.status in (OrderStatus.PENDING_SUBMIT, OrderStatus.UNKNOWN):
                    order.status, order.reject_reason = OrderStatus.FAILED, "reconciliation: not found at broker"
                    self.repo.update_order(order)
                    report["unknown_at_broker"].append(cid_)
                else:
                    report["unknown_at_broker"].append(cid_)
                    self.breaker.record_inconsistent_state(f"local open order {cid_} missing at broker")
            else:
                order.status = remote.status
                order.filled_quantity = remote.filled_quantity
                order.avg_fill_price = remote.avg_fill_price
                order.broker_order_id = remote.broker_order_id
                known = {f.fill_id for f in order.fills}
                order.fills.extend(f for f in remote.fills if f.fill_id not in known)
                self.repo.update_order(order)
        for cid_ in broker_open:
            if cid_ not in local_open and self.repo.get_order(cid_) is None:
                report["unknown_locally"].append(cid_)
        if report["unknown_locally"]:
            msg = f"{len(report['unknown_locally'])} open order(s) at broker were not placed by this agent"
            self.repo.record_risk_event(
                "foreign_orders", "WARNING", msg, cid, payload={"client_order_ids": report["unknown_locally"]}
            )
            if self.strict:
                self.breaker.record_inconsistent_state(msg)

        # -- positions vs expected ------------------------------------------
        expected = self._expected_positions()
        actual = {p.symbol: p.quantity for p in positions if p.quantity != 0}
        if expected is not None:
            for sym in set(expected) | set(actual):
                exp, act = expected.get(sym, Decimal("0")), actual.get(sym, Decimal("0"))
                if exp != act:
                    report["position_mismatches"].append({"symbol": sym, "expected": str(exp), "actual": str(act)})
            if report["position_mismatches"]:
                msg = f"positions differ from fill history: {report['position_mismatches']}"
                self.repo.record_risk_event(
                    "position_mismatch", "ERROR", msg, cid, payload={"mismatches": report["position_mismatches"]}
                )
                if self.strict:
                    self.breaker.record_inconsistent_state(msg)
        self.repo.set_state(
            self.STATE_KEY,
            {"positions": {k: str(v) for k, v in actual.items()}, "as_of": utcnow().isoformat(), "baseline": True},
        )
        return report

    def _expected_positions(self) -> dict[str, Decimal] | None:
        """Positions implied by the last baseline plus fills recorded since it. None without a baseline."""
        state = self.repo.get_state(self.STATE_KEY)
        if not state or not state.get("baseline"):
            return None
        expected = {k: Decimal(v) for k, v in state.get("positions", {}).items()}
        as_of = datetime.fromisoformat(state["as_of"])
        for f in self.repo.fills(limit=5000):
            if f.timestamp <= as_of:
                continue
            delta = f.quantity if f.side == Side.BUY else -f.quantity
            expected[f.symbol] = expected.get(f.symbol, Decimal("0")) + delta
        return {k: v for k, v in expected.items() if v != 0}
