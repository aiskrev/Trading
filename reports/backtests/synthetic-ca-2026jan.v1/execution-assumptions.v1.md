# Backtest: synthetic-ca-2026jan.v1

> BACKTEST research output only. This is not a trade recommendation, and it must not be used to place or size live orders. Past simulated performance does not predict future results.

**Dataset:** Synthetic 8-day 1-minute AAA/BBB quotes with a 2:1 split, a cash dividend, a delisting while held, a thin-size bar and a market holiday (for testing the backtester only) (synthetic: **True**, source: scripts/make_dataset.py)  
**Strategy:** demo mean reversion, long-only (`planning/demo_strategy.py`); every order passed the deterministic risk gateway (risk-policy.v2)  
**Execution assumptions:** `execution-assumptions.v1`: fills at the touch + 2.0 bps slippage, commission 1.0 bps (min 1.00/fill), sell fees 0.3 bps, latency 1500 ms (first quote at/after decision+latency), partial fills on (<= 1.0 x displayed size, IOC)

## Performance

| metric | gross | net | net, costs x2 | x2 effect |
|---|---|---|---|---|
| P&L | -283.94 | -289.53 | -292.63 | -3.10 |
| total return | -0.2839% | -0.2895% | -0.2926% | -0.0031% |
| max drawdown | 0.2839% | 0.2895% | 0.2926% | +0.0031% |
| daily volatility | 0.0366% | 0.0362% | 0.0360% | -0.0002% |
| Sharpe (annualized) | -15.42 | -15.90 | -16.17 | -0.26 |

Sharpe and volatility over 8 days are not statistically meaningful.

## Costs and execution

Spreads are market data and are paid in full in both runs (buys at the ask, sells at the bid). The x2 run doubles commissions, minimum commission, sell fees and slippage. Orders are limits at the decision-time touch, so slippage cannot push a fill above the limit: it can only cost missed fills.

| | base | costs x2 |
|---|---|---|
| traded notional | 10,945.70 | 10,945.70 |
| spread + slippage | 2.50 | 2.51 |
| commissions + fees | 3.09 | 6.18 |
| total cost (bps of traded) | 5.59 (5.1) | 8.69 (7.9) |
| fills / partial / expired orders | 3 / 1 / 0 | 3 / 1 / 0 |
| turnover (x starting equity) | 0.109 | 0.109 |
| dividends received | 25.15 | 25.15 |

## Corporate actions, delistings, calendar

- 2026-01-14 SPLIT AAA 2:1 applied to position and price history
- 2026-01-16 CASH_DIVIDEND BBB 0.25/share credited 25.15
- 2026-01-21 DELISTED BBB at 46.80: closed 100.583383 shares, P&L -292.70
- calendar: 0 quotes outside the session calendar; 8 trading days
- delisted securities stay in the data and this report; held positions are closed at the delisting price

## Integrity

- trading state at end: NORMAL (costs x2: NORMAL)
- audit ledger verified: True (head 48851ba8da675098...)
- final positions: {'AAA': Decimal('0.000146')}
- no future leakage: decisions use only data at or before their timestamp; corporate-action adjustments are applied on the ex-date; fills use quotes after the decision (+latency) and before the next decision (enforced: latency < bar spacing)

## Daily equity (net)

| day | net equity | unmarked |
|---|---|---|
| 2026-01-12 | 100,000.00 | - |
| 2026-01-13 | 99,980.70 | - |
| 2026-01-14 | 99,979.02 | - |
| 2026-01-15 | 99,941.81 | - |
| 2026-01-16 | 99,863.36 | - |
| 2026-01-20 | 99,784.90 | - |
| 2026-01-21 | 99,710.47 | - |
| 2026-01-22 | 99,710.47 | - |

> BACKTEST research output only. This is not a trade recommendation, and it must not be used to place or size live orders. Past simulated performance does not predict future results.
