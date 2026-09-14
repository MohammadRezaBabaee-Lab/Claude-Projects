from datetime import timedelta
from decimal import Decimal

from tests.conftest import OPEN_TIME
from trading_agent.domain import Account, Instrument, Order, OrderRequest, OrderStatus, OrderType, Position, Side
from trading_agent.portfolio.manager import PortfolioManager
from trading_agent.risk.manager import RiskContext, RiskManager
from trading_agent.risk.sizing import PositionSizer


def _state(settings, cash="100000", positions=None, open_orders=None, pm=None):
    pm = pm or PortfolioManager(settings.cash_reserve)
    acct = Account("A", "USD", Decimal(cash), Decimal(cash), Decimal(cash), Decimal(cash), OPEN_TIME)
    return pm.build(acct, positions or [], open_orders or [], OPEN_TIME.date(), OPEN_TIME)


def _sizer(settings):
    return PositionSizer(
        settings.max_risk_per_trade,
        settings.max_position_size,
        settings.max_order_value,
        settings.max_portfolio_exposure,
        settings.stop_loss_pct,
        settings.strategy_atr_stop_multiplier,
    )


def test_sizing_risk_budget_math(settings):
    state = _state(settings)
    # risk budget 1% of 100k = 1000; ATR 2 * 3 = 6 per share -> 166 shares; pct stop 8% of 100 = 8 > 6 -> 125 shares
    res = _sizer(settings).size(Decimal("100"), state, Decimal("2"))
    assert res.detail["risk_budget"] == Decimal("1000.00")
    assert res.detail["stop_distance_used"] == Decimal("8.00")
    assert res.quantity == Decimal("100")  # capped by max position size 10% = 10,000 / 100
    assert res.detail["cap_position_size"] == Decimal("100")


def test_sizing_capped_by_order_value_and_buying_power(settings):
    state = _state(settings, cash="5000")
    res = _sizer(settings).size(Decimal("10"), state, Decimal("0.1"))
    assert res.quantity * Decimal("10") <= state.buying_power
    assert res.quantity <= settings.max_order_value / Decimal("10")


def test_sizing_zero_for_bad_price(settings):
    assert _sizer(settings).size(Decimal("0"), _state(settings), Decimal("1")).quantity == 0


def _ctx(**kw):
    base = dict(
        trading_enabled=True,
        circuit_breaker_tripped=False,
        market_open=True,
        data_age_seconds=10.0,
        orders_today=0,
        orders_today_symbol=0,
        instrument=Instrument("AAA", sector="Tech"),
        now=OPEN_TIME,
    )
    base.update(kw)
    return RiskContext(**base)


def _req(qty="50", side=Side.BUY, symbol="AAA"):
    return OrderRequest(symbol=symbol, side=side, quantity=Decimal(qty), strategy="t")


def _failed(checks):
    return {c.name for c in checks if not c.passed}


def test_all_checks_pass_for_sane_buy(settings):
    rm = RiskManager(settings)
    checks = rm.evaluate(_req("50"), Decimal("100"), _state(settings), _ctx())
    assert RiskManager.all_passed(checks), _failed(checks)
    assert {c.name for c in checks} >= {
        "kill_switch",
        "circuit_breaker",
        "market_open",
        "data_freshness",
        "max_portfolio_exposure",
        "max_position_size",
        "buying_power",
        "cash_reserve",
        "max_open_positions",
        "sector_concentration",
    }


def test_kill_switch_breaker_market_and_stale_data_veto(settings):
    rm = RiskManager(settings)
    st = _state(settings)
    assert "kill_switch" in _failed(rm.evaluate(_req(), Decimal("100"), st, _ctx(trading_enabled=False)))
    assert "circuit_breaker" in _failed(rm.evaluate(_req(), Decimal("100"), st, _ctx(circuit_breaker_tripped=True)))
    assert "market_open" in _failed(rm.evaluate(_req(), Decimal("100"), st, _ctx(market_open=False)))
    assert "data_freshness" in _failed(rm.evaluate(_req(), Decimal("100"), st, _ctx(data_age_seconds=5000.0)))
    assert "data_freshness" in _failed(rm.evaluate(_req(), Decimal("100"), st, _ctx(data_age_seconds=None)))


