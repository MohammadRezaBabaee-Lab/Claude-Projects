"""Local dashboard API (FastAPI). Read-only: it never places orders or changes config.

Bind to localhost only; there is no authentication by design (see docs/security.md).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, PlainTextResponse

from trading_agent import __version__
from trading_agent.config import Settings
from trading_agent.db import Database, Repository
from trading_agent.domain import Fill, Side
from trading_agent.metrics import metrics
from trading_agent.portfolio.performance import compute_metrics

STATIC = Path(__file__).parent / "static"


def create_app(settings: Settings, db: Database | None = None) -> FastAPI:
    app = FastAPI(title="Trading agent dashboard", version=__version__, docs_url=None, redoc_url=None)
    repo = Repository(db or Database(settings.database_url))

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/agent")
    def agent():
        status = repo.get_state("agent_status") or {}
        return {
            "mode": settings.trading_mode.value,
            "paper": not settings.live_orders_permitted,
            "banner": "LIVE TRADING" if settings.live_orders_permitted else "PAPER TRADING",
            "trading_enabled": settings.trading_enabled,
            "live_orders_permitted": settings.live_orders_permitted,
            "kill_switch_active": not settings.trading_enabled,
            "broker": settings.broker.value,
            "strategy": settings.strategy,
            "universe": settings.universe,
            "status": status,
            "circuit_breaker": repo.get_state("circuit_breaker") or {"tripped": False},
            "last_run": repo.last_run(),
            "config": settings.redacted(),
        }

    @app.get("/api/portfolio")
    def portfolio():
        snaps = repo.snapshots(limit=1)
        return snaps[-1] if snaps else {}

    @app.get("/api/positions")
    def positions():
        return repo.latest_positions()

    @app.get("/api/orders")
    def orders(limit: int = 200):
        out = [o.to_dict() for o in repo.orders(limit=limit)]
        counts: dict[str, int] = {}
        for o in out:
            counts[o["status"]] = counts.get(o["status"], 0) + 1
        return {"orders": out, "counts": counts}

    @app.get("/api/decisions")
    def decisions(limit: int = 50):
        return repo.recent_decisions(limit=limit)

    @app.get("/api/events")
    def events(limit: int = 50):
        return {"system": repo.recent_events(limit, "system"), "risk": repo.recent_events(limit, "risk")}

    @app.get("/api/performance")
    def performance():
        snaps = repo.snapshots(limit=5000)
        fills = repo.fills(limit=5000)
        from datetime import datetime
        from decimal import Decimal

        # one equity point per trading day (last snapshot of the day)
        by_day: dict = {}
        for s in snaps:
            by_day[datetime.fromisoformat(s["timestamp"]).date()] = Decimal(s["portfolio_value"])
        equity = sorted(by_day.items())
        stats = compute_metrics(equity, fills) if len(equity) >= 2 else {}
        return {
            "equity_curve": [{"date": d.isoformat(), "value": str(v)} for d, v in equity],
            "drawdown_curve": stats.get("drawdown_curve", []),
            "monthly_returns": stats.get("monthly_returns", {}),
            "stats": {k: v for k, v in stats.items() if k not in ("drawdown_curve", "monthly_returns")},
            "trade_stats": _trade_stats(fills),
        }

    @app.get("/metrics", response_class=PlainTextResponse)
    def prom():
        # The agent runs in another process; it persists a snapshot every cycle.
        persisted = repo.get_state("metrics")
        return metrics.render(persisted) if persisted else metrics.prometheus()

    @app.get("/healthz")
    def healthz():
        status = repo.get_state("agent_status") or {}
        return {"ok": True, "version": __version__, "last_cycle": status.get("updated_at")}

    return app


def _trade_stats(fills: list[Fill]) -> dict:
    buys = [f for f in fills if f.side == Side.BUY]
    sells = [f for f in fills if f.side == Side.SELL]
    return {
        "fills": len(fills),
        "buys": len(buys),
        "sells": len(sells),
        "commissions": str(sum((f.commission for f in fills), start=__import__("decimal").Decimal("0"))),
    }
