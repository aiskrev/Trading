"""Per-session measurements computed only from persisted DecisionPackets (+ the run manifest)."""
import json
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional

from pydantic import Field

from trading_agent.audit.chain import ChainedLog
from trading_agent.contracts.base import Contract
from trading_agent.contracts.decision_packet import DecisionPacket
from trading_agent.core.types import Side

D0 = Decimal(0)
SCHEMA = {"SCHEMA_FAILURE"}
CRITIC_SCHEMA = {"CRITIC_SCHEMA_FAILURE"}
MODEL_ERR = {"LLM_UNAVAILABLE", "MODEL_FAILURE"}
CRITIC_ERR = {"CRITIC_UNAVAILABLE", "CRITIC_FAILURE"}
GAP_CODES = {"SEQUENCE_GAP", "STALE_FEED", "DISCONNECTED", "STALE_INSTRUMENT", "LATE_MESSAGE", "MALFORMED_MESSAGE",
             "MISSING_PRIMARY", "SOURCE_DIVERGENCE", "OUT_OF_ORDER", "DUPLICATE_MESSAGE", "RATE_LIMITED",
             "UNKNOWN_INSTRUMENT", "FUTURE_TIMESTAMP"}


class Latency(Contract):
    n: int
    p50_ms: Optional[float]
    p95_ms: Optional[float]
    max_ms: Optional[float]


class SessionMetrics(Contract):
    session_id: str
    decisions: int
    outcomes: Dict[str, int]
    signal_to_proposal: Latency
    risk_denials: int
    denial_reasons: Dict[str, int]
    simulated_fills: int
    partial_fills: int
    filled_notional: Decimal
    slippage_bps_mean: Optional[float]          # vs decision-time mid, side-adjusted (+ = cost)
    slippage_bps_max: Optional[float]
    max_gross_exposure: Decimal
    end_gross_exposure: Decimal
    start_equity: Optional[Decimal]
    end_equity: Optional[Decimal]
    pnl: Optional[Decimal]
    max_drawdown: float
    model_errors: int
    critic_errors: int
    operational_errors: List[str] = Field(default_factory=list)
    schema_failures: int
    critic_schema_failures: int
    data_gaps: Dict[str, int]
    empty_market_cycles: int
    restarts: int
    recoveries: List[Dict[str, object]] = Field(default_factory=list)
    end_state: Optional[str] = None


def _pct(xs: List[float], q: float) -> Optional[float]:
    if not xs:
        return None
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(q * (len(xs) - 1) + 0.5))], 3)


def _equity(p: DecisionPacket) -> Optional[Decimal]:
    a = p.account
    if a is None:
        return None
    value = D0
    for sym, pos in a.positions.items():
        q = p.market.quotes.get(sym)
        if q is None:
            return None
        value += pos.quantity * q.mid
    return a.cash + value


def _gross(p: DecisionPacket) -> Decimal:
    a = p.account
    if a is None:
        return D0
    return sum((abs(pos.quantity) * p.market.quotes[s].mid for s, pos in a.positions.items() if s in p.market.quotes),
               D0)


