"""PAPER composition end to end, on fakes: Alpaca stream (fake WebSocket server), Alpaca trading API (fake),
risk service (in process through its protocol), scripted model. No network and no real keys.

PAPER is NOT permitted at this stage. These tests widen the permitted set inside the test process only
(monkeypatch), and run against a temporary copy of config/ with a test-only approval entry for PAPER. They
prove the wiring works once the owner permits PAPER, and that it refuses everything else.
"""
import json
import secrets as _secrets
import shutil
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from pydantic import SecretStr

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent / "market_data"), str(HERE.parent / "invariants"), str(HERE.parent / "risk_service")]

from fake_websocket import FakeAlpacaServer  # noqa: E402
from rs_helpers import local_exchange  # noqa: E402
from test_alpaca_paper_broker import FakeAlpaca  # noqa: E402

from trading_agent.contracts.market import InstrumentStatus, VenueStatus  # noqa: E402
from trading_agent.core import mode as mode_module  # noqa: E402
from trading_agent.core.accounts import ALPACA_PAPER_ALIAS  # noqa: E402
from trading_agent.core.errors import ConfigError, ModeNotPermitted, SecretUnavailable  # noqa: E402
from trading_agent.core.mode import Mode, RuntimeConfig  # noqa: E402
from trading_agent.core.signing import EphemeralSigner  # noqa: E402
from trading_agent.core.trading_state import TradingState  # noqa: E402
from trading_agent.core.types import IdFactory  # noqa: E402
from trading_agent.execution.alpaca_http import AlpacaTradingClient  # noqa: E402
from trading_agent.market_data.alpaca_stream import AlpacaStreamTransport  # noqa: E402
from trading_agent.market_data.instruments import InstrumentRegistry  # noqa: E402
from trading_agent.orchestration import evaluation_gate  # noqa: E402
from trading_agent.orchestration.evaluation_gate import TRIGGER_CODE  # noqa: E402
from trading_agent.orchestration.paper import build_paper_runner, run_session  # noqa: E402
from trading_agent.planning.demo_strategy import demo_critic_response, demo_planner_response  # noqa: E402
from trading_agent.planning.recorded_llm import ScriptedLLM  # noqa: E402
from trading_agent.risk.policy import file_sha256, load_risk_policy  # noqa: E402
from trading_agent.risk.rules import mode as mode_rule  # noqa: E402
from trading_agent.risk_service.client import RemoteRiskGateway  # noqa: E402
from trading_agent.risk_service.service import build_risk_service  # noqa: E402
from trading_agent.testing.harness import ROOT  # noqa: E402
from trading_agent.testing.live import ManualClock  # noqa: E402

START = datetime(2026, 11, 17, 15, 0, 0, 500000, tzinfo=timezone.utc)      # Tuesday 10:00 New York
STRATEGY = "mean-reversion-demo.v1"


class Secrets:
    def __init__(self):
        self.key, self.secret = "PK" + _secrets.token_hex(8).upper(), _secrets.token_urlsafe(24)

    def get(self, name):
        if name != "alpaca-paper":
            raise SecretUnavailable(name)
        return SecretStr(f"{self.key}:{self.secret}")


class Broker(FakeAlpaca):
    """Fills IOC buys/sells at the limit price and keeps positions, like the paper account would."""

    def _advance(self, o):
        if o["status"] != "new":
            return
        o.update(status="filled", filled_qty=o["qty"], filled_avg_price=o["limit_price"], filled_at=START.isoformat())
        held = {p["symbol"]: Decimal(p["qty"]) for p in self.positions}
        sign = 1 if o["side"] == "buy" else -1
        held[o["symbol"]] = held.get(o["symbol"], Decimal(0)) + sign * Decimal(o["qty"])
        self.positions = [{"symbol": s, "qty": str(q), "side": "long"} for s, q in held.items() if q != 0]


class Status:
    def __init__(self):
        self.venue = VenueStatus.OPEN

    def venue_status(self, t):
        return self.venue

    def instrument_status(self, symbol):
        return InstrumentStatus.TRADING


@pytest.fixture()
def paper_permitted(monkeypatch):
    widened = frozenset({Mode.BACKTEST, Mode.PAPER})
    for module in (mode_module, mode_rule, evaluation_gate):
        monkeypatch.setattr(module, "PERMITTED_MODES_THIS_STAGE", widened)


