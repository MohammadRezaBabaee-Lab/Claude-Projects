"""Avanza: UNSUPPORTED.

Avanza (avanza.se) does not publish an official, documented trading API for
customers. The only integrations that exist are reverse-engineered wrappers of the
private endpoints used by their website/app. Using them would mean automating an
authenticated website in a way its terms do not permit and bypassing security
controls such as BankID/TOTP, which this project explicitly refuses to do.

This adapter therefore exists only to fail loudly if someone selects it.
"""

from __future__ import annotations

from trading_agent.broker.base import Broker, UnsupportedBrokerError

REASON = (
    "Avanza offers no official trading API. This project will not reverse-engineer private "
    "endpoints, scrape authenticated pages or bypass BankID/MFA. Use BROKER=alpaca (or add an "
    "adapter for another broker with an official API such as Saxo OpenAPI or Nordnet nExt)."
)


class AvanzaBroker(Broker):
    name = "avanza"

    def __init__(self, *_args, **_kwargs) -> None:
        raise UnsupportedBrokerError(REASON)

    # The abstract methods are never reachable; declared for completeness.
    def get_account(self):  # pragma: no cover
        raise UnsupportedBrokerError(REASON)

    def get_positions(self):  # pragma: no cover
        raise UnsupportedBrokerError(REASON)

    def get_orders(self, open_only: bool = True):  # pragma: no cover
        raise UnsupportedBrokerError(REASON)

    def get_order(self, client_order_id: str):  # pragma: no cover
        raise UnsupportedBrokerError(REASON)

    def submit_order(self, request):  # pragma: no cover
        raise UnsupportedBrokerError(REASON)

    def cancel_order(self, client_order_id: str):  # pragma: no cover
        raise UnsupportedBrokerError(REASON)

    def get_market_status(self):  # pragma: no cover
        raise UnsupportedBrokerError(REASON)

    def get_instrument(self, symbol: str):  # pragma: no cover
        raise UnsupportedBrokerError(REASON)
