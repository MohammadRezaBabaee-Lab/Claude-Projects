import pytest

from trading_agent.config import LIVE_CONFIRMATION_PHRASE, ConfigError, load_settings


def base(**kw):
    return load_settings(_env_file=None, **kw)


def test_defaults_are_paper_and_disabled():
    s = base()
    assert s.trading_mode.value == "paper"
    assert s.trading_enabled is False
    assert s.live_orders_permitted is False
    assert s.allow_margin is False and s.allow_short_selling is False and s.allow_derivatives is False


def test_universe_parsing():
    assert base(universe="spy, qqq ,aapl").universe == ["SPY", "QQQ", "AAPL"]


@pytest.mark.parametrize(
    "kw",
    [
        {"max_portfolio_exposure": "1.5"},
        {"max_position_size": "0.9", "max_portfolio_exposure": "0.5"},
        {"max_portfolio_exposure": "0.95", "cash_reserve": "0.10"},
        {"strategy_fast_window": 50, "strategy_slow_window": 20},
        {"max_open_positions": 0},
        {"allow_derivatives": True},
        {"alpaca_paper_base_url": "http://paper-api.alpaca.markets"},
        {"notifier": "webhook"},
        {"universe": ""},
    ],
)
def test_invalid_config_fails_closed(kw):
    with pytest.raises(ConfigError):
        base(**kw)


def test_avanza_is_unsupported():
    with pytest.raises(ConfigError, match="unsupported"):
        base(broker="avanza")


def test_alpaca_requires_credentials():
    with pytest.raises(ConfigError, match="ALPACA_API_KEY_ID"):
        base(broker="alpaca")


def test_live_gate_requires_every_flag():
    creds = {"broker": "alpaca", "alpaca_api_key_id": "k", "alpaca_api_secret_key": "s"}
    with pytest.raises(ConfigError):
        base(trading_mode="live", **creds)
    with pytest.raises(ConfigError):
        base(trading_mode="live", live_trading_confirmed=True, **creds)
    with pytest.raises(ConfigError):
        base(trading_mode="live", live_trading_confirmed=True, live_trading_confirmation_phrase="yes", **creds)
    s = base(trading_mode="live", live_trading_confirmed=True, live_trading_confirmation_phrase=LIVE_CONFIRMATION_PHRASE, **creds)
    assert s.live_orders_permitted is False  # TRADING_ENABLED still false
    s = base(
        trading_mode="live",
        trading_enabled=True,
        live_trading_confirmed=True,
        live_trading_confirmation_phrase=LIVE_CONFIRMATION_PHRASE,
        **creds,
    )
    assert s.live_orders_permitted is True


def test_live_with_simulated_broker_rejected():
    with pytest.raises(ConfigError, match="simulated"):
        base(
            trading_mode="live",
            trading_enabled=True,
            live_trading_confirmed=True,
            live_trading_confirmation_phrase=LIVE_CONFIRMATION_PHRASE,
        )


def test_latent_live_confirmation_rejected_in_paper_mode():
    with pytest.raises(ConfigError, match="latent"):
        base(live_trading_confirmed=True)
    with pytest.raises(ConfigError, match="latent"):
        base(live_trading_confirmation_phrase=LIVE_CONFIRMATION_PHRASE)


def test_redacted_hides_secrets():
    s = base(broker="alpaca", alpaca_api_key_id="PKTEST", alpaca_api_secret_key="supersecret")
    red = s.redacted()
    assert red["alpaca_api_secret_key"] == "***" and red["alpaca_api_key_id"] == "***"
    assert "supersecret" not in str(red)
