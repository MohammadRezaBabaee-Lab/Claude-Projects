"""Configuration loading and validation.

All settings come from environment variables (or a local ``.env`` file that is
git-ignored). Configuration is validated at startup and the process refuses to
start ("fails closed") if anything is inconsistent.

The live-trading gate
---------------------
Real orders are only ever permitted when *all* of the following hold:

* ``TRADING_MODE=live``
* ``TRADING_ENABLED=true``
* ``LIVE_TRADING_CONFIRMED=true``
* ``LIVE_TRADING_CONFIRMATION_PHRASE`` equals :data:`LIVE_CONFIRMATION_PHRASE`
* the selected broker supports live trading (``simulated`` never does)

There is deliberately no code path that flips paper mode to live mode at
runtime; the mode is fixed for the lifetime of the process.
"""

from __future__ import annotations

import os
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Annotated, Any

from pydantic import Field, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

LIVE_CONFIRMATION_PHRASE = "I UNDERSTAND THAT LIVE TRADING RISKS REAL MONEY"


class ConfigError(RuntimeError):
    """Raised when configuration is invalid. The agent must not start."""


class TradingMode(str, Enum):
    PAPER = "paper"
    LIVE = "live"


class BrokerKind(str, Enum):
    SIMULATED = "simulated"
    ALPACA = "alpaca"
    AVANZA = "avanza"  # unsupported: no official API


class MarketDataKind(str, Enum):
    CSV = "csv"
    ALPACA = "alpaca"
    SYNTHETIC = "synthetic"


class NotifierKind(str, Enum):
    LOG = "log"
    WEBHOOK = "webhook"
    NONE = "none"


def _split_csv(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list | tuple):
        return [str(v).strip().upper() for v in value if str(v).strip()]
    return [v.strip().upper() for v in str(value).split(",") if v.strip()]