def compute(packets: List[DecisionPacket], session_id: str, manifest: Optional[Dict] = None) -> SessionMetrics:
    ps = [p for p in packets if p.session_id == session_id]
    lat = [p.signal_to_proposal_ms for p in ps if p.signal_to_proposal_ms is not None]
    denials = [p for p in ps if p.outcome == "DENIED"]
    reasons = Counter(c for p in denials for c in p.risk_decision.reason_codes)
    fills = [(p, f) for p in ps for f in p.fills]
    slip = []
    for p, f in fills:
        q = p.market.quotes.get(f.symbol)
        if q is not None:
            sign = 1 if f.side is Side.BUY else -1
            slip.append(float(sign * (f.price - q.mid) / q.mid * 10000))
    partial = sum(1 for p in ps for f in p.fills
                  if p.approved_order and f.quantity * f.price < Decimal(p.approved_order["notional"]) * Decimal("0.95"))
    eq = [e for e in (_equity(p) for p in ps) if e is not None]
    peak, mdd = (eq[0] if eq else D0), 0.0
    for e in eq:
        peak = max(peak, e)
        mdd = max(mdd, float((peak - e) / peak) if peak else 0.0)
    gaps = Counter(i.code.value for p in ps for i in p.data_issues if i.code.value in GAP_CODES)
    recov = [r for r in (manifest or {}).get("recoveries", []) if r.get("kind") != "fresh_start"]
    segs = {p.run_segment for p in ps}
    return SessionMetrics(
        session_id=session_id, decisions=len(ps), outcomes=dict(Counter(p.outcome for p in ps)),
        signal_to_proposal=Latency(n=len(lat), p50_ms=_pct(lat, 0.5), p95_ms=_pct(lat, 0.95),
                                   max_ms=max(lat) if lat else None),
        risk_denials=len(denials), denial_reasons=dict(reasons.most_common()),
        simulated_fills=len(fills), partial_fills=partial,
        filled_notional=sum((f.quantity * f.price for _, f in fills), D0),
        slippage_bps_mean=round(sum(slip) / len(slip), 3) if slip else None,
        slippage_bps_max=round(max(slip), 3) if slip else None,
        max_gross_exposure=max((_gross(p) for p in ps), default=D0),
        end_gross_exposure=_gross(ps[-1]) if ps else D0,
        start_equity=eq[0] if eq else None, end_equity=eq[-1] if eq else None,
        pnl=(eq[-1] - eq[0]) if eq else None, max_drawdown=mdd,
        model_errors=sum(1 for p in ps if p.planner_failure in MODEL_ERR),
        critic_errors=sum(1 for p in ps if p.critic and set(p.critic.reason_codes) & CRITIC_ERR),
        operational_errors=[e for p in ps for e in p.errors],
        schema_failures=sum(1 for p in ps if p.planner_failure in SCHEMA),
        critic_schema_failures=sum(1 for p in ps if p.critic and set(p.critic.reason_codes) & CRITIC_SCHEMA),
        data_gaps=dict(gaps.most_common()), empty_market_cycles=sum(1 for p in ps if not p.market.quotes),
        restarts=max(0, len(segs) - 1), recoveries=[r for r in recov if r.get("segment") in segs],
        end_state=ps[-1].state_after.value if ps else None,
    )


def load_and_compute(run_dir, session_id: str) -> SessionMetrics:
    run_dir = Path(run_dir)
    log, _ = ChainedLog.open(run_dir / "packets.jsonl", DecisionPacket)
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    return compute(log.items, session_id, manifest)


def render(m: SessionMetrics) -> str:
    def f(x, fmt="{:,.2f}"):
        return "n/a" if x is None else fmt.format(x)
    rows = [
        ("decisions (packets)", m.decisions), ("outcomes", m.outcomes),
        ("signal-to-proposal p50 / p95 / max ms",
         f"{f(m.signal_to_proposal.p50_ms)} / {f(m.signal_to_proposal.p95_ms)} / {f(m.signal_to_proposal.max_ms)}"),
        ("risk denials", f"{m.risk_denials} {m.denial_reasons or ''}"),
        ("simulated fills (partial)", f"{m.simulated_fills} ({m.partial_fills})"),
        ("filled notional", f(m.filled_notional)),
        ("slippage bps mean / max", f"{f(m.slippage_bps_mean, '{:.2f}')} / {f(m.slippage_bps_max, '{:.2f}')}"),
        ("gross exposure max / end", f"{f(m.max_gross_exposure)} / {f(m.end_gross_exposure)}"),
        ("P&L", f(m.pnl)), ("max drawdown", f"{m.max_drawdown:.4%}"),
        ("model errors (planner / critic)", f"{m.model_errors} / {m.critic_errors}"),
        ("schema failures (planner / critic)", f"{m.schema_failures} / {m.critic_schema_failures}"),
        ("data gaps", m.data_gaps or 0), ("cycles with no usable quotes", m.empty_market_cycles),
        ("operational errors", m.operational_errors or 0),
        ("restarts / recoveries", f"{m.restarts} / {[(r['kind'], r.get('state_after'), r.get('reconciled')) for r in m.recoveries]}"),
        ("end trading state", m.end_state),
    ]
    return "\n".join([f"# Session {m.session_id}", "", "| measure | value |", "|---|---|"]
                     + [f"| {k} | {v} |" for k, v in rows]) + "\n"
