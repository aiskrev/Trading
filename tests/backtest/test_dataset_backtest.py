"""Backtest realism requirements: no leakage, corporate actions, delistings, spreads, fees,
slippage, latency, calendar, partial fills, gross/net and cost sensitivity, no live recommendation."""
import shutil
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from trading_agent.backtest import run_dataset as rd
from trading_agent.backtest.dataset import DatasetSource, calendar_violations, load_dataset
from trading_agent.backtest.engine import BacktestEngine
from trading_agent.contracts.audit import AuditEventType
from trading_agent.contracts.market import MarketSnapshot, Quote, VenueStatus
from trading_agent.core.errors import ConfigError
from trading_agent.core.hashing import canonical_json
from trading_agent.core.types import Side
from trading_agent.execution.cost_model import load_execution_assumptions
from trading_agent.features.pipeline import FeaturePipeline
from trading_agent.market_data.ports import RawTick
from trading_agent.planning.demo_strategy import demo_critic_response, demo_planner_response
from trading_agent.planning.recorded_llm import ScriptedLLM
from trading_agent.testing.harness import ROOT, risk_policy, runtime_config

DS = "synthetic-ca-2026jan.v1"
EXEC = load_execution_assumptions(ROOT / "config" / "execution_assumptions.v1.yaml")


def engine(ticks=None, execution=EXEC, ds=None):
    ds = ds or load_dataset(DS)
    return BacktestEngine(runtime_config(), risk_policy(), ScriptedLLM(demo_planner_response, "demo"),
                          ScriptedLLM(demo_critic_response, "critic"), primary=DatasetSource(ticks or ds.ticks),
                          execution=execution, corporate_actions=ds.actions, listings=ds.listings,
                          dataset_version=DS, run_id="t")


@pytest.fixture(scope="module")
def runs():
    execu, base, stress = rd.run_with_sensitivity(DS, ROOT / "config" / "execution_assumptions.v1.yaml")
    return execu, base, stress


# ------------------------------------------------------------------ dataset integrity
def test_dataset_hashes_and_unknown_versions(tmp_path):
    with pytest.raises(ConfigError):
        load_dataset("no-such-dataset.v1")
    shutil.copytree(ROOT / "data" / "datasets" / DS, tmp_path / DS)
    q = tmp_path / DS / "quotes.csv"
    q.write_text(q.read_text(encoding="utf-8").replace(",AAA,99.", ",AAA,98.", 1), encoding="utf-8")
    with pytest.raises(ConfigError, match="does not match its manifest"):
        load_dataset(DS, root=tmp_path)


def test_calendar_rules():
    ds = load_dataset(DS)
    s = risk_policy().session
    assert calendar_violations(ds.ticks, s) == []
    assert not any(t.ts.date() == date(2026, 1, 19) for t in ds.ticks)  # MLK day: market closed
    t0 = ds.ticks[0]
    bad = [t0.model_copy(update={"ts": datetime(2026, 1, 17, 15, tzinfo=timezone.utc)}),   # Saturday
           t0.model_copy(update={"ts": datetime(2026, 1, 19, 15, tzinfo=timezone.utc)}),   # holiday
           t0.model_copy(update={"ts": datetime(2026, 1, 12, 21, 30, tzinfo=timezone.utc)})]  # after close
    assert len(calendar_violations(bad, s)) == 3


# ------------------------------------------------------------------ no future leakage
CUTOFF = datetime(2026, 1, 15, 18, 0, tzinfo=timezone.utc)   # 13:00 ET: the BBB entry bar


def _future_perturbed():
    """Same data before CUTOFF; after it, prices crash 40% and a fake split appears the next day."""
    from trading_agent.backtest.dataset import ActionType, CorporateAction
    ds = load_dataset(DS)
    f = Decimal("0.6")
    ticks = [t if t.ts < CUTOFF else t.model_copy(update={"bid": t.bid * f, "ask": t.ask * f, "last": t.last * f})
             for t in ds.ticks]
    actions = list(ds.actions) + [CorporateAction(ex_date=date(2026, 1, 16), symbol="AAA", type=ActionType.SPLIT,
                                                  value=Decimal(3))]
    return ds._replace(ticks=ticks, actions=sorted(actions, key=lambda a: (a.ex_date, a.symbol)))


