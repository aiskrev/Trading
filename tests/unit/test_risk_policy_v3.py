"""risk-policy.v3: real US symbols for the Alpaca paper account (roadmap P3 acceptance test 5 and P6 sizing).

Checks the released file is pinned and loads, that the instrument registry, the universe file and the policy
universe are the same set, that every sector has a limit, that sizing is coherent with whole-share orders,
the 2026-2027 NYSE holidays, and that the gateway applies it (a non-universe symbol is denied, an order is
capped at max_order_notional).
"""
from datetime import date, datetime, timezone
from decimal import Decimal

import yaml

from trading_agent.contracts.account import AccountSnapshot, BrokerAccountView
from trading_agent.contracts.critic import CriticVerdict, CriticVerdictKind
from trading_agent.contracts.proposal import Decision, OrderProposal, OrderType
from trading_agent.core.clock import SimulatedClock
from trading_agent.core.mode import Mode
from trading_agent.core.signing import EphemeralSigner
from trading_agent.core.trading_state import TradingState
from trading_agent.core.types import IdFactory, Side
from trading_agent.market_data.instruments import InstrumentRegistry
from trading_agent.risk.gateway import RiskGateway
from trading_agent.risk.policy import load_risk_policy
from trading_agent.testing.harness import ROOT, market, quote

CONFIG = ROOT / "config"
POLICY = load_risk_policy(CONFIG / "risk_policy.v3.yaml")
NYSE_HOLIDAYS = {"2026-11-26", "2026-12-25", "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31",
                 "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24"}
# A Tuesday in November 2026, 11:00 New York (16:00 UTC): inside the session.
T = datetime(2026, 11, 17, 16, 0, tzinfo=timezone.utc)


def test_v3_is_released_pinned_and_loads():
    assert POLICY.version == "risk-policy.v3" and POLICY.restricted_list_version == "restricted.v1"
    lock = yaml.safe_load((CONFIG / "risk_config.lock.yaml").read_text(encoding="utf-8"))["files"]
    assert lock["risk_policy.v3.yaml"] == POLICY.source_sha256


def test_registry_universe_and_policy_name_the_same_symbols():
    registry = InstrumentRegistry.load(CONFIG / "instruments.v2.yaml")
    universe = yaml.safe_load((CONFIG / "universe.v2.yaml").read_text(encoding="utf-8"))
    assert registry.version == "instruments.v2" and universe["version"] == "universe.v2"
    assert set(registry.symbols) == set(universe["symbols"]) == set(POLICY.universe)
    assert all(registry.normalize("alpaca", s) == s for s in POLICY.universe)        # Alpaca tickers are canonical


def test_every_sector_has_a_limit_and_limits_nest():
    sectors = {i.sector for i in POLICY.instruments.values()}
    assert sectors == set(POLICY.max_sector_exposure)
    assert POLICY.min_order_notional <= POLICY.max_order_notional <= POLICY.max_position_notional \
        <= POLICY.max_gross_exposure
    assert all(v <= POLICY.max_gross_exposure for v in POLICY.max_sector_exposure.values())
    assert not POLICY.allow_short and POLICY.critic_required and POLICY.require_instrument_status \
        and POLICY.require_broker_view


def test_sizing_keeps_orders_at_one_whole_share_or_more():
    # The execution port floors to whole shares. min_order_notional must exceed any plausible share price in the
    # universe (all are index ETFs or large caps trading well below 1000 USD); this pins the assumption.
    assert POLICY.min_order_notional >= Decimal(1000)
    assert POLICY.max_order_notional <= Decimal(5000)       # 5% of the 100k paper account


def test_loss_and_rate_limits_are_tighter_than_the_synthetic_policy():
    v2 = load_risk_policy(CONFIG / "risk_policy.v2.yaml")
    assert POLICY.daily_loss_limit < v2.daily_loss_limit and POLICY.max_drawdown_pct < v2.max_drawdown_pct
    assert POLICY.max_orders_per_day < v2.max_orders_per_day
    assert POLICY.max_order_notional <= v2.max_order_notional


def test_nyse_holidays_and_session_window():
    s = POLICY.session
    assert {d.isoformat() for d in s.holidays} == NYSE_HOLIDAYS
    assert all(d.weekday() < 5 for d in s.holidays)
    assert s.is_open(T)
    assert not s.is_open(T.replace(hour=14, minute=33))                      # 09:33: first 5 minutes avoided
    assert not s.is_open(T.replace(hour=20, minute=55))                      # 15:55: last 10 minutes avoided
    assert not s.is_open(datetime(2026, 11, 26, 16, 0, tzinfo=timezone.utc))  # Thanksgiving
    assert not s.is_open(datetime(2026, 11, 21, 16, 0, tzinfo=timezone.utc))  # Saturday
    assert s.is_open(datetime(2027, 7, 1, 15, 0, tzinfo=timezone.utc))      # summer: 11:00 EDT
    assert date(2027, 7, 4) not in s.holidays                                # Sunday; observed on the 5th


def _account():
    cash = Decimal(100000)
    return AccountSnapshot.build(as_of=T, cash=cash, positions={}, open_orders=[], realized_pnl_today=Decimal(0),
                                 orders_today=0, equity_high_water=cash)


def _evaluate(symbol, notional, mid="500.00"):
    quotes = {s: quote("500.00", "0.02", ts=T) for s in POLICY.universe}
    quotes[symbol] = quote(mid, "0.02", ts=T)
    m, a = market(quotes=quotes, as_of=T), _account()
    p = OrderProposal(decision=Decision.TRADE, symbol=symbol, side=Side.BUY, order_type=OrderType.LIMIT,
                      limit_price=Decimal(mid) + Decimal("0.03"), requested_notional=Decimal(notional),
                      invalidation_conditions=["x"], evidence_refs=[f"quote:{symbol}"], public_rationale="r",
                      proposal_id="p1", market_snapshot_id=m.snapshot_id, account_snapshot_id=a.snapshot_id,
                      model_version="m", prompt_version="p", prompt_sha256="0" * 64, strategy_version="s")
    c = CriticVerdict(verdict=CriticVerdictKind.PASS, max_notional=None, reason_codes=[], evidence_refs=[],
                      proposal_id="p1", model_version="c", prompt_version="c")
    b = BrokerAccountView(source_id="alpaca", as_of=T, positions={}, open_order_count=0)
    gw = RiskGateway(POLICY, Mode.BACKTEST, EphemeralSigner(), IdFactory("v3"), SimulatedClock(T))
    return gw.evaluate(p, c, m, a, TradingState.NORMAL, None, b)


def test_the_gateway_applies_v3_to_real_symbols():
    decision, order = _evaluate("AAPL", "20000")
    assert order is not None and order.notional == POLICY.max_order_notional        # capped at 5,000
    assert int(order.notional / order.limit_price) >= 1                             # at least one whole share
    decision, order = _evaluate("TSLA", "2000")                                     # not in the universe
    assert order is None and "OUTSIDE_UNIVERSE" in decision.reason_codes
    decision, order = _evaluate("MSFT", "500")                                      # below the minimum
    assert order is None and "BELOW_MIN_NOTIONAL" in decision.reason_codes
