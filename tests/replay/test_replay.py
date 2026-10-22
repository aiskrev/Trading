"""AT-16: deterministic replay. Same inputs + recorded LLM responses => byte-identical audit ledger."""
from trading_agent.backtest.engine import BacktestEngine
from trading_agent.core.hashing import canonical_json
from trading_agent.market_data.csv_source import CsvMarketDataSource
from trading_agent.planning.demo_strategy import demo_critic_response, demo_planner_response
from trading_agent.planning.recorded_llm import RecordedLLM, RecordingLLM, ScriptedLLM
from trading_agent.testing.harness import ROOT, risk_policy, runtime_config

FIX = ROOT / "data" / "fixtures"


def engine(planner, critic):
    return BacktestEngine(runtime_config(), risk_policy(), planner, critic,
                          primary=CsvMarketDataSource(FIX / "quotes_primary.csv", "primary"),
                          secondary=CsvMarketDataSource(FIX / "quotes_secondary.csv", "secondary"))


def ledger_bytes(e):
    return "\n".join(canonical_json(r) for r in e.ledger.records)


def test_replay_is_byte_identical():
    planner = RecordingLLM(ScriptedLLM(demo_planner_response, "demo-strategy.v1"))
    critic = RecordingLLM(ScriptedLLM(demo_critic_response, "demo-critic.v1"))
    first = engine(planner, critic)
    report1 = first.run()

    replay = engine(RecordedLLM(planner.recordings, "demo-strategy.v1"),
                    RecordedLLM(critic.recordings, "demo-critic.v1"))
    report2 = replay.run()

    assert report1.fills, "fixture should produce trades"
    assert ledger_bytes(first) == ledger_bytes(replay)
    assert report1.ledger_head == report2.ledger_head


def test_replay_with_missing_recordings_fails_closed():
    e = engine(RecordedLLM({}, "empty"), RecordedLLM({}, "empty"))
    report = e.run()
    assert report.fills == []
    # the model is consulted only when a deterministic trigger fires; every such call fails closed
    assert report.metrics.get("planner_llm_unavailable", 0) >= 1
    assert report.metrics.get("gate_no_trigger", 0) + report.metrics["planner_llm_unavailable"] <= report.ticks
