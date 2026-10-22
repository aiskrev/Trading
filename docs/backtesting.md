# Backtesting on versioned datasets

```bash
python -m trading_agent.backtest.run_dataset --dataset-version synthetic-ca-2026jan.v1
```

Runs the strategy in BACKTEST with `config/execution_assumptions.v1.yaml`, reruns with every modelled
cost doubled, and writes `reports/backtests/<dataset>/<execution-version>.{md,json}` with gross and
net performance and the cost sensitivity. Reports carry a disclaimer and never contain a live trade
recommendation; nothing in this path can place an order outside the simulator.

## Datasets (`data/datasets/<dataset_version>/`)

`manifest.yaml` (version, description, `synthetic` flag, source, SHA-256 per file), `quotes.csv`
(raw, **unadjusted** bid/ask/size), `corporate_actions.csv` (SPLIT ratio / CASH_DIVIDEND per share,
by ex-date), `listings.csv` (listed_from, delisted_on, delist_price). Loading refuses hash mismatches
and quotes outside a security's listing period. Only `synthetic-ca-2026jan.v1` exists today: it is
**synthetic test data** built by `scripts/make_dataset.py`, not market history.

## Guarantees and how they are tested (`tests/backtest/test_dataset_backtest.py`)

| requirement | implementation | test |
|---|---|---|
| no future leakage | events processed strictly in time; actions applied on ex-date; fills use the first quote at/after decision+latency and latency must be < bar spacing | crash all prices after a cutoff and add a future split: every decision before the cutoff is byte-identical; mutations (1-min look-ahead, action a day early) fail it |
| corporate actions | split: position x ratio, avg cost / ratio, broker book adjusted, feature history rescaled on the ex-date; dividend: cash credited on ex-date, history adjusted by (1 - div/close) | `test_split_is_applied_point_in_time`, `test_feature_history_is_split_adjusted`, `test_dividend_credited_on_ex_date` |
| delisted securities kept | position closed at the delisting price, P&L reported, history and fills retained | `test_delisted_security_is_closed_not_dropped`, `test_quotes_after_delisting_are_rejected` |
| realistic spreads | buys fill at the ask, sells at the bid from the data | `test_spreads_are_paid_at_the_touch` |
| commissions and fees | commission bps with per-fill minimum; sell-side regulatory fee | `test_fees_and_minimum_commission` |
| configurable slippage | `slippage_bps` beyond the touch, capped by the order's limit | `test_configurable_slippage_moves_fills_when_not_capped_by_limit` |
| execution latency | `latency_ms`; order matched against first quote at/after decision+latency, same day only | `test_fills_happen_after_decision_plus_latency`, `test_latency_must_be_below_bar_spacing` |
| market calendar | quotes outside session/holidays rejected before running; gateway session rule | `test_calendar_rules` |
| partial fills | up to displayed size x `max_participation`; IOC remainder cancelled | `test_partial_fill_against_displayed_size` |
| gross vs net | gross = net equity + cumulative spread/slippage/fees | `test_gross_minus_net_equals_costs` |
| 2x cost sensitivity | rerun with commissions, min commission, fees and slippage doubled | `test_doubled_costs_sensitivity` |
| no live recommendation | disclaimer; banned phrasing checked | `test_report_has_gross_net_sensitivity_and_no_recommendation` |

## Known simplifications

* Dividends are credited on the ex-date (no pay-date lag, no withholding tax).
* With 1-minute data, latency means "next bar": conservative versus a real 1.5 s latency.
* Orders are IOC limits at the touch, so modelled slippage mostly shows up as missed fills, not worse
  prices; doubling it barely moves cost here. Spreads come from data and are not scaled.
* No halt data in the dataset; quoted symbols are treated as TRADING.
* No borrow costs, financing, or market impact beyond displayed size.
