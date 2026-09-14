from __future__ import annotations

from trading_agent.config import MarketDataKind, Settings
from trading_agent.market_data.base import MarketDataProvider


def build_market_data(settings: Settings) -> MarketDataProvider:
    if settings.market_data_provider == MarketDataKind.CSV:
        from trading_agent.market_data.csv_provider import CsvMarketDataProvider

        return CsvMarketDataProvider(settings.market_data_dir)
    if settings.market_data_provider == MarketDataKind.ALPACA:
        from trading_agent.market_data.alpaca_data import AlpacaMarketDataProvider

        return AlpacaMarketDataProvider(
            settings.alpaca_api_key_id,
            settings.alpaca_api_secret_key,
            base_url=settings.alpaca_data_base_url,
            feed=settings.alpaca_data_feed,
            timeout_seconds=settings.broker_timeout_seconds,
        )
    if settings.market_data_provider == MarketDataKind.SYNTHETIC:
        from datetime import timedelta

        from trading_agent.domain import utcnow
        from trading_agent.market_data.synthetic import SyntheticMarketDataProvider

        end = utcnow().date()
        return SyntheticMarketDataProvider(settings.universe, end - timedelta(days=500), end)
    raise ValueError(f"unknown market data provider {settings.market_data_provider}")
