"""Regression evals (AT-20): python -m evals.runner

At BACKTEST stage the model output in each case is fixed, so these evals test the
guardrails around the model (parser, gateway, critic handling), not model quality.
A live-model eval suite is a separate, later gate.
"""
import json
import sys
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Tuple

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT / "src", ROOT / ".deps"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from trading_agent.contracts.market import NewsItem, VenueStatus  # noqa: E402
from trading_agent.testing.harness import PASS_JSON, T0, Harness, market, quote, trade_json  # noqa: E402

CLEAN_TRADE = trade_json()


def _snapshot(scenario: Dict[str, Any]):
    as_of = T0 - timedelta(seconds=scenario.get("age_seconds", 0))
    quotes = scenario.get("quotes")
    if quotes is not None:
        quotes = {s: quote(m, ts=as_of) for s, m in quotes.items()}
    news = [NewsItem(ts=as_of, body="", **n) for n in scenario.get("news", [])]
    return market(quotes=quotes, as_of=as_of, venue=VenueStatus(scenario.get("venue", "OPEN")), news=news)


def _check(result, expect: Dict[str, Any]) -> List[str]:
    errors = []
    submitted = result.order is not None
    if submitted != expect["order_submitted"]:
        errors.append(f"order_submitted={submitted}, expected {expect['order_submitted']}")
    if "max_approved_notional" in expect and result.decision is not None:
        if result.decision.approved_notional > Decimal(expect["max_approved_notional"]):
            errors.append(f"approved {result.decision.approved_notional} > {expect['max_approved_notional']}")
    if "planner_failure" in expect:
        rationale = result.proposal.public_rationale if result.proposal else ""
        if expect["planner_failure"] not in rationale:
            errors.append(f"expected planner failure {expect['planner_failure']}, rationale={rationale!r}")
    for code in expect.get("reason_codes_include", []):
        codes = result.decision.reason_codes if result.decision else []
        if code not in codes:
            errors.append(f"missing reason code {code}; got {codes}")
    return errors


def run_planner_cases() -> List[Tuple[str, List[str]]]:
    out = []
    for case in json.loads((ROOT / "evals" / "planner" / "cases.json").read_text(encoding="utf-8")):
        h = Harness(planner_fn=lambda p, raw=case["model_output"]: raw, critic_fn=lambda p: PASS_JSON)
        out.append((case["id"], _check(h.tick(_snapshot(case["scenario"])), case["expect"])))
    return out


def run_critic_cases() -> List[Tuple[str, List[str]]]:
    out = []
    for case in json.loads((ROOT / "evals" / "critic" / "cases.json").read_text(encoding="utf-8")):
        h = Harness(planner_fn=lambda p: CLEAN_TRADE, critic_fn=lambda p, raw=case["critic_output"]: raw)
        out.append((case["id"], _check(h.tick(), case["expect"])))
    return out


def main() -> int:
    results = run_planner_cases() + run_critic_cases()
    failed = [(cid, errs) for cid, errs in results if errs]
    for cid, errs in results:
        print(f"{'PASS' if not errs else 'FAIL'}  {cid}" + ("" if not errs else "  -> " + "; ".join(errs)))
    print(f"\n{len(results) - len(failed)}/{len(results)} eval cases passed (gate requires 100%)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
