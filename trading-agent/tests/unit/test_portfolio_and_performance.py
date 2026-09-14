from datetime import date, timedelta
from decimal import Decimal

from tests.conftest import OPEN_TIME
from trading_agent.domain import Account, Fill, Order, OrderStatus, OrderType, Position, Side
from trading_agent.portfolio.manager import PortfolioManager
from trading_agent.portfolio.performance import compute_metrics, monthly_returns, round_trips


def _acct(cash, bp=None):
    return Account("A", "USD", Decimal(cash), Decimal(bp if bp is not None else cash), Decimal(cash), Decimal(cash), OPEN_TIME)


def test_capital_buckets():
    pm = PortfolioManager(Decimal("0.10"))
    pos = [Position("AAA", Decimal("100"), Decimal("90"), Decimal("100"), OPEN_TIME)]
    open_buy = Order(
        "c",
        "BBB",
        Side.BUY,
        Decimal("10"),
        OrderType.LIMIT,
        OrderStatus.ACCEPTED,
        OPEN_TIME,
        OPEN_TIME,
        limit_price=Decimal("50"),
    )
    st = pm.build(_acct("50000"), pos, [open_buy], OPEN_TIME.date())
    assert st.invested_capital == Decimal("10000.00")
    assert st.portfolio_value == Decimal("60000.00")
    assert st.reserved_cash == Decimal("6500.00")  # 500 committed + 6000 reserve
    assert st.buying_power == Decimal("43500.00")
    assert st.exposure == Decimal("0.1667")
    assert st.weights["AAA"] == Decimal("0.1667")


def test_buying_power_never_exceeds_broker():
    pm = PortfolioManager(Decimal("0"))
    st = pm.build(_acct("50000", bp="1000"), [], [], OPEN_TIME.date())
    assert st.buying_power == Decimal("1000.00")


def test_peak_drawdown_daily_pnl_and_rollover():
    pm = PortfolioManager(Decimal("0"))
    d = OPEN_TIME.date()
    st = pm.build(_acct("100000"), [], [], d)
    assert st.drawdown == 0 and st.daily_pnl == 0 and st.total_pnl == 0
    st = pm.build(_acct("110000"), [], [], d)
    assert st.peak_value == Decimal("110000.00") and st.daily_pnl == Decimal("10000.00")
    st = pm.build(_acct("99000"), [], [], d + timedelta(days=1))
    assert st.daily_pnl == 0  # new day resets day-start
    assert st.drawdown == Decimal("0.1000") and st.total_pnl == Decimal("-1000.00")
    restored = PortfolioManager(Decimal("0"), state=pm.dump())
    assert restored.peak_value == pm.peak_value and restored.initial_value == pm.initial_value


def _fill(sym, side, qty, px, day, comm="1"):
    return Fill(
        f"f{sym}{side.value}{day}", "o", sym, side, Decimal(qty), Decimal(px), Decimal(comm), OPEN_TIME + timedelta(days=day)
    )


def test_round_trips_fifo():
    fills = [_fill("A", Side.BUY, "10", "100", 0), _fill("A", Side.BUY, "10", "110", 1), _fill("A", Side.SELL, "15", "120", 2)]
    trips = round_trips(fills)
    assert [t.quantity for t in trips] == [Decimal("10"), Decimal("5")]
    assert trips[0].entry_price == Decimal("100") and trips[1].entry_price == Decimal("110")
    assert trips[0].pnl + trips[1].pnl < Decimal("10") * 20 + Decimal("5") * 10  # commissions deducted


def test_metrics_on_known_curve():
    start = date(2025, 1, 1)
    equity = [(start + timedelta(days=i), Decimal(100000) * (Decimal("1.001") ** i)) for i in range(365)]
    fills = [
        _fill("A", Side.BUY, "10", "100", 0),
        _fill("A", Side.SELL, "10", "120", 5),
        _fill("B", Side.BUY, "10", "100", 6),
        _fill("B", Side.SELL, "10", "90", 9),
    ]
    m = compute_metrics(equity, fills, [Decimal("0.5")] * 365, [Decimal("0.1")] * 365, Decimal("12.5"))
    assert m["total_return"] > 0.4 and m["cagr"] > 0.4
    assert m["max_drawdown"] == 0
    assert m["n_round_trips"] == 2 and m["win_rate"] == 0.5
    assert m["profit_factor"] > 1
    assert m["transaction_costs"] == 4.0 and m["slippage_cost"] == 12.5
    assert m["avg_exposure"] == 0.5 and m["peak_max_position_weight"] == 0.1
    assert m["sharpe_ratio"] > 0 and m["annual_volatility"] >= 0
    assert len(m["monthly_returns"]) == 12


def test_monthly_returns():
    eq = [(date(2025, 1, 1), Decimal(100)), (date(2025, 1, 31), Decimal(110)), (date(2025, 2, 28), Decimal(99))]
    mr = monthly_returns(eq)
    assert mr["2025-01"] == 0.1 and mr["2025-02"] == -0.1
