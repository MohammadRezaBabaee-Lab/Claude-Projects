from __future__ import annotations

import json
from pathlib import Path

from trading_agent.backtesting.engine import BacktestResult


def render_text(result: BacktestResult) -> str:
    m = result.metrics
    rows = [
        ("Period", f"{m.get('start_date')} -> {m.get('end_date')}"),
        ("Start / end value", f"{m.get('start_value'):,.2f} / {m.get('end_value'):,.2f}"),
        ("Total return", f"{m.get('total_return', 0):.2%}"),
        ("CAGR", f"{m.get('cagr', 0):.2%}"),
        ("Annual volatility", f"{m.get('annual_volatility', 0):.2%}"),
        ("Sharpe ratio", f"{m.get('sharpe_ratio', 0):.2f}"),
        ("Sortino ratio", f"{m.get('sortino_ratio') if m.get('sortino_ratio') is not None else 'n/a'}"),
        ("Max drawdown", f"{m.get('max_drawdown', 0):.2%}"),
        ("Round trips / fills", f"{m.get('n_round_trips')} / {m.get('n_fills')}"),
        ("Win rate", f"{m.get('win_rate'):.1%}" if m.get("win_rate") is not None else "n/a"),
        ("Profit factor", f"{m.get('profit_factor')}" if m.get("profit_factor") is not None else "n/a"),
        ("Transaction costs", f"{m.get('transaction_costs', 0):,.2f}"),
        ("Slippage cost", f"{m.get('slippage_cost', 0):,.2f}"),
        ("Avg / max exposure", f"{m.get('avg_exposure')} / {m.get('max_exposure')}"),
        ("Avg / peak max position weight", f"{m.get('avg_max_position_weight')} / {m.get('peak_max_position_weight')}"),
        ("Decisions / orders / vetoes", f"{len(result.decisions)} / {result.orders} / {result.vetoes}"),
        ("Forced liquidations (delisted)", str(len(result.forced_liquidations))),
    ]
    width = max(len(r[0]) for r in rows) + 2
    lines = ["Backtest results", "=" * 60] + [f"{k:<{width}}{v}" for k, v in rows]
    lines.append("")
    lines.append("This is a simulation. Past performance does not guarantee future results.")
    return "\n".join(lines)


def write_report(result: BacktestResult, path: Path) -> None:
    payload = {
        "metrics": result.metrics,
        "equity_curve": [{"date": d.isoformat(), "value": str(v)} for d, v in result.equity_curve],
        "fills": [
            {
                "timestamp": f.timestamp.isoformat(),
                "symbol": f.symbol,
                "side": f.side.value,
                "quantity": str(f.quantity),
                "price": str(f.price),
                "commission": str(f.commission),
            }
            for f in result.fills
        ],
        "forced_liquidations": result.forced_liquidations,
        "decisions_sample": [d.to_dict() for d in result.decisions if d.decision != "HOLD"][:200],
        "summary": result.summary(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str))