@pytest.fixture()
def config_dir(tmp_path):
    """A copy of config/ with a TEST-ONLY approval entry that also covers PAPER."""
    target = tmp_path / "config"
    shutil.copytree(ROOT / "config", target)
    raw = yaml.safe_load((target / "approvals.yaml").read_text(encoding="utf-8"))
    raw["approvals"].append({
        "id": "APR-9999", "date": "2026-11-13", "approver": "test", "approved_via": "test", "statement": "test only",
        "modes": ["PAPER"], "code_scope": "test only", "not_approved_for": [],
        "files": {"risk_policy.v3.yaml": file_sha256(target / "risk_policy.v3.yaml"),
                  "restricted.v1.yaml": file_sha256(target / "restricted.v1.yaml"),
                  f"config/strategies/{STRATEGY}.yaml": file_sha256(target / "strategies" / f"{STRATEGY}.yaml"),
                  TRIGGER_CODE: file_sha256(ROOT / TRIGGER_CODE)}})
    (target / "approvals.yaml").write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    return target


def paper_config():
    return RuntimeConfig(mode=Mode.PAPER, deployment_id="paper-test", code_commit="test", strategy_version=STRATEGY,
                         risk_policy_version="risk-policy.v3", model_version="scripted",
                         prompt_versions={"trading_planner": "v1", "risk_critic": "v1"})


def build(tmp_path, config_dir, clock, broker=None, positions=None, risk_mode=Mode.PAPER, alias=ALPACA_PAPER_ALIAS):
    policy = load_risk_policy(config_dir / "risk_policy.v3.yaml")
    registry = InstrumentRegistry.load(config_dir / "instruments.v2.yaml")
    secrets = Secrets()
    stream = FakeAlpacaServer(secrets.key, secrets.secret)
    service = build_risk_service(policy, risk_mode, EphemeralSigner("rs-e2e"), clock, config_dir,
                                 tmp_path / "risk" / "requests.jsonl", "rs-e2e", account_alias=alias)
    gateway = RemoteRiskGateway(risk_mode, {policy.source_sha256}, IdFactory("orch"), clock,
                                exchange=local_exchange(service))
    broker = broker or Broker(positions=positions or [])
    runner = build_paper_runner(
        tmp_path / "run", paper_config(), policy, registry, secrets, gateway,
        ScriptedLLM(demo_planner_response, "demo"), ScriptedLLM(demo_critic_response, "demo"), clock,
        stream_transport=AlpacaStreamTransport("iex", socket_factory=stream.factory, answer_timeout_s=3),
        trading_client=AlpacaTradingClient(broker, secrets, "alpaca-paper", clock), market_status=Status(),
        config_dir=config_dir)
    runner.adapters[0].poll()          # connect, authenticate and subscribe before the fake server pushes data
    return runner, stream, broker, policy


def push_quotes(stream, clock, policy, mids):
    t = clock.now().strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    stream.push_text(json.dumps([{"T": "q", "S": s, "bp": float(Decimal(mids.get(s, "100.00")) - Decimal("0.01")),
                                  "ap": float(Decimal(mids.get(s, "100.00")) + Decimal("0.01")), "bs": 5, "as": 5,
                                  "t": t} for s in sorted(policy.universe)]))
    time.sleep(0.05)


