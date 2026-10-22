"""AT-17: end-to-end backtest on fixtures produces fills, slippage/IS and an auditor report."""
from trading_agent.backtest import run as cli
from trading_agent.backtest.engine import BacktestEngine
from trading_agent.core.trading_state import TradingState
from trading_agent.market_data.csv_source import CsvMarketDataSource
from trading_agent.planning.demo_strategy import demo_critic_response, demo_planner_response
from trading_agent.planning.recorded_llm import ScriptedLLM
from trading_agent.testing.harness import ROOT, risk_policy, runtime_config

FIX = ROOT / "data" / "fixtures"


def run():
    return BacktestEngine(runtime_config(), risk_policy(),
                          ScriptedLLM(demo_planner_response, "demo-strategy.v1"),
                          ScriptedLLM(demo_critic_response, "demo-critic.v1"),
                          primary=CsvMarketDataSource(FIX / "quotes_primary.csv", "primary"),
                          secondary=CsvMarketDataSource(FIX / "quotes_secondary.csv", "secondary")).run()


def test_end_to_end_backtest():
    report = run()
    assert [f.side.value for f in report.fills] == ["BUY", "SELL"]
    assert report.trading_state is TradingState.NORMAL
    assert report.audit.ledger_verified
    assert report.metrics["ticks"] == 60
    assert "planner_schema_failure" not in report.metrics
    trades = report.audit.trades
    assert len(trades) == 2
    for t in trades:
        assert t.slippage_bps is not None and t.implementation_shortfall is not None
        assert t.evidence_ids and all(len(e) == 64 for e in t.evidence_ids)
    assert report.realized_pnl > 0  # the fixture is built to revert


def test_cli_runs(capsys):
    assert cli.main(["--ledger", ""]) == 0
    assert '"ledger_verified": true' in capsys.readouterr().out
