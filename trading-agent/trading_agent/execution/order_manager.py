"""Order management system (OMS).

Every submission walks the same checklist:

 1. market open            6. risk checks (from the decision, re-verified)
 2. symbol tradable        7. no equivalent order already exists (DB + broker)
 3. account state fresh    8. decision logged
 4. buying power           9. order persisted as PENDING_SUBMIT with an idempotency key
 5. current position      10. broker submit; response verified
                          11. tracked until a terminal state

Idempotency: the key is derived from (symbol, side, quantity, strategy, trading day)
and enforced with a unique DB constraint *before* the broker is called. A retry of
the same decision returns the existing order instead of creating a new one. If the
outcome of a submission is unknown (network error after the request left), the order
is marked UNKNOWN and must be reconciled (looked up by client id) before any new
order for that symbol is allowed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from trading_agent.broker.base import (
    Broker,
    BrokerAuthError,
    BrokerConnectionError,
    OrderNotSubmittedError,
    OrderOutcomeUnknownError,
    OrderRejectedError,
)
from trading_agent.db.repository import DuplicateOrderError, Repository
from trading_agent.domain import Decision, Order, OrderStatus, Side, money, utcnow
from trading_agent.logging_config import EventLogger, get_correlation_id
from trading_agent.metrics import Metrics
from trading_agent.notifications.base import Notifier
from trading_agent.portfolio.manager import PortfolioState
from trading_agent.risk.circuit_breaker import CircuitBreaker

log = EventLogger("execution.oms")


@dataclass(slots=True)
class SubmissionResult:
    order: Order | None
    submitted: bool
    reason: str


class OrderManager:
    def __init__(
        self,
        broker: Broker,
        repo: Repository,
        breaker: CircuitBreaker,
        notifier: Notifier,
        metrics: Metrics,
        mode: str,
        trading_enabled: bool,
        live_orders_permitted: bool = False,
    ) -> None:
        self.broker = broker
        self.repo = repo
        self.breaker = breaker
        self.notifier = notifier
        self.metrics = metrics
        self.mode = mode
        self.trading_enabled = trading_enabled
        self.live_orders_permitted = live_orders_permitted

    # ------------------------------------------------------------ submit
    def submit(self, decision: Decision, state: PortfolioState, market_open: bool, trading_day: date) -> SubmissionResult:
        req = decision.order_request
        cid = get_correlation_id()
        if req is None or not decision.approved:
            return SubmissionResult(None, False, "decision not approved")

        # Hard gates that never depend on the caller having done the right thing.
        if not self.trading_enabled:
            return self._veto(decision, "kill switch: TRADING_ENABLED=false")
        if self.breaker.tripped:
            return self._veto(decision, f"circuit breaker tripped: {self.breaker.trip_reason}")
        if self.mode == "live" and not self.live_orders_permitted:
            return self._veto(decision, "live gate not satisfied; refusing to submit live order")
        if not self.broker.is_paper and not self.live_orders_permitted:
            return self._veto(decision, "broker is not a paper endpoint but live gate is closed")
        if not market_open:  # 1
            return self._veto(decision, "market closed")
        failed = [c for c in decision.risk_checks if not c.passed]  # 6
        if failed:
            return self._veto(decision, "risk checks failed: " + ", ".join(c.name for c in failed))
        if not self.broker.is_tradable(req.symbol):  # 2
            return self._veto(decision, f"{req.symbol} not tradable at broker")

        # 3-5: re-read account/position from the broker right before submitting.
        try:
            account = self.broker.get_account()
            positions = {p.symbol: p for p in self.broker.get_positions()}
        except BrokerAuthError as exc:
            self.breaker.record_auth_failure()
            return self._fail(decision, f"auth failure while verifying account: {exc}")
        except BrokerConnectionError as exc:
            self.breaker.record_connectivity_failure()
            return self._fail(decision, f"broker unavailable while verifying account: {exc}")
        price_ref = decision.current_price or Decimal("0")
        order_value = money(req.quantity * price_ref)
        if req.side == Side.BUY:
            if order_value > account.buying_power or order_value > state.buying_power:  # 4
                return self._veto(
                    decision, f"order value {order_value} exceeds buying power {min(account.buying_power, state.buying_power)}"
                )
        else:
            held = positions.get(req.symbol).quantity if req.symbol in positions else Decimal("0")  # 5
            if req.quantity > held:
                return self._veto(decision, f"cannot sell {req.quantity}: only {held} held")

        # 7: equivalent order at broker or in our DB?
        if any(o.symbol == req.symbol and o.side == req.side and o.is_open for o in self.repo.open_orders()):
            return self._veto(decision, "equivalent open order already recorded locally")
        try:
            if any(o.symbol == req.symbol and o.side == req.side and o.is_open for o in self.broker.get_orders(open_only=True)):
                return self._veto(decision, "equivalent open order already exists at broker")
        except BrokerConnectionError as exc:
            self.breaker.record_connectivity_failure()
            return self._fail(decision, f"broker unavailable while checking open orders: {exc}")

        # 8: log the decision (the persisted Decision record is written by the caller).
        log.info(
            "order_decision",
            decision.explanation(),
            symbol=req.symbol,
            client_order_id=req.client_order_id,
            strategy=req.strategy,
        )

        # 9: persist with idempotency key before touching the broker.
        base_key = self.repo.idempotency_key(req.symbol, req.side, req.quantity, req.strategy, trading_day.isoformat())
        existing = self.repo.find_order_by_idempotency_key(base_key)
        key = base_key
        if existing is not None:
            if existing.status == OrderStatus.FAILED:
                # Confirmed not at broker: allow exactly one retry per failed attempt.
                attempt = 1
                while existing is not None and existing.status == OrderStatus.FAILED:
                    key = self.repo.idempotency_key(
                        req.symbol, req.side, req.quantity, req.strategy, f"{trading_day.isoformat()}#retry{attempt}"
                    )
                    existing = self.repo.find_order_by_idempotency_key(key)
                    attempt += 1
            if existing is not None:
                log.info(
                    "order_idempotent_hit",
                    symbol=req.symbol,
                    client_order_id=existing.client_order_id,
                    status=existing.status.value,
                )
                return SubmissionResult(
                    existing, False, f"idempotent: order {existing.client_order_id} already exists ({existing.status.value})"
                )
        now = utcnow()
        order = Order(
            client_order_id=req.client_order_id,
            symbol=req.symbol,
            side=req.side,
            quantity=req.quantity,
            order_type=req.order_type,
            status=OrderStatus.PENDING_SUBMIT,
            created_at=now,
            updated_at=now,
            limit_price=req.limit_price,
            stop_price=req.stop_price,
            time_in_force=req.time_in_force,
            strategy=req.strategy,
            reason=req.reason,
            correlation_id=cid,
        )
        try:
            self.repo.create_order(order, key, self.mode, [c.to_dict() for c in decision.risk_checks])
        except DuplicateOrderError:
            existing = self.repo.find_order_by_idempotency_key(key)
            return SubmissionResult(existing, False, "idempotent: duplicate submission suppressed")

        # 10: submit and verify response.
        started = time.perf_counter()
        try:
            broker_order = self.broker.submit_order(req)
        except OrderRejectedError as exc:
            order.status, order.reject_reason, order.updated_at = OrderStatus.REJECTED, str(exc), utcnow()
            self.repo.update_order(order)
            self.breaker.record_rejected_order()
            self.metrics.inc("orders_submitted")
            self.metrics.inc("orders_rejected")
            self.repo.record_risk_event("order_rejected", "WARNING", str(exc), cid, req.symbol, req.client_order_id)
            self.notifier.notify(
                "order_rejected", f"Order rejected: {req.side.value.upper()} {req.quantity} {req.symbol}", str(exc), "WARNING"
            )
            return SubmissionResult(order, False, f"rejected: {exc}")
        except BrokerAuthError as exc:
            order.status, order.reject_reason, order.updated_at = OrderStatus.FAILED, str(exc), utcnow()
            self.repo.update_order(order)
            self.breaker.record_auth_failure()
            self.metrics.inc("errors")
            self.notifier.notify("broker_auth_failure", "Broker authentication failed", str(exc), "ERROR")
            return SubmissionResult(order, False, f"auth failure: {exc}")
        except OrderOutcomeUnknownError as exc:
            order.status, order.reject_reason, order.updated_at = OrderStatus.UNKNOWN, str(exc), utcnow()
            self.repo.update_order(order)
            self.breaker.record_api_error()
            self.metrics.inc("errors")
            self.repo.record_system_event(
                "oms", "order_outcome_unknown", "ERROR", str(exc), cid, {"client_order_id": req.client_order_id}
            )
            self.notifier.notify("order_outcome_unknown", f"Order outcome unknown for {req.symbol}", str(exc), "ERROR")
            return SubmissionResult(order, False, f"outcome unknown: {exc}")
        except OrderNotSubmittedError as exc:
            order.status, order.reject_reason, order.updated_at = OrderStatus.FAILED, str(exc), utcnow()
            self.repo.update_order(order)
            self.breaker.record_connectivity_failure()
            self.metrics.inc("errors")
            self.notifier.notify("broker_connection_failure", "Broker unreachable before submit", str(exc), "ERROR")
            return SubmissionResult(order, False, f"not submitted: {exc}")
        except BrokerConnectionError as exc:
            # The request may or may not have reached the broker: verify by client id.
            found = None
            try:
                found = self.broker.get_order(req.client_order_id)
            except Exception:  # noqa: BLE001
                found = None
                lookup_failed = True
            else:
                lookup_failed = False
            if found is not None:
                broker_order = found
            else:
                order.status = OrderStatus.UNKNOWN if lookup_failed else OrderStatus.FAILED
                order.reject_reason, order.updated_at = str(exc), utcnow()
                self.repo.update_order(order)
                self.breaker.record_connectivity_failure()
                self.metrics.inc("errors")
                self.notifier.notify("broker_connection_failure", "Broker connection failure during submit", str(exc), "ERROR")
                return SubmissionResult(order, False, f"connection failure: {exc} (status {order.status.value})")
        latency_ms = int((time.perf_counter() - started) * 1000)
        self.metrics.observe("order_submit_latency_ms", latency_ms)
        self.metrics.inc("orders_submitted")
        if broker_order.client_order_id != req.client_order_id:
            self.breaker.record_inconsistent_state("broker returned a different client_order_id")
            order.status, order.reject_reason, order.updated_at = OrderStatus.UNKNOWN, "client_order_id mismatch", utcnow()
            self.repo.update_order(order)
            return SubmissionResult(order, False, "broker response mismatch")
        self._merge(order, broker_order)
        self.repo.update_order(order)
        log.info(
            "order_submitted",
            symbol=order.symbol,
            client_order_id=order.client_order_id,
            order_id=order.broker_order_id,
            latency_ms=latency_ms,
            status=order.status.value,
        )
        self.notifier.notify(
            "order_submitted",
            f"Order submitted: {order.side.value.upper()} {order.quantity} {order.symbol}",
            f"{order.order_type.value} order, status {order.status.value}",
            "INFO",
            {"client_order_id": order.client_order_id, "mode": self.mode},
        )
        if order.status == OrderStatus.FILLED:
            self._notify_fill(order)
        elif order.status == OrderStatus.REJECTED:
            self.breaker.record_rejected_order()
            self.metrics.inc("orders_rejected")
            self.notifier.notify("order_rejected", f"Order rejected: {order.symbol}", order.reject_reason or "", "WARNING")
        return SubmissionResult(order, True, "submitted")

    # ----------------------------------------------------------- tracking
    def track_open_orders(self) -> list[Order]:
        """Refresh every locally-open order from the broker (11). Returns orders that changed."""
        changed: list[Order] = []
        for order in self.repo.open_orders():
            try:
                remote = self.broker.get_order(order.client_order_id)
            except BrokerAuthError:
                self.breaker.record_auth_failure()
                continue
            except BrokerConnectionError:
                self.breaker.record_connectivity_failure()
                continue
            if remote is None:
                if order.status in (OrderStatus.PENDING_SUBMIT, OrderStatus.UNKNOWN):
                    age = (utcnow() - order.created_at).total_seconds()
                    if age > 120:
                        order.status, order.reject_reason, order.updated_at = (
                            OrderStatus.FAILED,
                            "not found at broker after submission",
                            utcnow(),
                        )
                        self.repo.update_order(order)
                        changed.append(order)
                    continue
                self.breaker.record_inconsistent_state(f"open order {order.client_order_id} vanished at broker")
                continue
            before = (order.status, order.filled_quantity)
            self._merge(order, remote)
            if before != (order.status, order.filled_quantity):
                order.updated_at = utcnow()
                self.repo.update_order(order)
                changed.append(order)
                if order.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
                    self._notify_fill(order)
                elif order.status == OrderStatus.REJECTED:
                    self.breaker.record_rejected_order()
                    self.metrics.inc("orders_rejected")
                    self.notifier.notify(
                        "order_rejected", f"Order rejected: {order.symbol}", order.reject_reason or "", "WARNING"
                    )
        return changed

    def cancel_all_open(self, reason: str) -> int:
        n = 0
        for order in self.repo.open_orders():
            try:
                remote = self.broker.cancel_order(order.client_order_id)
            except (BrokerConnectionError, OrderRejectedError, BrokerAuthError) as exc:
                log.warning("cancel_failed", client_order_id=order.client_order_id, error=str(exc))
                continue
            self._merge(order, remote)
            self.repo.update_order(order)
            n += 1
        if n:
            self.repo.record_system_event(
                "oms", "orders_cancelled", "WARNING", f"cancelled {n} open orders: {reason}", get_correlation_id()
            )
        return n

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _merge(local: Order, remote: Order) -> None:
        local.broker_order_id = remote.broker_order_id or local.broker_order_id
        local.status = remote.status
        local.filled_quantity = remote.filled_quantity
        local.avg_fill_price = remote.avg_fill_price
        local.commission = remote.commission or local.commission
        local.reject_reason = remote.reject_reason or local.reject_reason
        local.updated_at = utcnow()
        known = {f.fill_id for f in local.fills}
        for f in remote.fills:
            if f.fill_id not in known:
                local.fills.append(f)

    def _notify_fill(self, order: Order) -> None:
        self.metrics.inc("orders_filled" if order.status == OrderStatus.FILLED else "orders_partially_filled")
        self.notifier.notify(
            "order_filled",
            f"Order {order.status.value}: {order.side.value.upper()} {order.filled_quantity}/{order.quantity} {order.symbol}",
            f"avg price {order.avg_fill_price}, commission {order.commission}",
            "INFO",
            {"client_order_id": order.client_order_id, "mode": self.mode},
        )

    def _veto(self, decision: Decision, reason: str) -> SubmissionResult:
        self.metrics.inc("orders_vetoed")
        log.warning("order_vetoed", reason, symbol=decision.symbol, strategy=decision.strategy)
        self.repo.record_risk_event(
            "order_vetoed",
            "WARNING",
            reason,
            get_correlation_id(),
            decision.symbol,
            decision.order_request.client_order_id if decision.order_request else None,
        )
        return SubmissionResult(None, False, reason)

    def _fail(self, decision: Decision, reason: str) -> SubmissionResult:
        self.metrics.inc("errors")
        log.error("order_submission_failed", reason, symbol=decision.symbol)
        self.repo.record_system_event(
            "oms", "order_submission_failed", "ERROR", reason, get_correlation_id(), {"symbol": decision.symbol}
        )
        return SubmissionResult(None, False, reason)

    def summary(self) -> dict[str, Any]:
        return {"mode": self.mode, "trading_enabled": self.trading_enabled, "breaker": self.breaker.status()}
