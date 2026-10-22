"""python -m trading_agent.backtest.run_dataset --dataset-version <ID>

Runs the strategy in BACKTEST on a versioned dataset with the versioned execution assumptions,
then reruns with every modelled transaction cost doubled, and writes a report with gross and net
performance and the cost sensitivity. The report is research output only: it never contains a
live trade recommendation, and nothing here can place an order outside the simulator.
"""
import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Tuple

from trading_agent.backtest.dataset import DatasetSource, calendar_violations, file_sha256, load_dataset
from trading_agent.core.mode import Mode
from trading_agent.risk.approvals import load_approvals
from trading_agent.backtest.engine import BacktestEngine, BacktestReport
from trading_agent.backtest.performance import Summary, summarize
from trading_agent.core.errors import ConfigError
from trading_agent.core.mode import load_runtime_config
from trading_agent.execution.cost_model import ExecutionAssumptions, load_execution_assumptions
from trading_agent.planning.demo_strategy import demo_critic_response, demo_planner_response
from trading_agent.planning.recorded_llm import ScriptedLLM
from trading_agent.risk.policy import load_risk_policy

ROOT = Path(__file__).resolve().parents[3]
REPORTS = ROOT / "reports" / "backtests"
DISCLAIMER = ("BACKTEST research output only. This is not a trade recommendation, and it must not be used "
              "to place or size live orders. Past simulated performance does not predict future results.")


def run_once(dataset_version: str, execution: ExecutionAssumptions, run_id: str) -> Tuple[BacktestReport, Summary]:
    config = load_runtime_config(ROOT / "config" / "runtime.backtest.yaml")
    policy = load_risk_policy(ROOT / "config" / f"{config.risk_policy_version.replace('-', '_')}.yaml")
    ds = load_dataset(dataset_version)
    bad = calendar_violations(ds.ticks, policy.session)
    if bad:
        raise ConfigError(f"{len(bad)} quotes outside the market calendar, e.g. {bad[:3]}")
    engine = BacktestEngine(
        config, policy, ScriptedLLM(demo_planner_response, "demo-strategy.v1"),
        ScriptedLLM(demo_critic_response, "demo-critic.v1"), primary=DatasetSource(ds.ticks),
        execution=execution, corporate_actions=ds.actions, listings=ds.listings,
        dataset_version=ds.manifest.dataset_version, run_id=run_id)
    report = engine.run()
    return report, summarize(report)


APPROVED_EXECUTION_FILES = ("src/trading_agent/execution/cost_model.py",
                            "src/trading_agent/execution/simulated_broker.py")


def require_execution_approval(assumptions_path: Path, mode: Mode = Mode.BACKTEST, root: Path = ROOT) -> str:
    """The simulator code and the assumptions file must match an approval for `mode` (CLAUDE.md)."""
    rel = assumptions_path.resolve().relative_to(root.resolve()).as_posix()
    wanted = {p: file_sha256(root / p) for p in APPROVED_EXECUTION_FILES + (rel,)}
    for a in load_approvals(root / "config"):
        if mode in a.modes and mode not in a.not_approved_for and all(a.files.get(p) == h for p, h in wanted.items()):
            return a.id
    changed = [p for p in wanted]
    raise ConfigError(f"no approval for the current simulator/assumptions in {mode.value}: {', '.join(changed)}")


def run_with_sensitivity(dataset_version: str, assumptions_path: Path):
    require_execution_approval(assumptions_path)
    base_exec = load_execution_assumptions(assumptions_path)
    doubled = base_exec.model_copy(update={"version": base_exec.version + "+costs-x2",
                                           "costs": base_exec.costs.scaled(Decimal(2))})
    base = run_once(dataset_version, base_exec, "base")
    stress = run_once(dataset_version, doubled, "costx2")
    return base_exec, base, stress


