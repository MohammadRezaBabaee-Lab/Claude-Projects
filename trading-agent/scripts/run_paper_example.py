"""Paper-trading example: a few cycles against the simulated broker with synthetic prices.

Run:  python scripts/run_paper_example.py
Then: trading-agent dashboard   (with DATABASE_URL=sqlite:///./data/paper_example.db)
"""

from __future__ import annotations

import json

from trading_agent.config import load_settings
from trading_agent.logging_config import configure_logging
from trading_agent.scheduler.runner import AgentRunner


def main() -> None:
    configure_logging("INFO", "text")
    settings = load_settings(
        _env_file=None,
        trading_mode="paper",  # the only mode this example can run in
        trading_enabled=True,
        broker="simulated",
        market_data_provider="synthetic",
        universe="AAA,BBB,CCC",
        database_url="sqlite:///./data/paper_example.db",
        sim_enforce_market_hours=False,  # demo only; real paper runs respect the calendar
        cycle_interval_seconds=5,
    )
    runner = AgentRunner(settings)
    runner.startup()
    for _ in range(3):
        report = runner.run_once()
        print(
            json.dumps(
                {
                    k: v
                    for k, v in report.to_dict().items()
                    if k in ("run_id", "market_open", "orders_submitted", "orders_vetoed", "errors")
                }
            )
        )
    runner.shutdown()
    print("\nDecisions recorded:")
    for d in runner.repo.recent_decisions(limit=6):
        print(f"  {d['timestamp'][:19]} {d['decision']:<7} {d['symbol']:<5} {d['reason'][:90]}")
    print("\nOpen the dashboard with:\n  DATABASE_URL=sqlite:///./data/paper_example.db trading-agent dashboard")


if __name__ == "__main__":
    main()
