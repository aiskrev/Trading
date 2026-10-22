"""Pre-evaluation gate: every prerequisite, UNKNOWN => NO_TRADE + reason_code, model only on a trigger."""
import shutil
from datetime import timedelta
from decimal import Decimal

import pytest
import yaml
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from trading_agent.backtest.dataset import file_sha256
from trading_agent.contracts.account import AccountSnapshot, BrokerAccountView, Position
from trading_agent.contracts.features import FeatureSet
from trading_agent.contracts.market import InstrumentStatus, VenueStatus
from trading_agent.contracts.proposal import Decision
from trading_agent.core.mode import RuntimeConfig
from trading_agent.core.trading_state import TradingState
from trading_agent.orchestration.evaluation_gate import TRIGGER_CODE, KillSwitchState, build_gate
from trading_agent.testing.harness import PASS_JSON, ROOT, T0, Harness, market, quote, risk_policy, runtime_config, \
    trade_json

D = Decimal
STRATEGY = "mean-reversion-demo.v1"


@pytest.fixture()
def approved_config(tmp_path):
    """A copy of config/ with an extra (test-only) approval for the strategy + trigger code."""
    cfg = tmp_path / "config"
    shutil.copytree(ROOT / "config", cfg)
    raw = yaml.safe_load((cfg / "approvals.yaml").read_text(encoding="utf-8"))
    raw["approvals"].append({
        "id": "APR-9999", "date": "2026-10-22", "approver": "test", "approved_via": "test fixture",
        "statement": "test only", "modes": ["BACKTEST"], "code_scope": "test",
        "files": {f"config/strategies/{STRATEGY}.yaml": file_sha256(cfg / "strategies" / f"{STRATEGY}.yaml"),
                  TRIGGER_CODE: file_sha256(ROOT / TRIGGER_CODE)}})
    (cfg / "approvals.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8")
    return cfg


def gate(config_dir, cfg=None, kill=()):
    return build_gate(cfg or runtime_config(), risk_policy(), list(kill), config_dir=config_dir)


def acct(held=D(0), as_of=T0):
    pos = {"AAA": Position(symbol="AAA", quantity=held, avg_price=D(100))} if held else {}
    return AccountSnapshot.build(as_of=as_of, cash=D(100000), positions=pos, equity_high_water=D(100000))


def feats(m, dev=D(-50), sym="AAA", snapshot_id=None):
    return FeatureSet(snapshot_id=snapshot_id or m.snapshot_id, feature_version="f",
                      values={sym: {"dev_from_sma_10_bps": dev}})


def broker(a, **kw):
    return BrokerAccountView(**{"source_id": "b", "as_of": T0, "positions": {s: p.quantity for s, p in
                                                                             a.positions.items()}, **kw})


def check(g, m=None, a="auto", b="auto", f="auto", state=TradingState.NORMAL, now=T0):
    m = m or market()
    a = acct() if a == "auto" else a
    b = broker(a) if b == "auto" and a is not None else (None if b == "auto" else b)
    f = feats(m) if f == "auto" else f
    return g.check(m, a, b, f, state, now)


# ------------------------------------------------------------------ approval of versions
def test_unapproved_strategy_is_never_operated(tmp_path):
    cfg = tmp_path / "config"
    shutil.copytree(ROOT / "config", cfg)
    raw = yaml.safe_load((cfg / "approvals.yaml").read_text(encoding="utf-8"))
    raw["approvals"] = [a for a in raw["approvals"] if not any(k.startswith("config/strategies/") for k in a["files"])]
    (cfg / "approvals.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8")
    r = check(gate(cfg))
    assert not r.ok and r.reason_code == "STRATEGY_NOT_APPROVED"


def test_real_config_approves_exactly_this_strategy_for_backtest_only():
    from trading_agent.core.mode import Mode
    from trading_agent.orchestration.evaluation_gate import strategy_approval
    assert strategy_approval(STRATEGY, Mode.BACKTEST) == "APR-0003"
    assert all(strategy_approval(STRATEGY, m) is None for m in Mode if m is not Mode.BACKTEST)


def test_wrong_risk_policy_version(approved_config):
    cfg = RuntimeConfig.model_validate({**runtime_config().model_dump(), "risk_policy_version": "risk-policy.v1"})
    assert check(gate(approved_config, cfg)).reason_code == "RISK_POLICY_NOT_APPROVED"