def test_future_data_cannot_change_past_decisions():
    real = engine()
    real.run()
    fake = engine(ds=_future_perturbed(), ticks=_future_perturbed().ticks)
    fake.run()

    def decisions_before(e, t):
        return [canonical_json(r) for r in e.ledger.records
                if r.event_type in (AuditEventType.DECISION, AuditEventType.SCHEMA_FAILURE) and r.at < t]
    before = decisions_before(real, CUTOFF)
    assert len(before) > 1000 and before == decisions_before(fake, CUTOFF)
    # every record (incl. fills) is identical up to one bar before the cutoff
    one_bar = CUTOFF - timedelta(minutes=1)
    assert [canonical_json(r) for r in real.ledger.records if r.at < one_bar] == \
           [canonical_json(r) for r in fake.ledger.records if r.at < one_bar]
    # and the perturbation really matters afterwards (the test is not vacuous)
    assert [canonical_json(r) for r in real.ledger.records] != [canonical_json(r) for r in fake.ledger.records]


def test_latency_must_be_below_bar_spacing():
    with pytest.raises(ConfigError, match="latency"):
        engine(execution=EXEC.model_copy(update={"latency_ms": 60000})).run()


def test_fills_happen_after_decision_plus_latency():
    eng = engine()
    rep = eng.run()
    decided_at = {r.approved_order["order_id"]: r.at for r in eng.ledger.records
                  if r.event_type is AuditEventType.DECISION and r.approved_order}
    assert rep.fills
    for f in rep.fills:
        assert f.at >= decided_at[f.order_id] + timedelta(milliseconds=EXEC.latency_ms)
        assert f.at < decided_at[f.order_id] + timedelta(minutes=2)  # the next bar, not later


# ------------------------------------------------------------------ corporate actions / delisting
def test_split_is_applied_point_in_time(runs):
    _, (report, _), _ = runs
    aaa = [f for f in report.fills if f.symbol == "AAA"]
    buy, sell = aaa[0], aaa[1]
    assert buy.at.date() < date(2026, 1, 14) <= sell.at.date()
    assert sell.quantity == pytest.approx(buy.quantity * 2, abs=Decimal("0.001"))  # position doubled on ex-date
    assert any("SPLIT AAA 2:1" in e for e in report.events)
    assert report.trading_state.value == "NORMAL"  # broker and internal book both split: reconciliation holds


def test_feature_history_is_split_adjusted():
    def q(mid, minute):
        m = Decimal(mid)
        return Quote(bid=m - Decimal("0.01"), ask=m + Decimal("0.01"), bid_size=Decimal(1), ask_size=Decimal(1),
                     last=m, ts=datetime(2026, 1, 13, 15, minute, tzinfo=timezone.utc))

    def snap(mid, minute):
        return MarketSnapshot.build(as_of=q(mid, minute).ts, received_at=q(mid, minute).ts,
                                    venue_status=VenueStatus.OPEN, quotes={"AAA": q(mid, minute)}, source_ids=["p"])
    adjusted, raw = FeaturePipeline(), FeaturePipeline()
    for i in range(10):
        adjusted.update(snap("100.00", i))
        raw.update(snap("100.00", i))
    adjusted.adjust_history("AAA", Decimal("0.5"), Decimal(2))
    dev_adj = adjusted.update(snap("50.00", 10)).values["AAA"]["dev_from_sma_10_bps"]
    dev_raw = raw.update(snap("50.00", 10)).values["AAA"]["dev_from_sma_10_bps"]
    assert abs(dev_adj) < 1 and dev_raw < -4000  # unadjusted history would fake a -45% signal


def test_dividend_credited_on_ex_date(runs):
    _, (report, summary), _ = runs
    bbb_buy = next(f for f in report.fills if f.symbol == "BBB")
    assert summary.dividends == (bbb_buy.quantity * Decimal("0.25")).quantize(Decimal("0.01"))


def test_delisted_security_is_closed_not_dropped(runs):
    _, (report, _), _ = runs
    bbb_buy = next(f for f in report.fills if f.symbol == "BBB")
    note = next(e for e in report.events if "DELISTED BBB" in e)
    expected = (Decimal("46.80") - bbb_buy.price) * bbb_buy.quantity
    assert "46.80" in note and str(expected.quantize(Decimal("0.01"))) in note
    assert "BBB" not in report.final_positions
    assert any(f.symbol == "BBB" for f in report.fills)  # its history stays in the results


def test_quotes_after_delisting_are_rejected(tmp_path):
    shutil.copytree(ROOT / "data" / "datasets" / DS, tmp_path / DS)
    q = tmp_path / DS / "quotes.csv"
    q.write_text(q.read_text(encoding="utf-8") + "2026-01-22T15:00:00+00:00,BBB,40.00,40.02,500,500,40.01\n",
                 encoding="utf-8")
    import yaml
    from trading_agent.backtest.dataset import file_sha256
    m = yaml.safe_load((tmp_path / DS / "manifest.yaml").read_text(encoding="utf-8"))
    m["files"]["quotes.csv"] = file_sha256(q)
    (tmp_path / DS / "manifest.yaml").write_text(yaml.safe_dump(m), encoding="utf-8")
    with pytest.raises(ConfigError, match="outside its listing period"):
        load_dataset(DS, root=tmp_path)


