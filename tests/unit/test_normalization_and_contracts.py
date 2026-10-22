from datetime import timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from trading_agent.contracts.market import MarketSnapshot, VenueStatus
from trading_agent.core.errors import ConfigError
from trading_agent.market_data.ports import RawTick
from trading_agent.normalization.normalizer import Normalizer
from trading_agent.normalization.validators import DataIssue, DataIssueCode
from trading_agent.orchestration.fail_closed import escalation_for
from trading_agent.core.trading_state import TradingState
from trading_agent.risk.policy import RiskPolicy, load_risk_policy
from trading_agent.testing.harness import ROOT, T0, market, risk_policy


def tick(source, symbol, bid, ask, ts=T0):
    return RawTick(source_id=source, symbol=symbol, ts=ts, bid=Decimal(bid), ask=Decimal(ask),
                   bid_size=Decimal(1), ask_size=Decimal(1), last=(Decimal(bid) + Decimal(ask)) / 2)


def build(ticks):
    return Normalizer("primary", "secondary").build(ticks, as_of=T0, received_at=T0, venue_status=VenueStatus.OPEN)


def test_snapshot_is_content_hashed_and_tamper_evident():
    snap = market()
    data = snap.model_dump()
    data["quotes"]["AAA"]["bid"] = Decimal("1")
    with pytest.raises(ValidationError):
        MarketSnapshot.model_validate(data)
    with pytest.raises(ValidationError):
        snap.venue_status = VenueStatus.CLOSED


def test_naive_timestamps_rejected():
    with pytest.raises(ValidationError):
        market(as_of=T0.replace(tzinfo=None))


@pytest.mark.parametrize("bid,ask,code", [("0", "1", DataIssueCode.BAD_PRICE),
                                          ("101", "100", DataIssueCode.CROSSED_QUOTE)])
def test_bad_ticks_are_dropped(bid, ask, code):
    result = build([tick("primary", "AAA", bid, ask), tick("primary", "BBB", "49.99", "50.01")])
    assert "AAA" not in result.snapshot.quotes and "BBB" in result.snapshot.quotes
    assert [i.code for i in result.issues] == [code]


def test_divergence_drops_symbol():
    result = build([tick("primary", "AAA", "99.99", "100.01"), tick("secondary", "AAA", "101.99", "102.01")])
    assert result.snapshot.quotes == {}
    assert result.issues[0].code is DataIssueCode.SOURCE_DIVERGENCE


def test_future_tick_is_clock_skew():
    result = build([tick("primary", "AAA", "99.99", "100.01", ts=T0 + timedelta(seconds=5))])
    assert result.issues[0].code is DataIssueCode.CLOCK_SKEW and result.snapshot.quotes == {}


def test_missing_primary_and_bad_symbol():
    result = build([tick("secondary", "AAA", "99.99", "100.01"), tick("primary", "bad sym", "1", "2")])
    assert {i.code for i in result.issues} == {DataIssueCode.MISSING_PRIMARY, DataIssueCode.BAD_SYMBOL}


def test_unknown_issue_code_halts():
    weird = DataIssue.model_construct(code="SOMETHING_NEW", symbol=None, detail="")
    assert escalation_for([weird])[0] is TradingState.HALTED


def test_policy_loads_and_rejects_incoherent_limits(tmp_path):
    p = load_risk_policy(ROOT / "config" / "risk_policy.v2.yaml")
    assert p.universe == frozenset({"AAA", "BBB"}) and p.version == "risk-policy.v2"
    for bad_override in ({"min_order_notional": Decimal("999999")}, {"max_sector_exposure": {"TECH": "1"}},
                         {"max_orders_per_instrument_per_minute": 99}, {"allow_short": True}):
        with pytest.raises(ValidationError):
            RiskPolicy.model_validate({**p.model_dump(), **bad_override})
    bad = tmp_path / "policy.yaml"
    bad.write_text("version: x\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_risk_policy(bad)
