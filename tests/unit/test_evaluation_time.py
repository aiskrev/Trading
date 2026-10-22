"""The planner payload carries the orchestrator's clock as `evaluation_time` (roadmap P2)."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from trading_agent.contracts.account import AccountSnapshot
from trading_agent.contracts.features import FeatureSet
from trading_agent.core.types import IdFactory
from trading_agent.planning.planner import ClaudePlanner
from trading_agent.planning.prompts import PromptSpec
from trading_agent.testing.harness import NO_TRADE_JSON, PASS_JSON, T0, Harness, market, trade_json


def _planner():
    llm = type("NoLLM", (), {"model_version": "none"})()
    return ClaudePlanner(llm, PromptSpec("trading_planner", "v1", "", "0" * 64), "s.v1", {"name": "t"}, ["AAA"],
                         IdFactory("t"))


def _inputs():
    m = market()
    a = AccountSnapshot.build(as_of=T0, cash=Decimal(100000), positions={}, open_orders=[],
                              realized_pnl_today=Decimal(0), orders_today=0, equity_high_water=Decimal(100000))
    f = FeatureSet(snapshot_id=m.snapshot_id, feature_version="features.v1", values={"AAA": {}})
    return m, a, f


def test_evaluation_time_is_utc_iso_and_matches_the_other_timestamps_format():
    m, a, f = _inputs()
    payload = _planner().build_payload(m, a, f, None, T0)
    assert payload["evaluation_time"] == "2026-01-05T14:30:00Z" == payload["market_snapshot"]["as_of"]
    local = datetime(2026, 1, 5, 16, 30, 0, 250000, tzinfo=timezone(timedelta(hours=2)))
    assert _planner().build_payload(m, a, f, None, local)["evaluation_time"] == "2026-01-05T14:30:00.250000Z"


def test_without_a_clock_the_key_is_absent_and_a_naive_time_is_refused():
    m, a, f = _inputs()
    assert "evaluation_time" not in _planner().build_payload(m, a, f)
    with pytest.raises(ValueError):
        _planner().build_payload(m, a, f, None, datetime(2026, 1, 5, 14, 30))


def test_the_pipeline_sends_its_own_clock_to_the_planner():
    seen = []

    def planner_fn(payload):
        seen.append(payload)
        return NO_TRADE_JSON
    h = Harness(planner_fn=planner_fn, critic_fn=lambda p: PASS_JSON)
    h.tick(advance_seconds=0)
    h.tick(advance_seconds=7)
    assert [p["evaluation_time"] for p in seen] == ["2026-01-05T14:30:00Z", "2026-01-05T14:30:07Z"]


def test_a_changed_clock_changes_the_request_key_so_replays_cannot_mix_times():
    from trading_agent.planning.recorded_llm import request_key
    m, a, f = _inputs()
    p = _planner()
    k1 = request_key("trading_planner", "v1", "s", p.build_payload(m, a, f, None, T0))
    k2 = request_key("trading_planner", "v1", "s", p.build_payload(m, a, f, None, T0 + timedelta(seconds=1)))
    assert k1 != k2
