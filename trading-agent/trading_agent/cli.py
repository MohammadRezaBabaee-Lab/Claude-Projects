"""Command-line entry point: ``trading-agent <command>``."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from trading_agent import __version__
from trading_agent.config import LIVE_CONFIRMATION_PHRASE, ConfigError, TradingMode, load_settings
from trading_agent.logging_config import EventLogger, configure_logging

log = EventLogger("cli")


def _settings(args: argparse.Namespace):
    try:
        overrides = {}
        if getattr(args, "env_file", None):
            overrides["_env_file"] = args.env_file
        return load_settings(**overrides)
    except ConfigError as exc:
        print(f"CONFIGURATION ERROR (refusing to start): {exc}", file=sys.stderr)
        sys.exit(2)


def cmd_validate_config(args: argparse.Namespace) -> int:
    s = _settings(args)
    print(json.dumps(s.redacted(), indent=2, default=str))
    print(f"\nmode={s.trading_mode.value} trading_enabled={s.trading_enabled} live_orders_permitted={s.live_orders_permitted}")
    return 0


def cmd_init_db(args: argparse.Namespace) -> int:
    from trading_agent.db import Database

    s = _settings(args)
    db = Database(s.database_url)
    db.create_all()
    print(f"database initialised at {s.database_url}")
    return 0


def cmd_backtest(args: argparse.Namespace) -> int:
    from trading_agent.backtesting import BacktestEngine
    from trading_agent.backtesting.report import render_text, write_report
    from trading_agent.market_data.calendar import MarketCalendar
    from trading_agent.strategies.registry import strategy_from_settings

    s = _settings(args)
    symbols = [x.strip().upper() for x in (args.symbols or ",".join(s.universe)).split(",") if x.strip()]
    start = date.fromisoformat(args.start) if args.start else None
    end = date.fromisoformat(args.end) if args.end else None
    cal = MarketCalendar(
        s.exchange_calendar, start=(start or date(2010, 1, 1)).isoformat(), end=(end or date.today()).isoformat()
    )
    history = {}
    if args.synthetic:
        from trading_agent.market_data.synthetic import generate_bars

        sessions = cal.sessions_in_range(start or date(2020, 1, 2), end or date.today())
        history = {sym: generate_bars(sym, sessions, start_price=50 + 25 * i, seed=i) for i, sym in enumerate(symbols)}
    else:
        from trading_agent.market_data.csv_provider import CsvMarketDataProvider

        provider = CsvMarketDataProvider(Path(args.data_dir or s.market_data_dir))
        for sym in symbols:
            bars = provider.get_bars(sym, start, end)
            if not bars:
                print(f"warning: no data for {sym} in {provider.data_dir}", file=sys.stderr)
                continue
            history[sym] = bars
    if not history:
        print("no historical data; use --synthetic or provide CSV files", file=sys.stderr)
        return 1
    engine = BacktestEngine(s, strategy_from_settings(s), history, calendar=cal)
    result = engine.run(start, end)
    print(render_text(result))
    if args.report:
        write_report(result, Path(args.report))
        print(f"report written to {args.report}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from trading_agent.scheduler.runner import AgentRunner

    s = _settings(args)
    if s.trading_mode == TradingMode.LIVE and not args.i_understand_live_trading:
        print(
            "TRADING_MODE=live requires the explicit flag --i-understand-live-trading on the command line.\nRefusing to start.",
            file=sys.stderr,
        )
        return 2
    runner = AgentRunner(s)
    if args.once:
        runner.startup()
        report = runner.run_once()
        runner.shutdown()
        print(json.dumps(report.to_dict(), indent=2, default=str))
        return 0
    runner.run_forever(interval_seconds=args.interval, max_cycles=args.max_cycles)
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    import uvicorn

    from trading_agent.api.app import create_app

    s = _settings(args)
    app = create_app(s)
    host = args.host or s.dashboard_host
    if host not in ("127.0.0.1", "localhost", "::1"):
        print(
            f"WARNING: dashboard bound to {host}; it has no authentication. Keep it on localhost or behind a reverse proxy.",
            file=sys.stderr,
        )
    uvicorn.run(app, host=host, port=args.port or s.dashboard_port, log_level="warning")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    from trading_agent.db import Database, Repository

    s = _settings(args)
    repo = Repository(Database(s.database_url))
    print(
        json.dumps(
            {
                "agent_status": repo.get_state("agent_status"),
                "circuit_breaker": repo.get_state("circuit_breaker"),
                "last_run": repo.last_run(),
            },
            indent=2,
            default=str,
        )
    )
    return 0


def cmd_reset_breaker(args: argparse.Namespace) -> int:
    from trading_agent.db import Database, Repository
    from trading_agent.risk.circuit_breaker import CircuitBreaker

    s = _settings(args)
    repo = Repository(Database(s.database_url))
    breaker = CircuitBreaker(1, 1, 1, 1, state=repo.get_state(CircuitBreaker.STATE_KEY))
    was = breaker.status()
    breaker.reset(args.reason)
    repo.set_state(CircuitBreaker.STATE_KEY, breaker.dump())
    repo.record_system_event("cli", "circuit_breaker_reset", "WARNING", f"manual reset: {args.reason}", payload={"previous": was})
    print(f"circuit breaker reset (was: {was})")
    return 0


def cmd_confirm_live(_args: argparse.Namespace) -> int:
    print(
        "Live trading is NOT enabled by this command. It only prints what you must do by hand.\n\n"
        "Before going live: complete the checklist in docs/live_trading.md (backtest reviewed,\n"
        ">= 4 weeks paper trading, alerts verified, risk limits reviewed).\n\n"
        "Then set ALL of the following in your .env (each is required):\n"
        "  BROKER=alpaca                      (a broker with an official API; never 'simulated')\n"
        "  TRADING_MODE=live\n"
        "  TRADING_ENABLED=true\n"
        "  LIVE_TRADING_CONFIRMED=true\n"
        f"  LIVE_TRADING_CONFIRMATION_PHRASE={LIVE_CONFIRMATION_PHRASE}\n"
        "  ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY = your LIVE keys (paper keys will not work)\n\n"
        "and start the agent with:\n"
        "  trading-agent run --i-understand-live-trading\n\n"
        "Any missing element makes the agent refuse to start. Remove LIVE_TRADING_CONFIRMED and\n"
        "the phrase again when returning to paper mode; the config validator rejects a latent\n"
        "confirmation while TRADING_MODE=paper."
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="trading-agent", description="Automated trading agent (paper by default)")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--env-file", help="path to .env file (default ./.env)")
    p.add_argument("--log-level", default=None)
    p.add_argument("--log-format", choices=["json", "text"], default=None)
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("validate-config", help="validate configuration and print it (secrets redacted)").set_defaults(
        func=cmd_validate_config
    )
    sub.add_parser("init-db", help="create database tables (dev; use alembic in production)").set_defaults(func=cmd_init_db)

    b = sub.add_parser("backtest", help="run a backtest")
    b.add_argument("--symbols", help="comma-separated symbols (default UNIVERSE)")
    b.add_argument("--start")
    b.add_argument("--end")
    b.add_argument("--data-dir")
    b.add_argument("--synthetic", action="store_true", help="use deterministic synthetic prices")
    b.add_argument("--report", help="write JSON report to this path")
    b.set_defaults(func=cmd_backtest)

    r = sub.add_parser("run", help="run the agent loop (paper by default)")
    r.add_argument("--once", action="store_true", help="run a single cycle and exit")
    r.add_argument("--interval", type=int, default=None)
    r.add_argument("--max-cycles", type=int, default=None)
    r.add_argument("--i-understand-live-trading", action="store_true", help="required when TRADING_MODE=live")
    r.set_defaults(func=cmd_run)
    pp = sub.add_parser("paper", help="alias for 'run' (paper mode)")
    pp.add_argument("--once", action="store_true")
    pp.add_argument("--interval", type=int, default=None)
    pp.add_argument("--max-cycles", type=int, default=None)
    pp.set_defaults(func=cmd_run, i_understand_live_trading=False)

    d = sub.add_parser("dashboard", help="serve the local web dashboard")
    d.add_argument("--host")
    d.add_argument("--port", type=int)
    d.set_defaults(func=cmd_dashboard)

    sub.add_parser("status", help="print agent status from the database").set_defaults(func=cmd_status)
    rb = sub.add_parser("reset-breaker", help="manually reset the circuit breaker")
    rb.add_argument("--reason", required=True)
    rb.set_defaults(func=cmd_reset_breaker)
    sub.add_parser("confirm-live", help="print the live-trading enablement procedure (does not enable anything)").set_defaults(
        func=cmd_confirm_live
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    fmt = args.log_format or ("text" if sys.stdout.isatty() else "json")
    configure_logging(args.log_level or "INFO", fmt)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