def render(dataset_version: str, manifest, execu: ExecutionAssumptions, base, stress) -> str:
    (rb, sb), (rs, ss) = base, stress
    c = execu.costs

    def row(name, f, attr, pct=True):
        a, b = getattr(sb.net, attr), getattr(ss.net, attr)
        if a is None or b is None:
            d = "n/a"
        else:
            d = f"{float(b) - float(a):+.4%}" if pct else f"{float(b) - float(a):+,.2f}"
        return f"| {name} | {f(sb.gross)} | {f(sb.net)} | {f(ss.net)} | {d} |"

    lines = [
        f"# Backtest: {dataset_version}", "", f"> {DISCLAIMER}", "",
        f"**Dataset:** {manifest.description} (synthetic: **{manifest.synthetic}**, source: {manifest.source})  ",
        "**Strategy:** demo mean reversion, long-only (`planning/demo_strategy.py`); every order passed the "
        "deterministic risk gateway (risk-policy.v2)  ",
        f"**Execution assumptions:** `{execu.version}`: fills at the touch + {c.slippage_bps} bps slippage, "
        f"commission {c.commission_bps} bps (min {c.min_commission}/fill), sell fees {c.sell_fee_bps} bps, "
        f"latency {execu.latency_ms} ms (first quote at/after decision+latency), partial fills "
        f"{'on' if execu.partial_fills else 'off'} (<= {execu.max_participation} x displayed size, IOC)", "",
        "## Performance", "",
        "| metric | gross | net | net, costs x2 | x2 effect |", "|---|---|---|---|---|",
        row("P&L", lambda p: f"{p.pnl:,.2f}", "pnl", pct=False),
        row("total return", lambda p: f"{p.total_return:.4%}", "total_return"),
        row("max drawdown", lambda p: f"{p.max_drawdown:.4%}", "max_drawdown"),
        row("daily volatility", lambda p: f"{p.daily_volatility:.4%}", "daily_volatility"),
        row("Sharpe (annualized)", lambda p: "n/a" if p.sharpe_annualized is None else f"{p.sharpe_annualized:.2f}",
            "sharpe_annualized", pct=False),
        "", f"Sharpe and volatility over {sb.days} days are not statistically meaningful.", "",
        "## Costs and execution", "",
        "Spreads are market data and are paid in full in both runs (buys at the ask, sells at the bid). The x2 "
        "run doubles commissions, minimum commission, sell fees and slippage. Orders are limits at the "
        "decision-time touch, so slippage cannot push a fill above the limit: it can only cost missed fills.", "",
        "| | base | costs x2 |", "|---|---|---|",
        f"| traded notional | {sb.costs.traded_notional:,.2f} | {ss.costs.traded_notional:,.2f} |",
        f"| spread + slippage | {sb.costs.spread_and_slippage:,.2f} | {ss.costs.spread_and_slippage:,.2f} |",
        f"| commissions + fees | {sb.costs.fees:,.2f} | {ss.costs.fees:,.2f} |",
        f"| total cost (bps of traded) | {sb.costs.total:,.2f} ({sb.costs.cost_bps_of_traded:.1f}) | "
        f"{ss.costs.total:,.2f} ({ss.costs.cost_bps_of_traded:.1f}) |",
        f"| fills / partial / expired orders | {sb.fills} / {sb.partial_fills} / {sb.orders_expired} | "
        f"{ss.fills} / {ss.partial_fills} / {ss.orders_expired} |",
        f"| turnover (x starting equity) | {sb.turnover:.3f} | {ss.turnover:.3f} |",
        f"| dividends received | {sb.dividends:,.2f} | {ss.dividends:,.2f} |", "",
        "## Corporate actions, delistings, calendar", "",
        *[f"- {e}" for e in rb.events],
        f"- calendar: 0 quotes outside the session calendar; {sb.days} trading days",
        "- delisted securities stay in the data and this report; held positions are closed at the delisting price",
        "", "## Integrity", "",
        f"- trading state at end: {rb.trading_state.value} (costs x2: {rs.trading_state.value})",
        f"- audit ledger verified: {rb.audit.ledger_verified} (head {rb.ledger_head[:16]}...)",
        f"- final positions: {dict(rb.final_positions) or 'flat'}",
        "- no future leakage: decisions use only data at or before their timestamp; corporate-action "
        "adjustments are applied on the ex-date; fills use quotes after the decision (+latency) and before the next "
        "decision (enforced: latency < bar spacing)", "",
        "## Daily equity (net)", "", "| day | net equity | unmarked |", "|---|---|---|",
        *[f"| {m.day} | {m.equity:,.2f} | {', '.join(m.unmarked_positions) or '-'} |" for m in rb.daily],
        "", f"> {DISCLAIMER}", ""]
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dataset-version", required=True)
    ap.add_argument("--execution", default=str(ROOT / "config" / "execution_assumptions.v1.yaml"))
    a = ap.parse_args(argv)
    if "<" in a.dataset_version or ">" in a.dataset_version:
        print(f"'{a.dataset_version}' looks like a placeholder; pass a real dataset version from data/datasets/")
        return 2
    execu, base, stress = run_with_sensitivity(a.dataset_version, Path(a.execution))
    manifest = load_dataset(a.dataset_version).manifest
    text = render(a.dataset_version, manifest, execu, base, stress)
    out = REPORTS / a.dataset_version
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{execu.version}.md").write_bytes(text.encode("utf-8"))
    (out / f"{execu.version}.json").write_bytes(json.dumps(
        {"dataset_version": a.dataset_version, "execution": execu.model_dump(mode="json"),
         "base": base[1].model_dump(mode="json"), "costs_x2": stress[1].model_dump(mode="json"),
         "events": base[0].events, "disclaimer": DISCLAIMER}, indent=1).encode("utf-8"))
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
