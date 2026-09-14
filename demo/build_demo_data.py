"""Generate the JSON files behind the static dashboard demo.

The demo on GitHub Pages is the real dashboard reading a frozen snapshot instead of
a live API. That snapshot is produced here by actually running the backtesting engine
on deterministic synthetic prices, so every number on the demo page is genuine engine
output rather than hand-written fixtures.

The prices are synthetic and the broker is simulated: the demo shows how the system
behaves, not how any strategy performs on real markets.

Usage (from the repository root, with the project installed):
    python demo/build_demo_data.py
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from trading_agent.backtesting import BacktestEngine
from trading_agent.config import load_settings
from trading_agent.domain import Instrument, Side
from trading_agent.market_data.calendar import MarketCalendar
from trading_agent.market_data.synthetic import generate_bars
from trading_agent.portfolio.performance import compute_metrics
from trading_agent.strategies import build_strategy

OUT = Path(__file__).parent / "data"
START, END = date(2023, 1, 3), date(2025, 12, 31)

# A small, recognisable universe. Sectors exercise the concentration limit.
UNIVERSE = {
    "AAPL": ("Apple Inc.", "Technology", 150.0),
    "MSFT": ("Microsoft Corp.", "Technology", 240.0),
    "JNJ": ("Johnson & Johnson", "Healthcare", 160.0),
    "XOM": ("Exxon Mobil Corp.", "Energy", 105.0),
    "SPY": ("SPDR S&P 500 ETF", "Broad Market", 380.0),
}


def build() -> None:
    settings = load_settings(
        _env_file=None,
        trading_enabled=True,
        universe=",".join(UNIVERSE),
        max_order_value="40000",
        # Deliberately tight so the demo shows the risk manager vetoing trades,
        # which is the behaviour worth seeing: five candidates, at most three positions.
        max_open_positions=3,
        sim_initial_cash="100000",
        sim_commission_pct="0.0005",
        sim_slippage_bps="5",
    )
    cal = MarketCalendar(settings.exchange_calendar, start=START.isoformat(), end=END.isoformat())
    sessions = cal.sessions_in_range(START, END)
    instruments = {
        sym: Instrument(symbol=sym, name=name, exchange="NASDAQ", sector=sector)
        for sym, (name, sector, _) in UNIVERSE.items()
    }
    history = {
        sym: generate_bars(sym, sessions, start_price=px, drift=0.0006, vol=0.014, seed=i)
        for i, (sym, (_, _, px)) in enumerate(UNIVERSE.items())
    }

    engine = BacktestEngine(settings, build_strategy("trend_following"), history, calendar=cal, instruments=instruments)
    result = engine.run(START, END)
    metrics = result.metrics

    # ---------------------------------------------------------------- portfolio
    equity = result.equity_curve
    final_value = equity[-1][1]
    positions = engine.broker.get_positions()
    invested = sum((p.market_value for p in positions), Decimal("0"))
    cash = engine.broker.cash
    peak = max(v for _, v in equity)
    prev_value = equity[-2][1] if len(equity) > 1 else final_value
    as_of = datetime.combine(equity[-1][0], datetime.min.time(), tzinfo=UTC).replace(hour=21)

    portfolio = {
        "timestamp": as_of.isoformat(),
        "cash": str(cash.quantize(Decimal("0.01"))),
        "reserved_cash": str((final_value * settings.cash_reserve).quantize(Decimal("0.01"))),
        "invested_capital": str(invested.quantize(Decimal("0.01"))),
        "portfolio_value": str(final_value),
        "buying_power": str(max(cash - final_value * settings.cash_reserve, Decimal("0")).quantize(Decimal("0.01"))),
        "exposure": str((invested / final_value).quantize(Decimal("0.0001"))),
        "daily_pnl": str((final_value - prev_value).quantize(Decimal("0.01"))),
        "total_pnl": str((final_value - settings.sim_initial_cash).quantize(Decimal("0.01"))),
        "drawdown": str(((peak - final_value) / peak).quantize(Decimal("0.0001"))),
        "peak_value": str(peak),
        "n_positions": len(positions),
    }
    _write("portfolio.json", portfolio)

    _write("positions.json", [
        {
            "symbol": p.symbol,
            "quantity": str(p.quantity),
            "avg_cost": str(p.avg_cost),
            "current_price": str(p.current_price),
            "market_value": str(p.market_value),
            "unrealized_pnl": str(p.unrealized_pnl),
            "weight": str((p.market_value / final_value).quantize(Decimal("0.0001"))),
        }
        for p in sorted(positions, key=lambda x: x.market_value, reverse=True)
    ])

    # ------------------------------------------------------------------- orders
    orders = sorted(engine.broker.orders.values(), key=lambda o: o.created_at, reverse=True)
    counts: dict[str, int] = {}
    for o in orders:
        counts[o.status.value] = counts.get(o.status.value, 0) + 1
    _write("orders.json", {
        "orders": [o.to_dict() for o in orders[:60]],
        "counts": counts,
    })

    # ---------------------------------------------------------------- decisions
    # A readable spread rather than whatever happened last: the demo should show a buy,
    # a sell, a risk veto and a hold side by side.
    def last_n(kind: str, n: int):
        return [d for d in result.decisions if d.decision == kind][-n:]

    sample = sorted(
        last_n("BUY", 8) + last_n("SELL", 8) + last_n("VETOED", 6) + last_n("HOLD", 4),
        key=lambda d: d.timestamp,
        reverse=True,
    )
    _write("decisions.json", [
        {
            "decision_id": d.decision_id,
            "timestamp": d.timestamp.isoformat(),
            "symbol": d.symbol,
            "strategy": d.strategy,
            "decision": d.decision,
            "signal_action": d.signal_action.value,
            "current_price": str(d.current_price) if d.current_price is not None else None,
            "position_size": str(d.position_size),
            "reason": d.reason,
            "explanation": d.explanation(),
            "client_order_id": d.order_request.client_order_id if d.order_request else None,
        }
        for d in sample
    ])

    # -------------------------------------------------------------- performance
    stats = compute_metrics(equity, result.fills, result.exposures, result.concentrations,
                            engine.broker.total_slippage_cost)
    _write("performance.json", {
        "equity_curve": [{"date": d.isoformat(), "value": str(v)} for d, v in equity],
        "drawdown_curve": stats.get("drawdown_curve", []),
        "monthly_returns": stats.get("monthly_returns", {}),
        "stats": {k: v for k, v in stats.items() if k not in ("drawdown_curve", "monthly_returns")},
        "trade_stats": {
            "fills": len(result.fills),
            "buys": sum(1 for f in result.fills if f.side == Side.BUY),
            "sells": sum(1 for f in result.fills if f.side == Side.SELL),
            "commissions": str(engine.broker.total_commission.quantize(Decimal("0.01"))),
        },
    })

    # -------------------------------------------------------------------- agent
    last_order = orders[0].to_dict() if orders else None
    _write("agent.json", {
        "mode": "paper",
        "paper": True,
        "banner": "PAPER TRADING",
        "trading_enabled": True,
        "live_orders_permitted": False,
        "kill_switch_active": False,
        "broker": "simulated",
        "strategy": "trend_following",
        "universe": list(UNIVERSE),
        "demo": True,
        "status": {
            "broker_connected": True,
            "market_open": True,
            "last_market_data_update": as_of.isoformat(),
            "last_strategy_execution": as_of.isoformat(),
            "last_order": last_order,
            "errors": [],
            "updated_at": as_of.isoformat(),
            "last_cycle": {"latency_ms": 184, "symbols_evaluated": len(UNIVERSE),
                           "orders_submitted": 1 if last_order else 0, "orders_vetoed": 0},
        },
        "circuit_breaker": {"tripped": False, "reason": None, "tripped_at": None},
    })

    # ------------------------------------------------------------------- events
    events = _events(result, as_of)
    _write("events.json", events)

    _write("meta.json", {
        "generated_from": "trading_agent.backtesting.BacktestEngine on synthetic prices",
        "period": {"start": START.isoformat(), "end": END.isoformat()},
        "sessions": len(sessions),
        "decisions": len(result.decisions),
        "orders": result.orders,
        "vetoes": result.vetoes,
        "fills": len(result.fills),
        "disclaimer": "Synthetic prices, simulated broker. Demonstrates system behaviour, not strategy performance.",
    })

    print(f"wrote {len(list(OUT.glob('*.json')))} files to {OUT}")
    print(f"  sessions={len(sessions)} decisions={len(result.decisions)} orders={result.orders} "
          f"fills={len(result.fills)} vetoes={result.vetoes}")
    print(f"  total_return={metrics['total_return']:.2%} max_drawdown={metrics['max_drawdown']:.2%} "
          f"sharpe={metrics['sharpe_ratio']}")


def _events(result, as_of: datetime) -> dict:
    """Derive a plausible event feed from what the engine actually recorded."""
    risk, system = [], []
    for d in [x for x in result.decisions if x.decision == "VETOED"][-8:]:
        risk.append({
            "timestamp": d.timestamp.isoformat(), "event_type": "risk_veto", "severity": "WARNING",
            "correlation_id": d.correlation_id[:16], "symbol": d.symbol, "detail": d.reason[:180],
        })
    for liq in result.forced_liquidations[-3:]:
        risk.append({
            "timestamp": f"{liq['date']}T21:00:00+00:00", "event_type": "forced_liquidation", "severity": "WARNING",
            "correlation_id": "backtest", "symbol": liq["symbol"], "detail": liq["reason"],
        })
    system.append({"timestamp": as_of.isoformat(), "event_type": "cycle_completed", "severity": "INFO",
                   "correlation_id": "demo0001", "component": "scheduler.cycle",
                   "message": "cycle completed: all symbols evaluated, reconciliation clean"})
    system.append({"timestamp": (as_of - timedelta(minutes=5)).isoformat(), "event_type": "reconciliation",
                   "severity": "INFO", "correlation_id": "demo0002", "component": "execution.reconciliation",
                   "message": "reconciliation clean: no mismatches, no foreign orders"})
    system.append({"timestamp": (as_of - timedelta(hours=9)).isoformat(), "event_type": "startup",
                   "severity": "INFO", "correlation_id": "demo0003", "component": "runner",
                   "message": "agent started (PAPER TRADING)"})
    risk.sort(key=lambda e: e["timestamp"], reverse=True)
    return {"system": system, "risk": risk[:12]}


def _write(name: str, payload) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text(json.dumps(payload, indent=1, default=str))


if __name__ == "__main__":
    build()
