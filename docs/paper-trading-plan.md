# PAPER trading plan (proposed, not active)

The system runs in **BACKTEST**. Nothing here changes the mode: per CLAUDE.md only the owner can,
after approving the steps below. PAPER = live market data, simulated fills, **no execution
credentials, ever**.

## What is ready (tested in BACKTEST on mock feeds)

* `orchestration/live_loop.py`: live run loop: streaming adapters -> normalizer -> pipeline ->
  simulator, one **DecisionPacket per cycle** (including NO_TRADE and skipped cycles) in a
  hash-chained `packets.jsonl`, with raw model text, snapshots, risk decision, fills and stage timings.
* `orchestration/session_metrics.py`: per-session signal-to-proposal latency (p50/p95/max), risk
  denials by reason, simulated fills and partials, slippage vs decision mid, gross exposure, P&L,
  max drawdown, model and critic errors, schema failures, data gaps, empty-market cycles, restarts.
* Restart recovery: the ledger resumes its hash chain (a torn last line is set aside, other corruption
  refuses to start); the book is rebuilt from the ledger and reconciled against the simulator's own
  book file; a restart inside a session resumes no better than REDUCE_ONLY; a mismatch HALTs; a HALT
  survives into later sessions until `halt_ack.json` acknowledges that exact HALT record.
* Fail closed (`config/ops_policy.v1.yaml`): feed outage > 120 s or 3 consecutive model errors =>
  REDUCE_ONLY; packet/ledger write failure, clock going backwards, or any unexpected exception => HALTED.
* Tests: `tests/live/test_live_loop.py` (14).

## Recommended data vendor

**Alpaca Market Data API** for a first PAPER phase, because it is beginner-friendly: one account
gives a REST API and a WebSocket quote stream; it has a free real-time tier (IEX exchange quotes only);
and it is widely documented. Plan the upgrade path to the paid consolidated (SIP) feed: IEX-only
quotes show wider spreads and thinner size than the full market, so PAPER slippage and fill rates
will look worse than reality until you switch.

Verify before signing up (I cannot check current terms from here): today's pricing and data
licence, that the stream provides **trading-status / halt messages** (without them every
instrument is UNKNOWN and the gateway correctly denies all trades), and that you can create a key
that is **read-only for market data**. Alternative if Alpaca does not fit: Polygon.io (consolidated
feed, paid).

## Recommended length: 20 regular sessions (about 4 weeks)

Long enough to include a month-end, at least one high-volatility day and a variety of quiet days,
short enough to iterate. Include drills:

* 3 deliberate restarts mid-session (kill the process; expect REDUCE_ONLY and a clean reconciliation),
* 1 deliberate feed outage > 120 s (expect REDUCE_ONLY),
* 1 deliberate model outage (expect REDUCE_ONLY after 3 errors).

Acceptance (all must hold before discussing any next mode):

| measure | requirement |
|---|---|
| DecisionPackets | one per cycle, 100%; chain verifies every day |
| schema failures leading to an order | 0 |
| unexplained HALTs | 0 (every HALT has a cause and an acknowledgement) |
| restart drills | 3/3 reconcile and resume in REDUCE_ONLY |
| signal-to-proposal p95 | agreed target (measure first; e.g. < 5 s) |
| risk denials | every denial explained by its reason code |
| slippage | within the backtest's assumption, or the assumption is revised |
| data gaps | each one visible in the report and handled fail-closed |

## Steps only the owner can take

1. Decide to proceed with PAPER.
2. Open the vendor account; put the **read-only market-data** key in the approved secrets manager
   (never in chat, code, config or git).
3. Approve, with tests, as separate changes:
   * a market-data transport module for the chosen vendor: the one allowed exception to
     `tests/architecture/test_no_network_code.py`, market data only;
   * a new risk policy version with real symbols (instrument registry + universe), pinned and approved;
   * `/execution`: map PAPER -> SimulatedBroker in the registry and add `SimulatedBroker.restore_positions`
     (the live loop currently restores the simulator's book through a documented internal helper);
   * widening `PERMITTED_MODES_THIS_STAGE` to include PAPER, and an approval entry for PAPER.
4. Change the runtime mode to PAPER yourself.

## Known limits of the loop today

* Paper fills use the quote at decision time (+ slippage, displayed-size partials); a fill-on-next-quote
  latency model needs an /execution change.
* After a restart, feature history restarts empty (about 10 bars of warm-up with no signals), and the
  gateway's per-minute order window restarts (the session is REDUCE_ONLY by then).
