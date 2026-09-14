"""Performance statistics for backtests and live/paper equity curves."""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from trading_agent.domain import Fill, Side


@dataclass(slots=True)
class RoundTrip:
    symbol: str
    entry_date: date
    exit_date: date
    quantity: Decimal
    entry_price: Decimal
    exit_price: Decimal
    pnl: Decimal  # net of commissions
    commissions: Decimal


def round_trips(fills: list[Fill]) -> list[RoundTrip]:
    """FIFO matching of buys and sells per symbol into closed round trips."""
    lots: dict[str, list[list]] = defaultdict(list)  # symbol -> [qty, price, date, commission_per_share]
    trips: list[RoundTrip] = []
    for f in sorted(fills, key=lambda x: x.timestamp):
        if f.side == Side.BUY:
            cps = f.commission / f.quantity if f.quantity else Decimal("0")
            lots[f.symbol].append([f.quantity, f.price, f.timestamp.date(), cps])
            continue
        remaining = f.quantity
        sell_cps = f.commission / f.quantity if f.quantity else Decimal("0")
        while remaining > 0 and lots[f.symbol]:
            lot = lots[f.symbol][0]
            take = min(remaining, lot[0])
            comm = take * (lot[3] + sell_cps)
            pnl = take * (f.price - lot[1]) - comm
            trips.append(RoundTrip(f.symbol, lot[2], f.timestamp.date(), take, lot[1], f.price, pnl, comm))
            lot[0] -= take
            remaining -= take
            if lot[0] == 0:
                lots[f.symbol].pop(0)
    return trips


def compute_metrics(
    equity: list[tuple[date, Decimal]],
    fills: list[Fill],
    exposures: list[Decimal] | None = None,
    concentrations: list[Decimal] | None = None,
    total_slippage: Decimal = Decimal("0"),
    risk_free_rate: float = 0.0,
    periods_per_year: int = 252,
) -> dict[str, Any]:
    if len(equity) < 2:
        return {"error": "not enough equity points"}
    values = [float(v) for _, v in equity]
    start_value, end_value = values[0], values[-1]
    rets = [values[i] / values[i - 1] - 1 for i in range(1, len(values)) if values[i - 1] > 0]
    n = len(rets)
    total_return = end_value / start_value - 1 if start_value > 0 else 0.0
    days = (equity[-1][0] - equity[0][0]).days
    years = days / 365.25 if days > 0 else n / periods_per_year
    cagr = (end_value / start_value) ** (1 / years) - 1 if years > 0 and start_value > 0 and end_value > 0 else 0.0
    mean = sum(rets) / n if n else 0.0
    var = sum((r - mean) ** 2 for r in rets) / (n - 1) if n > 1 else 0.0
    vol = math.sqrt(var) * math.sqrt(periods_per_year)
    rf_daily = risk_free_rate / periods_per_year
    excess = [r - rf_daily for r in rets]
    sharpe = (sum(excess) / n) / math.sqrt(var) * math.sqrt(periods_per_year) if n > 1 and var > 0 else 0.0
    downside = [min(0.0, r - rf_daily) for r in rets]
    dvar = sum(d * d for d in downside) / n if n else 0.0
    sortino = (sum(excess) / n) / math.sqrt(dvar) * math.sqrt(periods_per_year) if n and dvar > 0 else 0.0
    peak, max_dd = values[0], 0.0
    dd_curve = []
    for v in values:
        peak = max(peak, v)
        dd = (peak - v) / peak if peak > 0 else 0.0
        dd_curve.append(dd)
        max_dd = max(max_dd, dd)
    trips = round_trips(fills)
    wins = [t for t in trips if t.pnl > 0]
    losses = [t for t in trips if t.pnl < 0]
    gross_profit = float(sum((t.pnl for t in wins), Decimal("0")))
    gross_loss = -float(sum((t.pnl for t in losses), Decimal("0")))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else (math.inf if gross_profit > 0 else 0.0)
    commissions = float(sum((f.commission for f in fills), Decimal("0")))
    monthly = monthly_returns(equity)
    return {
        "start_date": equity[0][0].isoformat(),
        "end_date": equity[-1][0].isoformat(),
        "start_value": round(start_value, 2),
        "end_value": round(end_value, 2),
        "total_return": round(total_return, 6),
        "cagr": round(cagr, 6),
        "annual_volatility": round(vol, 6),
        "sharpe_ratio": round(sharpe, 4),
        "sortino_ratio": round(sortino, 4) if math.isfinite(sortino) else None,
        "max_drawdown": round(max_dd, 6),
        "n_round_trips": len(trips),
        "n_fills": len(fills),
        "win_rate": round(len(wins) / len(trips), 4) if trips else None,
        "profit_factor": round(profit_factor, 4) if math.isfinite(profit_factor) else None,
        "avg_trade_pnl": round(float(sum((t.pnl for t in trips), Decimal("0"))) / len(trips), 2) if trips else None,
        "transaction_costs": round(commissions, 2),
        "slippage_cost": round(float(total_slippage), 2),
        "avg_exposure": round(float(sum(exposures) / len(exposures)), 4) if exposures else None,
        "max_exposure": round(float(max(exposures)), 4) if exposures else None,
        "avg_max_position_weight": round(float(sum(concentrations) / len(concentrations)), 4) if concentrations else None,
        "peak_max_position_weight": round(float(max(concentrations)), 4) if concentrations else None,
        "monthly_returns": monthly,
        "drawdown_curve": [round(d, 6) for d in dd_curve],
    }


def monthly_returns(equity: list[tuple[date, Decimal]]) -> dict[str, float]:
    by_month: dict[str, list[float]] = {}
    for d, v in equity:
        by_month.setdefault(d.strftime("%Y-%m"), []).append(float(v))
    out: dict[str, float] = {}
    prev_close: float | None = None
    for month, vals in by_month.items():
        start = prev_close if prev_close is not None else vals[0]
        out[month] = round(vals[-1] / start - 1, 6) if start > 0 else 0.0
        prev_close = vals[-1]
    return out
