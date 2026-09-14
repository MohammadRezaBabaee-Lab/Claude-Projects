from __future__ import annotations

from trading_agent.broker.base import Broker, UnsupportedBrokerError
from trading_agent.config import BrokerKind, Settings


def build_broker(settings: Settings, **kwargs) -> Broker:
    """Create the configured broker. The simulated broker needs a price source & calendar (kwargs)."""
    if settings.broker == BrokerKind.SIMULATED:
        from trading_agent.broker.simulated import SimulatedBroker

        return SimulatedBroker(
            price_source=kwargs["price_source"],
            clock=kwargs.get("clock"),
            market_status_fn=kwargs.get("market_status_fn"),
            initial_cash=settings.sim_initial_cash,
            currency=settings.sim_currency,
            commission_per_order=settings.sim_commission_per_order,
            commission_pct=settings.sim_commission_pct,
            slippage_bps=settings.sim_slippage_bps,
            partial_fill_probability=settings.sim_partial_fill_probability,
            enforce_market_hours=settings.sim_enforce_market_hours,
            instruments=kwargs.get("instruments"),
        )
    if settings.broker == BrokerKind.ALPACA:
        from trading_agent.broker.alpaca import AlpacaBroker

        return AlpacaBroker(
            api_key_id=settings.alpaca_api_key_id,
            api_secret_key=settings.alpaca_api_secret_key,
            paper_base_url=settings.alpaca_paper_base_url,
            live_base_url=settings.alpaca_live_base_url,
            live_orders_permitted=settings.live_orders_permitted,
            allow_margin=settings.allow_margin,
            timeout_seconds=settings.broker_timeout_seconds,
            max_retries=settings.broker_max_retries,
        )
    if settings.broker == BrokerKind.AVANZA:
        from trading_agent.broker.avanza import AvanzaBroker

        return AvanzaBroker()
    raise UnsupportedBrokerError(f"unknown broker {settings.broker}")
