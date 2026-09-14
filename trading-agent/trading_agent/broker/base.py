from __future__ import annotations

from abc import ABC, abstractmethod

from trading_agent.domain import Account, Instrument, MarketStatus, Order, OrderRequest, Position


class BrokerError(Exception):
    """Base class for broker failures."""


class BrokerConnectionError(BrokerError):
    """Network/timeouts/5xx. The request may safely be retried if idempotent."""


class BrokerAuthError(BrokerError):
    """Authentication/authorization failure (401/403)."""


class OrderRejectedError(BrokerError):
    """The broker explicitly rejected the order (4xx with a business reason)."""


class InsufficientFundsError(OrderRejectedError):
    pass


class OrderNotSubmittedError(BrokerConnectionError):
    """A connectivity failure that happened *before* the order request was sent (safe to retry)."""


class OrderOutcomeUnknownError(BrokerError):
    """Submission failed *and* we could not determine whether the broker accepted it.

    The order manager must reconcile before any retry so no duplicate is created.
    """


class UnsupportedBrokerError(BrokerError):
    """The requested broker has no official API and will not be automated."""


class Broker(ABC):
    """Uniform broker interface.

    Implementations must be idempotent with respect to ``OrderRequest.client_order_id``:
    submitting the same client order id twice must return the existing order rather
    than creating a second one.
    """

    name: str = "abstract"
    is_paper: bool = True

    @abstractmethod
    def get_account(self) -> Account: ...

    def get_cash_balance(self):
        return self.get_account().cash

    @abstractmethod
    def get_positions(self) -> list[Position]: ...

    @abstractmethod
    def get_orders(self, open_only: bool = True) -> list[Order]: ...

    @abstractmethod
    def get_order(self, client_order_id: str) -> Order | None: ...

    @abstractmethod
    def submit_order(self, request: OrderRequest) -> Order: ...

    @abstractmethod
    def cancel_order(self, client_order_id: str) -> Order: ...

    @abstractmethod
    def get_market_status(self) -> MarketStatus: ...

    @abstractmethod
    def get_instrument(self, symbol: str) -> Instrument | None: ...

    def is_tradable(self, symbol: str) -> bool:
        inst = self.get_instrument(symbol)
        return bool(inst and inst.tradable)

    def healthcheck(self) -> bool:
        try:
            self.get_account()
            return True
        except BrokerError:
            return False
