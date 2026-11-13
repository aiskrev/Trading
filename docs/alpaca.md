# Alpaca (owner decision 2026-11-13: Alpaca for data and paper broker)

Owner's choice: **Alpaca for everything**. One free account gives you three things: the Alpaca Paper broker,
real-time IEX quotes and the market clock. It covers US stocks only. Later, Alpaca Live uses the same code
with another host and keys, but only after every gate in `docs/roadmap.md` is passed.

## Step 1: market data and market status (read-only), built (APR-0010)

| piece | file | what it does |
|---|---|---|
| WebSocket client | `market_data/websocket.py` | RFC 6455 client: TLS with certificate and hostname checks, text frames only, ping/pong, size limits, non-blocking |
| stream transport | `market_data/alpaca_stream.py` `AlpacaStreamTransport` | `wss://stream.data.alpaca.markets/v2/{iex,sip,delayed_sip,test}` (host allow-list); sends only auth and subscribe (quotes + trades) |
| decoder | `market_data/alpaca_stream.py` `AlpacaQuoteDecoder` | `q` -> `QuoteRecord`; `t` keeps the last trade price; control and other types count as liveness; errors 402/409 -> AUTH_REJECTED, 406/407 -> RATE_LIMITED |
| HTTPS GET | `account_data/https_transport.py` | GET only, one host from an allow-list (`paper-api.alpaca.markets`), system trust store |
| market status | `account_data/alpaca_status.py` | `GET /v2/clock` -> venue OPEN/CLOSED; `GET /v2/assets/{sym}` -> TRADING/HALTED; any error, a clock disagreeing with ours by > 2 s, or an answer about another symbol -> UNKNOWN |
| owner check | `scripts/alpaca_check.py` | read-only: clock, asset status, a few seconds of quotes; places no orders |

They plug into the existing machinery unchanged. `StreamingQuoteAdapter` handles backoff, health,
staleness and quote-book quarantine. The `AlpacaMarketStatus` callables go into `LiveLoopRunner`'s
`venue_status` / `instrument_status`.

Fail-closed choices:
* **One-sided quotes** (bid or ask 0, common on IEX) update nothing. The previous quote ages out of the book.
* **`last`** is the latest trade price seen on this connection. Before the first trade it is the quote mid.
* **Liveness:** Alpaca sends no heartbeats, so a WebSocket ping goes out every 15 s and its pong counts as one.
* **Sizes** come in round lots and are multiplied by 100.
* **Halts:** an exchange halt is not visible on IEX (that needs the paid SIP status channel). A halted symbol
  stops quoting, so the freshness rules deny it.

## Owner steps (never paste keys in chat)

1. Create a free account at alpaca.markets and open the **Paper** dashboard.
2. Generate paper API keys there.
3. In WSL, inside the repository clone:
   ```bash
   python scripts/vault.py put-secret alpaca-paper
   ```
   At the hidden prompt, enter `KEY_ID:SECRET` as one value.
4. Grant a read token and keep it for this shell only:
   ```bash
   python scripts/vault.py grant market-data --read alpaca-paper
   ```
   ```bash
   export TRADING_VAULT_TOKEN=<the token it prints>
   ```
5. Run the read-only check:
   ```bash
   python scripts/alpaca_check.py
   ```
   During US market hours (15:30-22:00 Bulgarian time) you should see quotes for SPY, AAPL and MSFT.
   Outside market hours, use the test stream instead:
   ```bash
   python scripts/alpaca_check.py --feed test --symbols FAKEPACA --seconds 10
   ```

## Next steps (not built yet)

2. Account adapter: `GET /v2/account`, `/v2/positions` and open orders give `AccountSnapshot` and the broker view.
3. Alpaca Paper `ExecutionPort`. Limit orders get whole-share `qty = floor(approved notional / limit price)`
   (Alpaca's notional orders are market-only). `client_order_id` is the existing derivation; recovery uses
   `GET /v2/orders:by_client_order_id`, and fills are polled. It needs its own approval and the PAPER mode
   permission, which only the owner can grant.
4. A real-symbol instrument registry, universe and risk policy v3 (needs approval). After that, SHADOW and
   PAPER sessions per the roadmap.
