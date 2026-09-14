"""Automatic circuit breaker.

Trips (and stays tripped until a human resets it) when error patterns suggest the
system should stop trading: repeated API errors, rejected orders, auth failures,
connectivity loss, abnormal prices, abnormal portfolio loss or inconsistent state.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta
from typing import Any

from trading_agent.domain import utcnow


class CircuitBreaker:
    STATE_KEY = "circuit_breaker"

    def __init__(
        self,
        max_api_errors: int,
        max_rejected_orders: int,
        max_auth_failures: int,
        window_seconds: int,
        state: dict[str, Any] | None = None,
    ) -> None:
        self.max_api_errors = max_api_errors
        self.max_rejected_orders = max_rejected_orders
        self.max_auth_failures = max_auth_failures
        self.window = timedelta(seconds=window_seconds)
        self._events: dict[str, deque[datetime]] = {
            k: deque() for k in ("api_error", "rejected_order", "auth_failure", "connectivity")
        }
        self.tripped = False
        self.trip_reason: str | None = None
        self.tripped_at: datetime | None = None
        self.history: list[dict[str, Any]] = []
        if state:
            self.load(state)

    # -- persistence ------------------------------------------------------
    def dump(self) -> dict[str, Any]:
        return {
            "tripped": self.tripped,
            "trip_reason": self.trip_reason,
            "tripped_at": self.tripped_at.isoformat() if self.tripped_at else None,
            "history": self.history[-50:],
        }

    def load(self, state: dict[str, Any]) -> None:
        self.tripped = bool(state.get("tripped"))
        self.trip_reason = state.get("trip_reason")
        self.tripped_at = datetime.fromisoformat(state["tripped_at"]) if state.get("tripped_at") else None
        self.history = list(state.get("history", []))

    # -- events -------------------------------------------------------------
    def _record(self, kind: str, limit: int, now: datetime | None = None) -> None:
        now = now or utcnow()
        q = self._events[kind]
        q.append(now)
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= limit:
            self.trip(f"{len(q)} {kind} events within {int(self.window.total_seconds())}s", now)

    def record_api_error(self, now: datetime | None = None) -> None:
        self._record("api_error", self.max_api_errors, now)

    def record_rejected_order(self, now: datetime | None = None) -> None:
        self._record("rejected_order", self.max_rejected_orders, now)

    def record_auth_failure(self, now: datetime | None = None) -> None:
        self._record("auth_failure", self.max_auth_failures, now)

    def record_connectivity_failure(self, now: datetime | None = None) -> None:
        self._record("connectivity", self.max_api_errors, now)

    def record_abnormal_price(self, symbol: str, detail: str) -> None:
        self.trip(f"abnormal price data for {symbol}: {detail}")

    def record_abnormal_loss(self, detail: str) -> None:
        self.trip(f"abnormal portfolio loss: {detail}")

    def record_inconsistent_state(self, detail: str) -> None:
        self.trip(f"inconsistent account state: {detail}")

    def trip(self, reason: str, now: datetime | None = None) -> None:
        if self.tripped:
            return
        self.tripped = True
        self.trip_reason = reason
        self.tripped_at = now or utcnow()
        self.history.append({"at": self.tripped_at.isoformat(), "event": "trip", "reason": reason})

    def reset(self, reason: str) -> None:
        self.tripped = False
        self.trip_reason = None
        self.tripped_at = None
        for q in self._events.values():
            q.clear()
        self.history.append({"at": utcnow().isoformat(), "event": "reset", "reason": reason})

    def status(self) -> dict[str, Any]:
        return {
            "tripped": self.tripped,
            "reason": self.trip_reason,
            "tripped_at": self.tripped_at.isoformat() if self.tripped_at else None,
        }
