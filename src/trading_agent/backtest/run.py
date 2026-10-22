"""python -m trading_agent.backtest.run  — runs the demo backtest on the bundled fixtures."""
import argparse
import json
import sys
from pathlib import Path

from trading_agent.backtest.engine import BacktestEngine
from trading_agent.core.mode import load_runtime_config
from trading_agent.market_data.csv_source import CsvMarketDataSource
from trading_agent.planning.demo_strategy import demo_critic_response, demo_planner_response
from trading_agent.planning.recorded_llm import ScriptedLLM
from trading_agent.risk.policy import load_risk_policy

ROOT = Path(__file__).resolve().parents[3]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=str(ROOT / "config" / "runtime.backtest.yaml"))
    ap.add_argument("--policy", default=str(ROOT / "config" / "risk_policy.v2.yaml"))
    ap.add_argument("--primary", default=str(ROOT / "data" / "fixtures" / "quotes_primary.csv"))
    ap.add_argument("--secondary", default=str(ROOT / "data" / "fixtures" / "quotes_secondary.csv"))
    ap.add_argument("--ledger", default=None, help="optional JSONL audit output path")
    args = ap.parse_args(argv)

    config = load_runtime_config(args.config)  # refuses any mode other than BACKTEST
    engine = BacktestEngine(
        config, load_risk_policy(args.policy),
        planner_llm=ScriptedLLM(demo_planner_response, model_version="demo-strategy.v1"),
        critic_llm=ScriptedLLM(demo_critic_response, model_version="demo-critic.v1"),
        primary=CsvMarketDataSource(args.primary, "primary"),
        secondary=CsvMarketDataSource(args.secondary, "secondary") if args.secondary else None,
        ledger_path=args.ledger,
    )
    report = engine.run()
    out = report.model_dump(mode="json", exclude={"audit": {"trades"}})
    out["trades_audited"] = len(report.audit.trades)
    print(json.dumps(out, indent=2))
    return 0 if report.audit.ledger_verified else 1


if __name__ == "__main__":
    sys.exit(main())
