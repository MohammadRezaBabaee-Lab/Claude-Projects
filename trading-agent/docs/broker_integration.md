# Broker integration

## Investigation summary (September 2026)

| Broker | Official API? | Available to Swedish/EU residents | Markets | Decision |
|---|---|---|---|---|
| **Avanza** | **No.** Avanza publishes no developer API or terms permitting automation. All existing libraries reverse-engineer the private web/app endpoints and automate BankID/TOTP login. | n/a | Nordic | **Unsupported.** `BROKER=avanza` refuses to start. We will not scrape, bypass MFA or use private endpoints. |
| **Alpaca** (Alpaca Securities LLC) | Yes: documented REST/WebSocket Trading API and Market Data API, first-class paper environment, API-key auth | Yes. Alpaca completed EEA passporting to 29 countries including Sweden in July 2026 (verify your own eligibility in the account application). | US-listed equities and ETFs (USD) | **Implemented** (`trading_agent/broker/alpaca.py`). |
| **Saxo Bank OpenAPI** | Yes: documented REST API, SIM environment, OAuth 2 (Authorization Code / PKCE); live app credentials for funded direct clients | Yes (Saxo operates in Sweden) | Global incl. Nasdaq Stockholm | Documented as the recommended next adapter for Nordic listings. Not implemented. |
| **Nordnet nExt API** | Yes, official, but production access requires a certification process with Nordnet and the API is for the customer's own use only | Swedish customers | Nordic | Candidate for Nordic listings after certification. Not implemented. |
| **Interactive Brokers** | Yes: Client Portal Web API / OAuth | Yes (IBKR Ireland) | Global | Viable but requires the Client Portal Gateway process; not implemented. |
| **Trading 212** | Yes: public equity API (still marked beta with strict rate limits) | Yes (EU) | US/EU equities | Not implemented; less mature than the above. |

Sources consulted: Alpaca support pages and the EEA passporting announcement, Saxo developer
portal (`developer.saxo`), Nordnet external API portal (`api.test.nordnet.se`), IBKR Campus,
Trading 212 public API docs, and the absence of any developer offering on avanza.se.

## Why Alpaca first

* Official, documented, stable API with **explicit paper-trading support**: the same adapter
  code can be exercised end-to-end against `paper-api.alpaca.markets` with zero financial risk.
* Simple, revocable API-key authentication; keys can be regenerated at any time and paper and
  live keys are separate credentials.
* Commission-free US equities/ETFs (regulatory fees still apply), which keeps the initial
  scope to ordinary shares and ETFs.
* Available to Swedish residents.

Limitations you must accept:

* **US-listed instruments only** in USD. Stockholm-listed stocks need a second adapter (Saxo
  or Nordnet); the broker abstraction is designed for that.
* The free IEX market-data feed is partial/delayed; the agent's staleness check
  (`MAX_DATA_AGE_SECONDS`) governs whether a quote is usable. SIP data is a paid add-on.
* Alpaca accounts are margin accounts by default (`multiplier` 2). The adapter **caps buying
  power at cash** unless `ALLOW_MARGIN=true`, and the risk manager forbids margin anyway.
* Pattern-day-trader rules apply to US accounts under 25k USD; the default order-frequency
  limits keep the agent well below day-trading frequency.

## Permissions and data access (Alpaca)

| Item | Detail |
|---|---|
| Credentials | `APCA-API-KEY-ID` and `APCA-API-SECRET-KEY` headers. Generate them in the Alpaca dashboard; paper and live keys are distinct. Alpaca keys are account-scoped; there is no read-only scope, so protect them like a password. |
| Endpoints used | `GET /v2/account`, `GET /v2/positions`, `GET /v2/orders`, `GET /v2/orders:by_client_order_id`, `POST /v2/orders`, `DELETE /v2/orders/{id}`, `GET /v2/clock`, `GET /v2/assets/{symbol}` |
| Market data | `GET /v2/stocks/{symbol}/bars` (daily, split/dividend adjusted), `GET /v2/stocks/{symbol}/trades/latest`, `GET /v1/corporate-actions` on `data.alpaca.markets` |
| Order types | market, limit, stop, stop-limit; `day` / `gtc` |
| Idempotency | `client_order_id` (we generate one per order and look it up before any retry) |

## The broker interface

```python
class Broker(ABC):
    def get_account(self) -> Account
    def get_cash_balance(self) -> Decimal
    def get_positions(self) -> list[Position]
    def get_orders(self, open_only=True) -> list[Order]
    def get_order(self, client_order_id) -> Order | None
    def submit_order(self, request: OrderRequest) -> Order
    def cancel_order(self, client_order_id) -> Order
    def get_market_status(self) -> MarketStatus
    def get_instrument(self, symbol) -> Instrument | None
```

Errors are normalised to `BrokerConnectionError`, `BrokerAuthError`, `OrderRejectedError`,
`InsufficientFundsError`, `OrderNotSubmittedError` and `OrderOutcomeUnknownError` so the OMS
never has to know which broker it is talking to. Strategies never see the broker at all.

## Adding another broker (e.g. Saxo OpenAPI)

1. Implement `Broker` in `trading_agent/broker/saxo.py`, mapping Saxo's `port/v1/accounts`,
   `port/v1/balances`, `port/v1/positions`, `trade/v2/orders` and `ref/v1/instruments`.
2. Use Saxo's OAuth PKCE flow with the refresh token stored outside Git (OS keychain or
   environment); never store the login password.
3. Point the adapter at `https://gateway.saxobank.com/sim/openapi` unless the live gate passes.
4. Add the enum value in `config.py`, wire it in `broker/factory.py`, write adapter tests with
   `httpx.MockTransport` like `tests/unit/test_alpaca_adapter.py`.
5. Set `EXCHANGE_CALENDAR=XSTO` for Stockholm listings.

## Simulated broker

`SimulatedBroker` is used for backtests and paper trading and models: cash and positions,
market/limit/stop/stop-limit orders, commissions (fixed + percentage), slippage in basis
points, market hours (market orders rejected while closed; queued orders fill at the next
session), DAY-order expiry, partial fills, rejections (unknown symbol, insufficient cash,
short selling), order status transitions and valuation. Its state is serialisable and is
persisted every cycle.