def test_unknown_strategy_version(approved_config):
    cfg = RuntimeConfig.model_validate({**runtime_config().model_dump(), "strategy_version": "nope.v9"})
    assert check(gate(approved_config, cfg)).reason_code == "STRATEGY_NOT_APPROVED"


def test_edited_strategy_file_loses_its_approval(approved_config):
    p = approved_config / "strategies" / f"{STRATEGY}.yaml"
    p.write_text(p.read_text(encoding="utf-8").replace('"-30"', '"-5"'), encoding="utf-8")
    assert check(gate(approved_config)).reason_code == "STRATEGY_NOT_APPROVED"


# ------------------------------------------------------------------ each prerequisite
CASES = {
    "MARKET_DATA_UNKNOWN": dict(m=market(quotes={})),
    "MARKET_DATA_STALE": dict(m=market(as_of=T0 - timedelta(seconds=30))),
    "ACCOUNT_UNKNOWN": dict(a=None, b=None),
    "ACCOUNT_STALE": dict(a=acct(as_of=T0 - timedelta(seconds=30))),
    "RECONCILIATION_UNKNOWN": dict(b=None),
    "RECONCILIATION_UNKNOWN@stale": dict(b=broker(acct(), as_of=T0 - timedelta(seconds=30))),
    "RECONCILIATION_MISMATCH": dict(b=broker(acct(), positions={"AAA": D(3)})),
    "TRADING_HALTED": dict(state=TradingState.HALTED),
    "MARKET_STATUS_UNKNOWN": dict(m=market(venue=VenueStatus.UNKNOWN)),
    "MARKET_CLOSED": dict(m=market(venue=VenueStatus.CLOSED)),
    "INSTRUMENT_STATUS_UNKNOWN": dict(m=market(status={})),
    "NO_ELIGIBLE_INSTRUMENT": dict(m=market(quotes={"ZZZ": quote("10.00")})),
    "FEATURES_UNKNOWN": dict(f=None),
    "FEATURES_UNKNOWN@other_snapshot": dict(f=feats(market(), snapshot_id="0" * 64)),
    "NO_TRIGGER": dict(f=feats(market(), dev=D(-10))),
    "NO_TRIGGER@missing_feature": dict(f=FeatureSet(snapshot_id=market().snapshot_id, feature_version="f", values={})),
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_each_prerequisite_yields_no_trade_with_reason(approved_config, case):
    r = check(gate(approved_config), **CASES[case])
    assert not r.ok and r.reason_code == case.split("@")[0] and r.triggers == []


def test_mode_not_permitted(approved_config):
    cfg = RuntimeConfig.model_validate({**runtime_config().model_dump(), "mode": "PAPER"})
    g = gate(approved_config, cfg)
    assert check(g).reason_code == "STRATEGY_NOT_APPROVED"      # no approval covers PAPER
    g.strategy_approval_id, g.risk_approval_id = "APR-X", "APR-Y"  # even if approvals existed ...
    assert check(g).reason_code == "MODE_NOT_PERMITTED"           # ... the stage still only permits BACKTEST


def test_kill_switch_active_and_unknown(approved_config, tmp_path, monkeypatch):
    ks = tmp_path / "KILL_SWITCH"
    g = gate(approved_config, kill=[ks])
    assert check(g).ok
    ks.write_text("operator: stop", encoding="utf-8")
    assert check(g).reason_code == "KILL_SWITCH_ACTIVE"
    ks.unlink()
    monkeypatch.setattr(type(ks), "exists", lambda self: (_ for _ in ()).throw(PermissionError("denied")))
    assert g.kill_switch.state() is KillSwitchState.UNKNOWN and check(g).reason_code == "KILL_SWITCH_UNKNOWN"


def test_restricted_symbol_is_outside_the_universe(approved_config):
    import trading_agent.orchestration.evaluation_gate as eg
    g = eg.build_gate(runtime_config(), risk_policy(restricted=frozenset({"AAA", "BBB"})), [],
                      config_dir=approved_config)
    g.risk_approval_id = "APR-0001"  # variant policy object for this test only
    assert check(g).reason_code == "NO_ELIGIBLE_INSTRUMENT"


def test_entry_and_exit_triggers(approved_config):
    g = gate(approved_config)
    entry = check(g)
    assert entry.ok and [(h.symbol, h.side) for h in entry.triggers] == [("AAA", "BUY")]
    held = acct(held=D(10))
    exit_ = check(g, a=held, b=broker(held), f=feats(market(), dev=D(5)))
    assert exit_.ok and [(h.symbol, h.side) for h in exit_.triggers] == [("AAA", "SELL")]
    no_add = check(g, a=held, b=broker(held), f=feats(market(), dev=D(-50)))
    assert no_add.reason_code == "NO_TRIGGER"  # never adds to a held position


# ------------------------------------------------------------------ inside the pipeline
def _harness(approved_config, planner_fn, kill=()):
    h = Harness(planner_fn=planner_fn, critic_fn=lambda p: PASS_JSON)
    h.pipeline.gate = gate(approved_config, kill=kill)
    return h


def _features_for(h, snapshot, dev):
    return FeatureSet(snapshot_id=snapshot.snapshot_id, feature_version="f", values={"AAA": {"dev_from_sma_10_bps": dev}})


def test_model_is_not_called_without_a_trigger(approved_config):
    calls = []
    h = _harness(approved_config, lambda p: calls.append(p) or trade_json())
    m = market()
    r = h.pipeline.tick(m, _features_for(h, m, D(-10)))
    assert calls == [] and r.proposal.decision is Decision.NO_TRADE and r.proposal.reason_code == "NO_TRIGGER"
    assert r.order is None
    rec = [x for x in h.ledger.records if x.event_type.value == "DECISION"][-1]
    assert rec.details["reason_code"] == "NO_TRIGGER" and rec.proposal.model_version == "none:deterministic-gate"


def test_model_sees_triggers_and_may_only_act_on_them(approved_config):
    seen = []
    h = _harness(approved_config, lambda p: seen.append(p) or trade_json(symbol="BBB", limit="50.01"))
    m = market()
    h.current = m
    r = h.pipeline.tick(m, _features_for(h, m, D(-50)))
    assert seen and seen[0]["active_triggers"] == [{"symbol": "AAA", "trigger": "ENTRY", "side": "BUY",
                                                    "feature_value": "-50"}]
    assert r.order is None and r.proposal.reason_code == "PROPOSAL_NOT_TRIGGERED"


def test_triggered_proposal_still_goes_through_the_risk_gateway(approved_config):
    h = _harness(approved_config, lambda p: trade_json())
    m = market()
    h.current = m
    r = h.pipeline.tick(m, _features_for(h, m, D(-50)))
    assert r.order is not None and r.decision.verdict.value in ("APPROVE", "REDUCE")
    assert r.order.notional <= risk_policy().max_order_notional


def test_kill_switch_halts_the_pipeline(approved_config, tmp_path):
    ks = tmp_path / "KILL_SWITCH"
    ks.write_text("stop", encoding="utf-8")
    calls = []
    h = _harness(approved_config, lambda p: calls.append(1) or trade_json(), kill=[ks])
    m = market()
    r = h.pipeline.tick(m, _features_for(h, m, D(-50)))
    assert calls == [] and r.order is None and h.state.state is TradingState.HALTED


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture])
@given(quotes_ok=st.booleans(), broker_ok=st.sampled_from(["ok", "mismatch"]),
       venue=st.sampled_from(list(VenueStatus)), status=st.sampled_from(list(InstrumentStatus) + [None]),
       features_ok=st.booleans(), dev=st.integers(-100, 50))
def test_any_unknown_prerequisite_means_no_trade_and_no_model_call(approved_config, quotes_ok, broker_ok, venue,
                                                                  status, features_ok, dev):
    calls = []
    h = _harness(approved_config, lambda p: calls.append(1) or trade_json())
    st_map = {} if status is None else {"AAA": status, "BBB": status}
    m = market(venue=venue, status=st_map) if quotes_ok else market(quotes={}, venue=venue)
    h.current = m
    if broker_ok == "mismatch":
        h.broker.broker_positions = lambda: {"AAA": D(1)}  # broker reports a position our book does not have
    f = _features_for(h, m, D(dev)) if features_ok else None
    r = h.pipeline.tick(m, f) if f is not None else h.pipeline.tick(m, FeatureSet(snapshot_id="0" * 64,
                                                                                feature_version="f", values={}))
    unknown = (not quotes_ok or broker_ok == "mismatch" or venue is not VenueStatus.OPEN
               or status is not InstrumentStatus.TRADING or not features_ok or dev > -30)
    if unknown:
        assert r.order is None and calls == []
        assert r.proposal is None or (r.proposal.decision is Decision.NO_TRADE and r.proposal.reason_code)