# ------------------------------------------------------------------ the session works end to end
def test_a_paper_session_trades_through_the_whole_stack(tmp_path, config_dir, paper_permitted):
    clock = ManualClock(START)
    runner, stream, broker, policy = build(tmp_path, config_dir, clock)
    assert runner.recovery["kind"] == "fresh_start" and runner.portfolio.cash == Decimal(100000)
    packets = []
    script = ["100.00"] * 12 + ["99.40"] * 2 + ["100.20"] * 5         # steady, dip (BUY trigger), recover (EXIT)
    for mid in script:
        push_quotes(stream, clock, policy, {"AAPL": mid})
        packets.append(runner.step())
        clock.advance(30)
    outcomes = [p.outcome for p in packets]
    assert runner.state.state is TradingState.NORMAL, [p.errors for p in packets if p.errors]
    buys = [json.loads(c[3]) for c in broker.posts() if json.loads(c[3])["side"] == "buy"]
    sells = [json.loads(c[3]) for c in broker.posts() if json.loads(c[3])["side"] == "sell"]
    assert len(buys) == 1 and buys[0]["symbol"] == "AAPL" and buys[0]["time_in_force"] == "ioc"
    assert int(buys[0]["qty"]) * Decimal(buys[0]["limit_price"]) <= policy.max_order_notional
    # the exit is capped by max_order_notional too, so it may take more than one order; it ends flat
    assert sells and sum(int(o["qty"]) for o in sells) == int(buys[0]["qty"])
    assert broker.positions == [] and runner.portfolio.quantities() == {}
    assert "APPROVED" in outcomes or "REDUCED" in outcomes
    filled = [p for p in packets if p.fills]
    assert len(filled) == 1 + len(sells) and all(p.attribution.risk_policy_version == "risk-policy.v3" for p in packets)
    assert all(p.risk_decision is None or p.risk_decision.decision_id.startswith("rs-e2e:") for p in packets)
    text = (tmp_path / "run" / "packets.jsonl").read_text(encoding="utf-8")
    assert "signature" not in text


def test_run_session_stops_when_the_market_closes(tmp_path, config_dir, paper_permitted):
    clock = ManualClock(START)
    runner, stream, broker, policy = build(tmp_path, config_dir, clock)
    runner.venue_status = lambda t: VenueStatus.CLOSED
    lines = []
    cycles = run_session(runner, clock, cycle_seconds=30, max_cycles=10, sleep=lambda s: clock.advance(30),
                         report=lines.append)
    assert cycles == 3 and lines[-1] == "market closed: session ends"


def test_a_restart_mid_session_recovers_against_the_real_broker_book(tmp_path, config_dir, paper_permitted):
    clock = ManualClock(START)
    runner, stream, broker, policy = build(tmp_path, config_dir, clock)
    for mid in ["100.00"] * 12 + ["99.40"]:
        push_quotes(stream, clock, policy, {"AAPL": mid})
        runner.step()
        clock.advance(30)
    assert broker.positions, "the dip should have bought"
    stream.close()
    runner2, stream2, _, _ = build(tmp_path, config_dir, clock, broker=broker)    # same run dir, same broker
    assert runner2.recovery["kind"] == "restart_same_session" and runner2.recovery["reconciled"] is True
    assert runner2.state.state is TradingState.REDUCE_ONLY                          # ops floor after a restart


# ------------------------------------------------------------------ refusals
def test_paper_is_refused_while_it_is_not_permitted(tmp_path, config_dir):
    with pytest.raises(ModeNotPermitted):
        build(tmp_path, config_dir, ManualClock(START))


def test_a_new_run_refuses_an_account_that_already_holds_positions(tmp_path, config_dir, paper_permitted):
    with pytest.raises(ConfigError, match="holds positions"):
        build(tmp_path, config_dir, ManualClock(START), positions=[{"symbol": "AAPL", "qty": "3", "side": "long"}])


def test_a_blocked_account_is_refused(tmp_path, config_dir, paper_permitted):
    broker = Broker()
    broker.account = dict(Broker.account, trading_blocked=True)
    with pytest.raises(Exception, match="blocked"):
        build(tmp_path, config_dir, ManualClock(START), broker=broker)


def test_a_risk_service_for_another_account_is_refused(tmp_path, config_dir, paper_permitted):
    with pytest.raises(ConfigError, match="another mode, policy or account"):
        build(tmp_path, config_dir, ManualClock(START), alias="backtest-sim")


def test_the_backtest_runtime_config_cannot_be_used_for_paper(tmp_path, config_dir, paper_permitted):
    from trading_agent.core.mode import load_runtime_config
    policy = load_risk_policy(config_dir / "risk_policy.v3.yaml")
    with pytest.raises(ConfigError, match="for PAPER"):
        build_paper_runner(tmp_path / "run", load_runtime_config(ROOT / "config" / "runtime.backtest.yaml"), policy,
                           InstrumentRegistry.load(config_dir / "instruments.v2.yaml"), Secrets(), None, None, None,
                           ManualClock(START))