def test_position_size_and_exposure_limits(settings):
    rm = RiskManager(settings)
    st = _state(settings)
    f = _failed(rm.evaluate(_req("150"), Decimal("100"), st, _ctx()))  # 15% > 10%
    assert "max_position_size" in f and "max_order_value" not in f
    pos = [Position("BBB", Decimal("1300"), Decimal("50"), Decimal("50"), OPEN_TIME)]  # 65k invested
    st2 = _state(settings, cash="35000", positions=pos)
    f = _failed(rm.evaluate(_req("90"), Decimal("100"), st2, _ctx()))  # exposure 74% > 70%
    assert "max_portfolio_exposure" in f


def test_order_value_and_frequency_limits(settings):
    rm = RiskManager(settings)
    st = _state(settings, cash="1000000")
    assert "max_order_value" in _failed(rm.evaluate(_req("300"), Decimal("100"), st, _ctx()))
    assert "max_orders_per_day" in _failed(
        rm.evaluate(_req("10"), Decimal("100"), st, _ctx(orders_today=settings.max_orders_per_day))
    )
    assert "max_orders_per_symbol_per_day" in _failed(rm.evaluate(_req("10"), Decimal("100"), st, _ctx(orders_today_symbol=2)))


def test_daily_loss_and_drawdown_limits(settings):
    rm = RiskManager(settings)
    pm = PortfolioManager(settings.cash_reserve)
    _state(settings, cash="100000", pm=pm)  # sets peak & day start
    st = _state(settings, cash="90000", pm=pm)  # -10% today
    f = _failed(rm.evaluate(_req("10"), Decimal("100"), st, _ctx()))
    assert "max_daily_loss" in f
    st = _state(settings, cash="80000", pm=pm)  # drawdown 20% > 15%
    assert "max_drawdown" in _failed(rm.evaluate(_req("10"), Decimal("100"), st, _ctx()))


def test_buying_power_cash_reserve_and_max_positions(settings):
    rm = RiskManager(settings)
    st = _state(settings, cash="10000")
    # buying power = cash - 10% reserve = 9000; order 9500 fails buying power and cash reserve
    f = _failed(rm.evaluate(_req("95"), Decimal("100"), st, _ctx()))
    assert "buying_power" in f and "cash_reserve" in f
    positions = [
        Position(f"S{i}", Decimal("1"), Decimal("1"), Decimal("1"), OPEN_TIME) for i in range(settings.max_open_positions)
    ]
    st = _state(settings, cash="100000", positions=positions)
    assert "max_open_positions" in _failed(rm.evaluate(_req("10"), Decimal("100"), st, _ctx()))


def test_sector_concentration_and_leveraged(settings):
    rm = RiskManager(settings)
    pos = [Position("BBB", Decimal("500"), Decimal("50"), Decimal("50"), OPEN_TIME, sector="Tech")]  # 25k = 25%
    st = _state(settings, cash="75000", positions=pos)
    f = _failed(rm.evaluate(_req("80"), Decimal("100"), st, _ctx()))  # +8% -> 33% > 30%
    assert "sector_concentration" in f
    f = _failed(
        rm.evaluate(
            _req("10", symbol="LEV"), Decimal("100"), _state(settings), _ctx(instrument=Instrument("LEV", leveraged=True))
        )
    )
    assert "no_leveraged_etfs" in f
    f = _failed(rm.evaluate(_req("10"), Decimal("100"), _state(settings), _ctx(instrument=None)))
    assert "symbol_tradable" in f


def test_short_selling_and_equivalent_order(settings):
    rm = RiskManager(settings)
    st = _state(settings)
    assert "no_short_selling" in _failed(rm.evaluate(_req("10", side=Side.SELL), Decimal("100"), st, _ctx()))
    pos = [Position("AAA", Decimal("10"), Decimal("90"), Decimal("100"), OPEN_TIME)]
    st = _state(settings, positions=pos)
    assert RiskManager.all_passed(rm.evaluate(_req("10", side=Side.SELL), Decimal("100"), st, _ctx()))
    open_order = Order("c1", "AAA", Side.BUY, Decimal("5"), OrderType.MARKET, OrderStatus.ACCEPTED, OPEN_TIME, OPEN_TIME)
    st = _state(settings, open_orders=[open_order])
    assert "no_equivalent_open_order" in _failed(rm.evaluate(_req("10"), Decimal("100"), st, _ctx()))


def test_abnormal_price_check(settings):
    rm = RiskManager(settings)
    f = _failed(rm.evaluate(_req("10"), Decimal("200"), _state(settings), _ctx(price_reference=Decimal("100"))))
    assert "abnormal_price" in f


def test_risk_context_timestamp_unused_but_valid(settings):
    ctx = _ctx(now=OPEN_TIME + timedelta(minutes=1))
    assert ctx.now > OPEN_TIME
