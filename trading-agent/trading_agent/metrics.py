"""Minimal in-process metrics registry with Prometheus text exposition (no external deps)."""

from __future__ import annotations

import threading
from collections import defaultdict
from typing import Any


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.counters: dict[str, float] = defaultdict(float)
        self.gauges: dict[str, float] = {}
        self.hist: dict[str, dict[str, float]] = defaultdict(lambda: {"count": 0.0, "sum": 0.0, "max": 0.0})

    def inc(self, name: str, value: float = 1.0) -> None:
        with self._lock:
            self.counters[name] += value

    def set(self, name: str, value: float) -> None:
        with self._lock:
            self.gauges[name] = float(value)

    def observe(self, name: str, value: float) -> None:
        with self._lock:
            h = self.hist[name]
            h["count"] += 1
            h["sum"] += value
            h["max"] = max(h["max"], value)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            out: dict[str, Any] = {"counters": dict(self.counters), "gauges": dict(self.gauges), "histograms": {}}
            for k, h in self.hist.items():
                out["histograms"][k] = {**h, "avg": (h["sum"] / h["count"]) if h["count"] else 0.0}
            c = self.counters
            submitted = c.get("orders_submitted", 0.0)
            rejected = c.get("orders_rejected", 0.0)
            out["derived"] = {
                "order_rejection_rate": (rejected / submitted) if submitted else 0.0,
                "error_rate": (c.get("errors", 0.0) / c.get("cycles", 1.0)),
            }
            return out

    def prometheus(self) -> str:
        return self.render(self.snapshot())

    @staticmethod
    def render(snap: dict[str, Any]) -> str:
        """Prometheus text exposition of a snapshot (possibly one persisted by another process)."""
        lines = []
        for k, v in snap.get("counters", {}).items():
            lines.append(f"# TYPE trading_{k} counter\ntrading_{k} {v}")
        for k, v in snap.get("gauges", {}).items():
            lines.append(f"# TYPE trading_{k} gauge\ntrading_{k} {v}")
        for k, h in snap.get("histograms", {}).items():
            lines.append(f"# TYPE trading_{k} summary")
            lines.append(f"trading_{k}_count {h['count']}\ntrading_{k}_sum {h['sum']}\ntrading_{k}_max {h['max']}")
        for k, v in snap.get("derived", {}).items():
            lines.append(f"# TYPE trading_{k} gauge\ntrading_{k} {v}")
        return "\n".join(lines) + "\n"


metrics = Metrics()
