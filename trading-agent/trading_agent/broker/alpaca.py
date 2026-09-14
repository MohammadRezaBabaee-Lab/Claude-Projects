"""Alpaca Trading API adapter (https://docs.alpaca.markets).

Safety properties
-----------------
* The base URL is chosen from the live gate: unless ``Settings.live_orders_permitted``
  is True the adapter talks to ``paper-api.alpaca.markets`` only.
* Credentials are read from settings (environment) and only ever sent as HTTP
  headers over HTTPS; they are never logged.
* Order submission is idempotent through ``client_order_id``. If a POST fails with
  a network error the adapter looks the order up by client id before reporting the
  outcome, so retries can never create a duplicate order.
* GET requests are retried with exponential backoff; POST /orders is never blindly
  retried.
* Margin is never used: buying power is capped at cash unless ALLOW_MARGIN=true.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx

from trading_agent.broker.base import (
    Broker,
    BrokerAuthError,
    BrokerConnectionError,
    InsufficientFundsError,
    OrderNotSubmittedError,
    OrderOutcomeUnknownError,
    OrderRejectedError,
)
from trading_agent.domain import (
    Account,
    AssetClass,
    Fill,
    Instrument,
    MarketStatus,
    Order,
    OrderRequest,
    OrderStatus,
    OrderType,
    Position,
    Side,
    TimeInForce,
    money,
    price,
    utcnow,
)
from trading_agent.logging_config import EventLogger

log = EventLogger("broker.alpaca")

_STATUS_MAP = {
    "new": OrderStatus.SUBMITTED,
    "pending_new": OrderStatus.SUBMITTED,
    "accepted": OrderStatus.ACCEPTED,
    "accepted_for_bidding": OrderStatus.ACCEPTED,
    "held": OrderStatus.ACCEPTED,
    "partially_filled": OrderStatus.PARTIALLY_FILLED,
    "filled": OrderStatus.FILLED,
    "done_for_day": OrderStatus.CANCELLED,
    "canceled": OrderStatus.CANCELLED,
    "pending_cancel": OrderStatus.ACCEPTED,
    "pending_replace": OrderStatus.ACCEPTED,
    "replaced": OrderStatus.CANCELLED,
    "expired": OrderStatus.EXPIRED,
    "rejected": OrderStatus.REJECTED,
    "stopped": OrderStatus.ACCEPTED,
    "suspended": OrderStatus.ACCEPTED,
    "calculated": OrderStatus.ACCEPTED,
}

_TYPE_MAP = {
    OrderType.MARKET: "market",
    OrderType.LIMIT: "limit",
    OrderType.STOP: "stop",
    OrderType.STOP_LIMIT: "stop_limit",
}

_LEVERAGED_HINTS = ("2X", "3X", "ULTRA", "LEVERAGED", "-1X", "INVERSE", "DAILY BULL", "DAILY BEAR")


def _parse_ts(value: str | None) -> datetime:
    if not value:
        return utcnow()
    value = value.replace("Z", "+00:00")
    # Alpaca sends nanosecond precision which fromisoformat rejects; trim to microseconds.
    if "." in value:
        head, tail = value.split(".", 1)
        frac = "".join(ch for ch in tail if ch.isdigit())
        tz = tail[len(frac) :]
        value = f"{head}.{frac[:6]}{tz}"
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _dec(value: Any, default: str = "0") -> Decimal:
    if value is None or value == "":
        return Decimal(default)
    return Decimal(str(value))


class AlpacaBroker(Broker):
    name = "alpaca"

    def __init__(
        self,
        api_key_id: str,
        api_secret_key: str,
        paper_base_url: str = "https://paper-api.alpaca.markets",
        live_base_url: str = "https://api.alpaca.markets",
        live_orders_permitted: bool = False,
        allow_margin: bool = False,
        timeout_seconds: float = 10.0,
        max_retries: int = 3,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_key_id or not api_secret_key:
            raise BrokerAuthError("Alpaca credentials missing (ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY)")
        self.is_paper = not live_orders_permitted
        self.base_url = paper_base_url if self.is_paper else live_base_url
        if not self.base_url.startswith("https://"):
            raise BrokerConnectionError("Alpaca base URL must be https")
        self.allow_margin = allow_margin
        self.max_retries = max_retries
        self._client = httpx.Client(
            base_url=self.base_url,
            headers={
                "APCA-API-KEY-ID": api_key_id,
                "APCA-API-SECRET-KEY": api_secret_key,
                "Accept": "application/json",
                "User-Agent": "trading-agent/0.1",
            },
            timeout=timeout_seconds,
            transport=transport,
        )
        log.info("broker_initialised", endpoint="paper" if self.is_paper else "LIVE", base_url=self.base_url)

    # ------------------------------------------------------------- http
    def _request(self, method: str, path: str, *, retry: bool, **kwargs: Any) -> Any:
        attempts = self.max_retries if retry else 1
        delay = 0.5
        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            start = time.perf_counter()
            try:
                resp = self._client.request(method, path, **kwargs)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = BrokerConnectionError(f"{method} {path}: {exc.__class__.__name__}")
                log.warning("broker_transport_error", attempt=attempt, path=path, error=exc.__class__.__name__)
            else:
                latency = int((time.perf_counter() - start) * 1000)
                log.debug("broker_http", latency_ms=latency, path=path, status=resp.status_code)
                order_post = method == "POST" and path == "/v2/orders"
                if resp.status_code == 401 or (resp.status_code == 403 and not order_post):
                    raise BrokerAuthError(f"authentication failed ({resp.status_code}) for {path}")
                if resp.status_code == 404:
                    return None
                if resp.status_code == 429 or resp.status_code >= 500:
                    last_exc = BrokerConnectionError(f"{method} {path}: HTTP {resp.status_code}")
                    log.warning("broker_http_retryable", attempt=attempt, path=path, status=resp.status_code)
                elif resp.status_code >= 400:
                    detail = self._error_detail(resp)
                    if "insufficient" in detail.lower() or "buying power" in detail.lower():
                        raise InsufficientFundsError(detail)
                    raise OrderRejectedError(f"HTTP {resp.status_code}: {detail}")
                else:
                    return resp.json() if resp.content else None
            if attempt < attempts:
                time.sleep(delay)
                delay *= 2
        assert last_exc is not None
        raise last_exc

    @staticmethod
    def _error_detail(resp: httpx.Response) -> str:
        try:
            body = resp.json()
            return str(body.get("message") or body)
        except Exception:
            return resp.text[:200]

    # -------------------------------------------------------- interface
    def get_account(self) -> Account:
        data = self._request("GET", "/v2/account", retry=True)
        if data is None:
            raise BrokerConnectionError("account endpoint returned no data")
        cash = _dec(data.get("cash"))
        bp = _dec(data.get("buying_power"))
        non_margin_bp = _dec(data.get("non_marginable_buying_power"), default=str(cash))
        effective_bp = bp if self.allow_margin else min(bp, non_margin_bp, cash)
        return Account(
            account_id=str(data.get("account_number") or data.get("id")),
            currency=str(data.get("currency", "USD")),
            cash=money(cash),
            buying_power=money(max(effective_bp, Decimal("0"))),
            portfolio_value=money(_dec(data.get("portfolio_value"))),
            equity=money(_dec(data.get("equity"))),
            timestamp=utcnow(),
            multiplier=_dec(data.get("multiplier"), default="1"),
            status=str(data.get("status", "")),
            is_paper=self.is_paper,
        )

    def get_positions(self) -> list[Position]:
        data = self._request("GET", "/v2/positions", retry=True) or []
        out = []
        for p in data:
            out.append(
                Position(
                    symbol=p["symbol"],
                    quantity=_dec(p.get("qty")),
                    avg_cost=price(_dec(p.get("avg_entry_price"))),
                    current_price=price(_dec(p.get("current_price"))),
                    timestamp=utcnow(),
                )
            )
        return out

    def get_orders(self, open_only: bool = True) -> list[Order]:
        params = {"status": "open" if open_only else "all", "limit": 500, "direction": "desc"}
        data = self._request("GET", "/v2/orders", retry=True, params=params) or []
        return [self._to_order(o) for o in data]

    def get_order(self, client_order_id: str) -> Order | None:
        data = self._request("GET", "/v2/orders:by_client_order_id", retry=True, params={"client_order_id": client_order_id})
        return self._to_order(data) if data else None

    def submit_order(self, request: OrderRequest) -> Order:
        request.validate()
        if not self.is_paper and self.base_url != "https://api.alpaca.markets":
            raise BrokerConnectionError("refusing to submit live order to non-standard base URL")
        try:
            existing = self.get_order(request.client_order_id)
        except BrokerConnectionError as exc:
            raise OrderNotSubmittedError(f"pre-submit lookup failed, order not sent: {exc}") from exc
        if existing is not None:
            log.info("order_already_exists", client_order_id=request.client_order_id)
            return existing
        payload: dict[str, Any] = {
            "symbol": request.symbol,
            "qty": str(request.quantity),
            "side": request.side.value,
            "type": _TYPE_MAP[request.order_type],
            "time_in_force": request.time_in_force.value,
            "client_order_id": request.client_order_id,
        }
        if request.limit_price is not None:
            payload["limit_price"] = str(request.limit_price)
        if request.stop_price is not None:
            payload["stop_price"] = str(request.stop_price)
        try:
            data = self._request("POST", "/v2/orders", retry=False, json=payload)
        except BrokerConnectionError as exc:
            # Outcome unknown: check whether the broker has the order before surfacing.
            try:
                found = self.get_order(request.client_order_id)
            except Exception as lookup_exc:  # noqa: BLE001
                raise OrderOutcomeUnknownError(
                    f"submission failed ({exc}) and lookup failed ({lookup_exc}); reconcile before retrying"
                ) from exc
            if found is not None:
                return found
            raise
        if data is None:
            raise OrderOutcomeUnknownError("order endpoint returned no data; reconcile before retrying")
        return self._to_order(data)

    def cancel_order(self, client_order_id: str) -> Order:
        order = self.get_order(client_order_id)
        if order is None:
            raise OrderRejectedError(f"unknown order {client_order_id}")
        if order.status.is_terminal:
            return order
        self._request("DELETE", f"/v2/orders/{order.broker_order_id}", retry=False)
        refreshed = self.get_order(client_order_id)
        return refreshed or order

    def get_market_status(self) -> MarketStatus:
        data = self._request("GET", "/v2/clock", retry=True) or {}
        return MarketStatus(
            is_open=bool(data.get("is_open", False)),
            timestamp=_parse_ts(data.get("timestamp")),
            next_open=_parse_ts(data.get("next_open")) if data.get("next_open") else None,
            next_close=_parse_ts(data.get("next_close")) if data.get("next_close") else None,
            source="alpaca:clock",
        )

    def get_instrument(self, symbol: str) -> Instrument | None:
        data = self._request("GET", f"/v2/assets/{symbol}", retry=True)
        if not data:
            return None
        name = str(data.get("name", ""))
        upper = name.upper()
        leveraged = any(h in upper for h in _LEVERAGED_HINTS)
        return Instrument(
            symbol=data["symbol"],
            name=name,
            exchange=str(data.get("exchange", "")),
            currency="USD",
            asset_class=AssetClass.ETF if "ETF" in upper else AssetClass.EQUITY,
            tradable=bool(data.get("tradable", False)) and data.get("status") == "active",
            fractionable=bool(data.get("fractionable", False)),
            leveraged=leveraged,
            derivative=data.get("class") not in (None, "us_equity"),
        )

    # ---------------------------------------------------------- mapping
    def _to_order(self, o: dict[str, Any]) -> Order:
        status = _STATUS_MAP.get(str(o.get("status")), OrderStatus.UNKNOWN)
        filled_qty = _dec(o.get("filled_qty"))
        avg = o.get("filled_avg_price")
        order = Order(
            client_order_id=str(o.get("client_order_id")),
            symbol=str(o.get("symbol")),
            side=Side(o.get("side", "buy")),
            quantity=_dec(o.get("qty")),
            order_type=OrderType(o.get("type", "market")),
            status=status,
            created_at=_parse_ts(o.get("created_at")),
            updated_at=_parse_ts(o.get("updated_at")),
            limit_price=_dec(o["limit_price"]) if o.get("limit_price") else None,
            stop_price=_dec(o["stop_price"]) if o.get("stop_price") else None,
            time_in_force=TimeInForce(o.get("time_in_force", "day")),
            broker_order_id=str(o.get("id")),
            filled_quantity=filled_qty,
            avg_fill_price=price(_dec(avg)) if avg else None,
        )
        if filled_qty > 0 and avg:
            # Alpaca reports aggregate fills on the order; expose one synthetic fill for auditing.
            order.fills.append(
                Fill(
                    fill_id=f"alp_{order.broker_order_id}_{filled_qty}",
                    client_order_id=order.client_order_id,
                    symbol=order.symbol,
                    side=order.side,
                    quantity=filled_qty,
                    price=price(_dec(avg)),
                    commission=Decimal("0"),
                    timestamp=order.updated_at,
                    broker_fill_id=order.broker_order_id,
                )
            )
        return order

    def close(self) -> None:
        self._client.close()