# ------------------------------------------------------------------ execution realism
def test_partial_fill_against_displayed_size(runs):
    _, (report, summary), _ = runs
    first = report.fills[0]
    assert first.symbol == "AAA" and first.quantity == Decimal(30)   # thin bar showed 30 shares
    assert summary.partial_fills >= 1


def test_spreads_are_paid_at_the_touch(runs):
    _, (report, _), _ = runs
    for c in report.fill_costs:
        assert c.spread_and_slippage > 0  # buys at the ask / sells at the bid, never at mid


def test_fees_and_minimum_commission(runs):
    _, (report, _), _ = runs
    costs = EXEC.costs
    for f in report.fills:
        notional = f.quantity * f.price
        expected = max(costs.min_commission, notional * costs.commission_bps / 10000)
        if f.side is Side.SELL:
            expected += notional * costs.sell_fee_bps / 10000
        assert f.fee == expected.quantize(Decimal("0.01"))


def test_gross_minus_net_equals_costs(runs):
    _, (_, s), _ = runs
    assert s.gross.pnl - s.net.pnl == s.costs.total
    assert s.gross.pnl >= s.net.pnl


def test_doubled_costs_sensitivity(runs):
    _, (_, base), (_, x2) = runs
    assert x2.costs.fees == pytest.approx(base.costs.fees * 2, abs=Decimal("0.05"))
    assert x2.net.pnl <= base.net.pnl
    assert x2.gross.pnl == pytest.approx(base.gross.pnl, abs=Decimal("1"))  # same trades, same mids


def test_configurable_slippage_moves_fills_when_not_capped_by_limit():
    from trading_agent.execution.cost_model import CostModel
    q = Quote(bid=Decimal("99.99"), ask=Decimal("100.01"), bid_size=Decimal(1), ask_size=Decimal(1),
              last=Decimal(100), ts=datetime(2026, 1, 12, 15, tzinfo=timezone.utc))
    assert CostModel(slippage_bps=Decimal(10)).fill_price(Side.BUY, q, Decimal("101")) == Decimal("100.01") * Decimal("1.001")
    assert CostModel(slippage_bps=Decimal(10)).fill_price(Side.BUY, q, Decimal("100.01")) == Decimal("100.01")


# ------------------------------------------------------------------ reporting
def test_report_has_gross_net_sensitivity_and_no_recommendation(runs):
    execu, base, stress = runs
    text = rd.render(DS, load_dataset(DS).manifest, execu, base, stress)
    assert "| metric | gross | net | net, costs x2 |" in text
    assert text.count(rd.DISCLAIMER) == 2
    body = text.replace(rd.DISCLAIMER, "").lower()
    for phrase in ("recommend", "you should", "buy now", "sell now", "go live", "deploy"):
        assert phrase not in body
    assert "synthetic: **True**" in text


def test_simulator_and_assumptions_require_approval(tmp_path):
    assert rd.require_execution_approval(ROOT / "config" / "execution_assumptions.v1.yaml") == "APR-0002"
    for mode in ("PAPER", "FULL_CONFIGURED_LIVE"):
        from trading_agent.core.mode import Mode
        with pytest.raises(ConfigError, match="no approval"):
            rd.require_execution_approval(ROOT / "config" / "execution_assumptions.v1.yaml", Mode(mode))
    # an edited simulator (or assumptions file) is no longer covered by the approval
    for rel in ("src/trading_agent/execution/simulated_broker.py", "config/execution_assumptions.v1.yaml"):
        fake = tmp_path / "root"
        for p in rd.APPROVED_EXECUTION_FILES + ("config/execution_assumptions.v1.yaml", "config/approvals.yaml"):
            (fake / p).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(ROOT / p, fake / p)
        (fake / rel).write_bytes((fake / rel).read_bytes() + b"\n# edited\n")
        with pytest.raises(ConfigError, match="no approval"):
            rd.require_execution_approval(fake / "config" / "execution_assumptions.v1.yaml", root=fake)
        shutil.rmtree(fake)


def test_placeholder_dataset_id_is_refused(capsys):
    assert rd.main(["--dataset-version", "<ID>"]) == 2
    assert "placeholder" in capsys.readouterr().out
