# risk-policy.v3: real US symbols (Alpaca paper, ~100,000 USD), approved as APR-0012 (BACKTEST)

Files: `config/risk_policy.v3.yaml` (pinned in `risk_config.lock.yaml`), `config/instruments.v2.yaml`,
`config/universe.v2.yaml`, the compliance list `restricted.v1.yaml` (empty, unchanged). The synthetic
`risk-policy.v2` (AAA/BBB) stays for backtests and tests.

The owner is new to trading and delegated these numbers. Each one was chosen conservatively, and the reason is
below. They apply to a paper account. A live account gets a new version with smaller numbers (CANARY_LIVE).

| limit | v3 | why |
|---|---|---|
| universe | SPY, QQQ, IWM, AAPL, MSFT, GOOGL, AMZN, JPM, XOM | The most liquid US ETFs and large caps: tight spreads and continuous IEX quotes. No small caps, no leveraged ETFs and no names above about $1,000 a share. |
| sectors | INDEX_ETF, TECH, CONSUMER, FINANCIALS, ENERGY | Each sector is capped, so one theme cannot dominate. |
| session | 09:30-16:00 New York, skipping the first 5 and last 10 minutes | Opening and closing auctions bring wide, jumpy quotes. Early closes come from Alpaca's clock, which then reports CLOSED. |
| holidays | NYSE 2026-11 to 2027-12 | Pinned by a test. A new version is needed before 2028. |
| `max_quote_age_seconds` | 5 (v2: 3) | IEX quotes for less active names can pause for a few seconds. Stale quotes still deny. |
| `min_order_notional` | 1,000 | Must exceed every share price in the universe. Orders are whole shares, and anything below one share is never sent. |
| `max_order_notional` | 5,000 | 5% of the account per order. |
| `max_position_notional` | 10,000 | 10% per name. |
| `max_gross_exposure` / net | 40,000 | At most 40% invested; the rest stays in cash. |
| sector caps | 20,000 ETF / 20,000 tech / 10,000 others | Diversification. |
| `max_concentration_pct` | 0.12 | Single name after the trade, as a share of equity. |
| `min_cash_buffer` | 5,000 | Never spend the last 5% of cash. |
| `price_collar_bps` | 30 | The limit price must stay within 0.30% of mid. |
| `max_spread_bps` | 20 | The IEX book is wider than the national quote, but 0.20% still excludes thin moments. |
| `max_slippage_bps` | 15 | As in v2. |
| `commission_bps` | 1 | Alpaca charges no commission; this covers regulatory sell fees with margin. |
| `daily_loss_limit` | 1,000 | 1% per day, then no new risk. |
| `max_drawdown_pct` | 0.02 | 2% from the intraday high, then no new risk. |
| order rates | 10/day, 2/min, 1 per name per minute | Low activity makes every order reviewable. |
| short selling | off | Long only, as before. |
| critic, instrument status, broker view | required | Fail closed when unknown. |

Not covered by this version: early closes (the venue status covers them), options and crypto. Fractional
shares are not used.
