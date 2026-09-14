"""Alpaca Market Data API provider (https://docs.alpaca.markets/docs/about-market-data-api).

Uses the same API keys as the trading API. The free IEX feed is delayed/partial;
the staleness check in the agent decides whether a quote is usable.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx

from trading_agent.domain import Bar, Quote, price, utcnow
from trading_agent.market_data.base import MalformedDataError, MarketDataError, MarketDataProvider
from trading_agent.market_data.validation import validate_bars


def _ts(value: str) -> datetime:
    value = value.replace("Z", "+00:00")
    if "." in value:
        head, tail = value.split(".", 1)
        frac = "".join(ch for ch in tail if ch.isdigit())
        value = f"{head}.{frac[:6]}{tail[len(frac) :]}"
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


class AlpacaMarketDataProvider(MarketDataProvider):
    name = "alpaca"

    def __init__(
        self,
        api_key_id: str,
        api_secret_key: str,
        base_url: str = "https://data.alpaca.markets",
        feed: str = "iex",
        timeout_seconds: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not base_url.startswith("https://"):
            raise MarketDataError("data base URL must be https")
        self.feed = feed
        self._client = httpx.Client(
            base_url=base_url,
            headers={"APCA-API-KEY-ID": api_key_id, "APCA-API-SECRET-KEY": api_secret_key},
            timeout=timeout_seconds,
            transport=transport,
        )

    def _get(self, path: str, params: dict) -> dict:
        try:
            resp = self._client.get(path, params=params)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise MarketDataError(f"market data request failed: {exc.__class__.__name__}") from exc
        if resp.status_code >= 400:
            raise MarketDataError(f"market data HTTP {resp.status_code}")
        try:
            return resp.json()
        except ValueError as exc:
            raise MalformedDataError("market data response is not JSON") from exc

    def get_quote(self, symbol: str) -> Quote | None:
        data = self._get(f"/v2/stocks/{symbol}/trades/latest", {"feed": self.feed})
        trade = data.get("trade") or {}
        if "p" not in trade or "t" not in trade:
            return None
        q = Quote(symbol=symbol, price=price(Decimal(str(trade["p"]))), timestamp=_ts(trade["t"]))
        self._touch()
        return q

    def get_bars(self, symbol: str, start: date | None = None, end: date | None = None, limit: int | None = None) -> list[Bar]:
        end = end or utcnow().date()
        start = start or (end - timedelta(days=400))
        bars: list[Bar] = []
        params = {
            "timeframe": "1Day",
            "start": start.isoformat(),
            "end": end.isoformat(),
            "limit": 10000,
            "adjustment": "all",
            "feed": self.feed,
        }
        page_token = None
        while True:
            if page_token:
                params["page_token"] = page_token
            data = self._get(f"/v2/stocks/{symbol}/bars", params)
            for raw in data.get("bars") or []:
                try:
                    bars.append(
                        Bar(
                            symbol,
                            _ts(raw["t"]),
                            price(Decimal(str(raw["o"]))),
                            price(Decimal(str(raw["h"]))),
                            price(Decimal(str(raw["l"]))),
                            price(Decimal(str(raw["c"]))),
                            Decimal(str(raw.get("v", 0))),
                        )
                    )
                except (KeyError, ValueError, ArithmeticError) as exc:
                    raise MalformedDataError(f"{symbol}: malformed bar {raw}") from exc
            page_token = data.get("next_page_token")
            if not page_token:
                break
        bars.sort(key=lambda b: b.timestamp)
        validate_bars(bars)
        self._touch()
        return bars[-limit:] if limit else bars

    def get_corporate_actions(self, symbol: str) -> list[dict]:
        data = self._get("/v1/corporate-actions", {"symbols": symbol, "types": "forward_split,reverse_split,cash_dividend"})
        return [{"type": k, "items": v} for k, v in (data.get("corporate_actions") or {}).items()]

    def healthcheck(self) -> bool:
        try:
            self.get_quote("SPY")
            return True
        except MarketDataError:
            return False
