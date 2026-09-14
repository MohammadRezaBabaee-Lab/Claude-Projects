from datetime import timedelta
from decimal import Decimal

from fastapi.testclient import TestClient

from tests.conftest import FakeCalendar
from trading_agent.api.app import create_app
from trading_agent.domain import OrderStatus
from trading_agent.scheduler.runner import AgentRunner


def test_cycle_submits_order_on_signal_and_records_everything(cycle, repo, broker, notifier):
    report = cycle.run_once()
    assert report.market_open and report.broker_connected and report.data_ok
    assert report.symbols_evaluated == 2 and report.orders_submitted == 1 and not report.errors
    orders = repo.orders()
    assert len(orders) == 1 and orders[0].symbol == "AAA" and orders[0].status == OrderStatus.FILLED
    decisions = repo.recent_decisions()
    assert {d["symbol"] for d in decisions} == {"AAA", "BBB"}
    buy = next(d for d in decisions if d["decision"] == "BUY")
    assert "Signal:" in buy["explanation"] and "Risk:" in buy["explanation"] and "Position sizing:" in buy["explanation"]
    assert repo.snapshots()[-1]["n_positions"] == 1
    assert repo.last_run()["status"] == "ok"
    status = repo.get_state("agent_status")
    assert status["mode"] == "paper" and status["last_order"]["symbol"] == "AAA"
    # second cycle: already holding -> HOLD, no new order
    report2 = cycle.run_once()
    assert report2.orders_submitted == 0 and len(repo.orders()) == 1


def test_kill_switch_monitors_but_never_orders(cycle, oms, repo, settings):
    oms.trading_enabled = False
    settings.trading_enabled = False
    report = cycle.run_once()
    assert report.orders_submitted == 0 and repo.orders() == []
    assert repo.snapshots() and report.portfolio is not None
    assert any(d["decision"] == "VETOED" for d in repo.recent_decisions())


def test_market_closed_no_orders(cycle, calendar, repo):
    calendar.open = False
    report = cycle.run_once()
    assert not report.market_open and report.orders_submitted == 0 and repo.orders() == []


def test_api_endpoints(settings, db, cycle):
    cycle.run_once()
    client = TestClient(create_app(settings, db))
    agent = client.get("/api/agent").json()
    assert agent["banner"] == "PAPER TRADING" and agent["paper"] is True and agent["config"]["alpaca_api_secret_key"] == ""
    assert client.get("/").status_code == 200 and "PAPER TRADING" in client.get("/").text
    assert client.get("/api/positions").json()[0]["symbol"] == "AAA"
    orders = client.get("/api/orders").json()
    assert orders["counts"]["filled"] == 1
    perf = client.get("/api/performance").json()
    assert perf["equity_curve"] and "trade_stats" in perf
    assert client.get("/api/decisions").json()
    assert "trading_cycles" in client.get("/metrics").text
    assert client.get("/healthz").json()["ok"]


def test_process_restart_restores_state(tmp_path):
    from trading_agent.config import load_settings

    settings = load_settings(
        _env_file=None,
        trading_enabled=True,
        universe="AAA,BBB",
        market_data_provider="synthetic",
        database_url=f"sqlite:///{tmp_path}/restart.db",
        sim_enforce_market_hours=False,
    )
    r1 = AgentRunner(settings)
    r1.calendar = FakeCalendar(True)
    r1.cycle.cal = r1.calendar
    r1.startup()
    r1.breaker.trip("simulated outage")
    r1.broker.cash = Decimal("12345")
    r1.run_once()
    r1.shutdown()
    r2 = AgentRunner(settings)  # fresh process
    assert r2.breaker.tripped and "outage" in r2.breaker.trip_reason
    assert r2.broker.cash == Decimal("12345")
    assert r2.portfolio.peak_value is not None
    r2.startup()
    assert r2.repo.recent_events(5, "system")[0]["event_type"] == "startup"


def test_runner_loop_runs_bounded_cycles(tmp_path):
    from trading_agent.config import load_settings

    settings = load_settings(
        _env_file=None,
        universe="AAA",
        market_data_provider="synthetic",
        database_url=f"sqlite:///{tmp_path}/loop.db",
        cycle_interval_seconds=5,
    )
    runner = AgentRunner(settings)
    runner._stop.wait = lambda t: None  # do not sleep in tests
    runner.run_forever(interval_seconds=5, max_cycles=2)
    assert runner.repo.last_run() is not None
    assert runner.status()["mode"] == "paper"
    assert runner.calendar.next_open(
        runner.calendar.next_open(__import__("trading_agent.domain", fromlist=["utcnow"]).utcnow()) + timedelta(minutes=1)
    )