class Settings(BaseSettings):
    """Validated application settings."""

    model_config = SettingsConfigDict(
        env_file=os.environ.get("TRADING_AGENT_ENV_FILE", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- mode / safety -------------------------------------------------
    trading_mode: TradingMode = TradingMode.PAPER
    trading_enabled: bool = False
    live_trading_confirmed: bool = False
    live_trading_confirmation_phrase: str = ""

    # --- broker ----------------------------------------------------------
    broker: BrokerKind = BrokerKind.SIMULATED
    alpaca_api_key_id: str = ""
    alpaca_api_secret_key: str = ""
    alpaca_paper_base_url: str = "https://paper-api.alpaca.markets"
    alpaca_live_base_url: str = "https://api.alpaca.markets"
    alpaca_data_base_url: str = "https://data.alpaca.markets"
    alpaca_data_feed: str = "iex"
    broker_timeout_seconds: float = 10.0
    broker_max_retries: int = 3

    # --- market data -----------------------------------------------------
    market_data_provider: MarketDataKind = MarketDataKind.CSV
    market_data_dir: Path = Path("./data/history")
    max_data_age_seconds: int = 900
    exchange_calendar: str = "XNYS"
    local_timezone: str = "Europe/Stockholm"

    # --- universe / strategy ----------------------------------------------
    universe: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["SPY", "QQQ"])
    strategy: str = "trend_following"
    strategy_fast_window: int = 20
    strategy_slow_window: int = 50
    strategy_atr_window: int = 14
    strategy_atr_stop_multiplier: Decimal = Decimal("3.0")

    # --- risk --------------------------------------------------------------
    max_portfolio_exposure: Decimal = Decimal("0.70")
    max_position_size: Decimal = Decimal("0.10")
    max_risk_per_trade: Decimal = Decimal("0.01")
    max_daily_loss: Decimal = Decimal("0.02")
    max_drawdown: Decimal = Decimal("0.15")
    max_open_positions: int = 10
    cash_reserve: Decimal = Decimal("0.10")
    max_order_value: Decimal = Decimal("10000")
    max_orders_per_day: int = 20
    max_orders_per_symbol_per_day: int = 2
    max_sector_concentration: Decimal = Decimal("0.30")
    stop_loss_pct: Decimal = Decimal("0.08")
    abnormal_price_move_pct: Decimal = Decimal("0.25")
    allow_margin: bool = False
    allow_derivatives: bool = False
    allow_short_selling: bool = False
    allow_leveraged_etfs: bool = False

    circuit_breaker_max_api_errors: int = 5
    circuit_breaker_max_rejected_orders: int = 3
    circuit_breaker_max_auth_failures: int = 2
    circuit_breaker_window_seconds: int = 900

    # --- simulated broker -------------------------------------------------
    sim_initial_cash: Decimal = Decimal("100000")
    sim_currency: str = "USD"
    sim_commission_per_order: Decimal = Decimal("0")
    sim_commission_pct: Decimal = Decimal("0.0005")
    sim_slippage_bps: Decimal = Decimal("5")
    sim_partial_fill_probability: float = 0.0
    sim_enforce_market_hours: bool = True

    # --- persistence / runtime -------------------------------------------
    database_url: str = "sqlite:///./data/trading.db"
    cycle_interval_seconds: int = 300
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8000
    notifier: NotifierKind = NotifierKind.LOG
    notification_webhook_url: str = ""
    log_level: str = "INFO"
    log_format: str = "json"

    # ------------------------------------------------------------------
    @field_validator("universe", mode="before")
    @classmethod
    def _parse_universe(cls, v: Any) -> list[str]:
        return _split_csv(v)

    @field_validator("alpaca_paper_base_url", "alpaca_live_base_url", "alpaca_data_base_url")
    @classmethod
    def _https_only(cls, v: str) -> str:
        if not v.startswith("https://"):
            raise ValueError("broker URLs must use https://")
        return v.rstrip("/")

    @field_validator("notification_webhook_url")
    @classmethod
    def _webhook_https(cls, v: str) -> str:
        if v and not v.startswith("https://"):
            raise ValueError("NOTIFICATION_WEBHOOK_URL must use https://")
        return v

    @model_validator(mode="after")
    def _validate(self) -> Settings:
        problems: list[str] = []

        def frac(name: str, lo: Decimal = Decimal("0"), hi: Decimal = Decimal("1")) -> None:
            val = getattr(self, name)
            if not (lo <= val <= hi):
                problems.append(f"{name.upper()} must be between {lo} and {hi}, got {val}")

        for name in (
            "max_portfolio_exposure",
            "max_position_size",
            "max_risk_per_trade",
            "max_daily_loss",
            "max_drawdown",
            "cash_reserve",
            "max_sector_concentration",
            "stop_loss_pct",
            "abnormal_price_move_pct",
        ):
            frac(name)
        if self.max_position_size > self.max_portfolio_exposure:
            problems.append("MAX_POSITION_SIZE cannot exceed MAX_PORTFOLIO_EXPOSURE")
        if self.max_portfolio_exposure + self.cash_reserve > Decimal("1"):
            problems.append("MAX_PORTFOLIO_EXPOSURE + CASH_RESERVE cannot exceed 1.0")
        if self.max_open_positions < 1:
            problems.append("MAX_OPEN_POSITIONS must be >= 1")
        if self.max_order_value <= 0:
            problems.append("MAX_ORDER_VALUE must be positive")
        if self.max_orders_per_day < 1 or self.max_orders_per_symbol_per_day < 1:
            problems.append("order frequency limits must be >= 1")
        if self.strategy_fast_window < 2 or self.strategy_slow_window <= self.strategy_fast_window:
            problems.append("STRATEGY_SLOW_WINDOW must be greater than STRATEGY_FAST_WINDOW (>= 2)")
        if self.strategy_atr_window < 2:
            problems.append("STRATEGY_ATR_WINDOW must be >= 2")
        if self.max_data_age_seconds < 1:
            problems.append("MAX_DATA_AGE_SECONDS must be >= 1")
        if self.cycle_interval_seconds < 5:
            problems.append("CYCLE_INTERVAL_SECONDS must be >= 5")
        if not self.universe:
            problems.append("UNIVERSE must contain at least one symbol")
        if self.sim_initial_cash <= 0:
            problems.append("SIM_INITIAL_CASH must be positive")
        if not (0.0 <= self.sim_partial_fill_probability <= 1.0):
            problems.append("SIM_PARTIAL_FILL_PROBABILITY must be between 0 and 1")
        if self.allow_derivatives:
            problems.append("ALLOW_DERIVATIVES=true is not supported in this version (equities/ETFs only)")
        if self.notifier == NotifierKind.WEBHOOK and not self.notification_webhook_url:
            problems.append("NOTIFIER=webhook requires NOTIFICATION_WEBHOOK_URL")

        # --- broker consistency ---
        if self.broker == BrokerKind.AVANZA:
            problems.append(
                "BROKER=avanza is unsupported: Avanza publishes no official trading API and "
                "this project will not automate it through unofficial means"
            )
        if self.broker == BrokerKind.ALPACA and not (self.alpaca_api_key_id and self.alpaca_api_secret_key):
            problems.append("BROKER=alpaca requires ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY")

        # --- live gate ---
        if self.trading_mode == TradingMode.LIVE:
            if self.broker == BrokerKind.SIMULATED:
                problems.append("TRADING_MODE=live cannot be combined with BROKER=simulated")
            if not self.live_trading_confirmed:
                problems.append("TRADING_MODE=live requires LIVE_TRADING_CONFIRMED=true")
            if self.live_trading_confirmation_phrase.strip() != LIVE_CONFIRMATION_PHRASE:
                problems.append(
                    "TRADING_MODE=live requires LIVE_TRADING_CONFIRMATION_PHRASE to equal the exact phrase "
                    f"'{LIVE_CONFIRMATION_PHRASE}'"
                )
        else:
            # Paper mode must never carry live confirmations around; this prevents a later
            # one-variable change from silently enabling live trading.
            if self.live_trading_confirmed or self.live_trading_confirmation_phrase.strip():
                problems.append(
                    "LIVE_TRADING_CONFIRMED / LIVE_TRADING_CONFIRMATION_PHRASE must be unset while "
                    "TRADING_MODE=paper (refusing to keep a latent live confirmation)"
                )
        if problems:
            raise ValueError("; ".join(problems))
        return self

    # ------------------------------------------------------------------
    @property
    def is_live(self) -> bool:
        return self.trading_mode == TradingMode.LIVE

    @property
    def live_orders_permitted(self) -> bool:
        """True only when every element of the live gate is satisfied."""
        return (
            self.trading_mode == TradingMode.LIVE
            and self.trading_enabled
            and self.live_trading_confirmed
            and self.live_trading_confirmation_phrase.strip() == LIVE_CONFIRMATION_PHRASE
            and self.broker != BrokerKind.SIMULATED
        )

    def redacted(self) -> dict[str, Any]:
        """Settings safe for logging/dashboard (secrets masked)."""
        out: dict[str, Any] = {}
        for key, value in self.model_dump().items():
            if any(s in key for s in ("secret", "key", "password", "token", "webhook_url")):
                out[key] = "***" if value else ""
            elif isinstance(value, Decimal | Path | Enum):
                out[key] = str(value.value if isinstance(value, Enum) else value)
            else:
                out[key] = value
        return out


def load_settings(**overrides: Any) -> Settings:
    """Load and validate settings, raising :class:`ConfigError` on failure."""
    try:
        return Settings(**overrides)
    except ValidationError as exc:  # pragma: no cover - formatting only
        msgs = "; ".join(e.get("msg", str(e)) for e in exc.errors())
        raise ConfigError(f"Invalid configuration: {msgs}") from exc
    except ValueError as exc:
        raise ConfigError(f"Invalid configuration: {exc}") from exc
