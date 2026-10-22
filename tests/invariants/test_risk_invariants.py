"""Deterministic risk-gateway invariants: every approved order satisfies every limit (oracle),
each limit denies on its own, sizes are monotone, and signed orders cannot be forged."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st

from risk_oracle import violations
from trading_agent.contracts.account import AccountSnapshot, BrokerAccountView, OpenOrder, Position
from trading_agent.contracts.critic import CriticVerdict, CriticVerdictKind
from trading_agent.contracts.market import InstrumentStatus, VenueStatus
from trading_agent.contracts.proposal import Decision, OrderProposal, OrderType
from trading_agent.contracts.risk import RiskVerdict
from trading_agent.core.clock import SimulatedClock
from trading_agent.core.errors import DuplicateOrder, ForgedOrder
from trading_agent.core.mode import Mode
from trading_agent.core.signing import OrderSigner
from trading_agent.core.trading_state import TradingState
from trading_agent.core.types import IdFactory, Side
from trading_agent.execution.cost_model import CostModel
from trading_agent.execution.simulated_broker import SimulatedBroker
from trading_agent.risk.gateway import RiskGateway
from trading_agent.risk.rules import CAPS, GATE_RULES
from trading_agent.testing.harness import T0, market, quote, risk_policy

POLICY = risk_policy()
SETTINGS = settings(max_examples=400, deadline=None, suppress_health_check=[HealthCheck.too_slow])
D = Decimal


# ------------------------------------------------------------------ builders
def gateway(policy=POLICY, signer=None, clock=None, **kw):
    return RiskGateway(policy, Mode.BACKTEST, signer or OrderSigner(), IdFactory("inv"), clock or SimulatedClock(T0), **kw)


def account(held=D(0), bbb=D(0), cash=D(100000), pnl=D(0), orders=0, open_orders=(), hw="auto", as_of=T0, extra=None):
    positions = {}
    if held:
        positions["AAA"] = Position(symbol="AAA", quantity=held, avg_price=D(100))
    if bbb:
        positions["BBB"] = Position(symbol="BBB", quantity=bbb, avg_price=D(50))
    for sym, qty in (extra or {}).items():
        positions[sym] = Position(symbol=sym, quantity=qty, avg_price=D(100))
    if hw == "auto":
        hw = cash + held * 100 + bbb * 50 + sum((q * 100 for q in (extra or {}).values()), D(0))
        hw = hw if hw > 0 else D(1)
    return AccountSnapshot.build(as_of=as_of, cash=cash, positions=positions, open_orders=list(open_orders),
                                 realized_pnl_today=pnl, orders_today=orders, equity_high_water=hw)


def broker_for(a, as_of=T0, **override):
    fields = dict(source_id="sim", as_of=as_of, positions={s: p.quantity for s, p in a.positions.items()},
                  open_order_count=len(a.open_orders))
    fields.update(override)
    return BrokerAccountView(**fields)


def proposal(m, a, symbol="AAA", side=Side.BUY, limit=D("100.01"), notional=D(5000), pid="p1"):
    return OrderProposal(decision=Decision.TRADE, symbol=symbol, side=side, order_type=OrderType.LIMIT,
                         limit_price=limit, requested_notional=notional, invalidation_conditions=["x"],
                         evidence_refs=[f"quote:{symbol}"], public_rationale="r", proposal_id=pid,
                         market_snapshot_id=m.snapshot_id, account_snapshot_id=a.snapshot_id,
                         model_version="m", prompt_version="p", prompt_sha256="0" * 64, strategy_version="s")


def critic(pid="p1", kind=CriticVerdictKind.PASS, max_notional=None):
    return CriticVerdict(verdict=kind, max_notional=max_notional, reason_codes=[], evidence_refs=[],
                         proposal_id=pid, model_version="c", prompt_version="c")


def run(m=None, a=None, p=None, c="pass", state=TradingState.NORMAL, broker="auto", gw=None, **pkw):
    m = m or market()
    a = a or account()
    p = p or proposal(m, a, **pkw)
    c = critic(p.proposal_id) if c == "pass" else c
    b = broker_for(a) if broker == "auto" else broker
    return (gw or gateway()).evaluate(p, c, m, a, state, None, b)


# --------------------------------------------- AT-08: oracle over state space
money = st.decimals(min_value=D("0.01"), max_value=D("2000000"), places=2)


@SETTINGS
@given(
    symbol=st.sampled_from(["AAA", "AAA", "BBB", "ZZZ"]),
    side=st.sampled_from([Side.BUY, Side.BUY, Side.SELL]),
    notional=money,
    offset_bps=st.integers(-80, 80),
    held=st.sampled_from([D(0), D(0), D(10), D(150), D(240)]),
    bbb=st.sampled_from([D(0), D(100), D(400), D(700)]),
    cash=st.sampled_from([D(100000), D(60000), D(5000), D(1500), D(500)]),
    pnl=st.sampled_from([D(0), D(-500), D(-2000), D(-5000)]),
    hw_bump=st.sampled_from([D(0), D(0), D(1000), D(10000)]),
    open_buy=st.sampled_from([None, None, "AAA", "BBB"]),
    orders=st.sampled_from([0, 0, 5, 20]),
    venue=st.sampled_from([VenueStatus.OPEN] * 3 + list(VenueStatus)),
    status=st.sampled_from([InstrumentStatus.TRADING] * 3 + list(InstrumentStatus) + [None]),
    state=st.sampled_from([TradingState.NORMAL] * 2 + list(TradingState)),
    critic_kind=st.sampled_from([CriticVerdictKind.PASS] * 2 + list(CriticVerdictKind) + [None]),
    critic_cap=money,
    quote_age=st.sampled_from([0, 0, 2, 4]),
    half_spread=st.sampled_from(["0.01", "0.01", "0.05", "0.20"]),
    broker_mode=st.sampled_from(["ok", "ok", "ok", "missing", "stale", "mismatch"]),
)
def test_every_approved_order_satisfies_every_limit(symbol, side, notional, offset_bps, held, bbb, cash, pnl,
                                                     hw_bump, open_buy, orders, venue, status, state, critic_kind,
                                                     critic_cap, quote_age, half_spread, broker_mode):
    qts = T0 - timedelta(seconds=quote_age)
    quotes = {"AAA": quote("100.00", half_spread, ts=qts), "BBB": quote("50.00", half_spread, ts=qts),
              "ZZZ": quote("10.00", ts=qts)}
    statuses = {s: InstrumentStatus.TRADING for s in quotes}
    if status is None:
        del statuses[symbol]
    else:
        statuses[symbol] = status
    m = market(quotes=quotes, venue=venue, status=statuses)
    oo = [OpenOrder(order_id="o1", symbol=open_buy, side=Side.BUY, notional=D(3000),
                    limit_price=D(1))] if open_buy else []
    a = account(held=held, bbb=bbb, cash=cash, pnl=pnl, orders=orders, open_orders=oo)
    a = AccountSnapshot.build(**{**a.model_dump(exclude={"snapshot_id"}),
                                 "equity_high_water": a.equity_high_water + hw_bump})
    b = {"ok": broker_for(a), "missing": None, "stale": broker_for(a, as_of=T0 - timedelta(seconds=60)),
         "mismatch": broker_for(a, positions={"AAA": held + 1})}[broker_mode]
    mid = quotes[symbol].mid
    p = proposal(m, a, symbol, side, (mid * (1 + D(offset_bps) / 10000)).quantize(D("0.01")), notional)
    c = None if critic_kind is None else critic(
        "p1", critic_kind, critic_cap if critic_kind is CriticVerdictKind.REDUCE else None)

    decision, order = gateway().evaluate(p, c, m, a, state, None, b)

    assert decision.approved_notional <= notional
    assert decision.policy_version == POLICY.version and decision.policy_sha256 == POLICY.source_sha256
    event(f"verdict={decision.verdict.value}")
    if order is None:
        assert decision.verdict is RiskVerdict.DENY and decision.approved_notional == 0
        return
    assert decision.verdict in (RiskVerdict.APPROVE, RiskVerdict.REDUCE)
    assert critic_kind is not None  # critic_required
    if state is TradingState.REDUCE_ONLY:
        assert side is Side.SELL and held > 0
    assert violations(order, decision, p, m, a, POLICY, T0, state, b, c) == []


def _near_valid_case(side, notional, offset_bps, held, bbb, cash, open_buy, open_notional, pnl, hw_bump,
                     critic_cap, broker_drift=D(0)):
    """All gates clean; only sizing, exposure, cash and price vary, so most cases reach the caps."""
    m = market()
    oo = [OpenOrder(order_id="o1", symbol=open_buy, side=Side.BUY, notional=open_notional,
                    limit_price=D(1))] if open_buy else []
    a = account(held=held, bbb=bbb, cash=cash, pnl=pnl, open_orders=oo)
    a = AccountSnapshot.build(**{**a.model_dump(exclude={"snapshot_id"}),
                                 "equity_high_water": a.equity_high_water + hw_bump})
    sym = "BBB" if open_buy == "AAA" else "AAA"  # avoid the duplicate-order gate
    mid = m.quotes[sym].mid
    limit = (mid * (1 + D(offset_bps) / 10000)).quantize(D("0.01"))
    p = proposal(m, a, sym, side, limit, notional)
    c = critic(kind=CriticVerdictKind.REDUCE, max_notional=critic_cap) if critic_cap else critic()
    b = broker_for(a, positions={**{s_: q.quantity for s_, q in a.positions.items()},
                                 **({"AAA": a.quantity("AAA") + broker_drift} if broker_drift else {})})
    decision, order = gateway().evaluate(p, c, m, a, TradingState.NORMAL, None, b)
    assert decision.approved_notional <= notional
    if order is not None:
        assert violations(order, decision, p, m, a, POLICY, T0, TradingState.NORMAL, b, c) == []
    return decision.verdict


NEAR_VALID = dict(
    side=st.sampled_from([Side.BUY, Side.BUY, Side.SELL]),
    notional=money,
    offset_bps=st.integers(-25, 25),
    held=st.sampled_from([D(0), D(20), D(120), D(199), D(260)]),
    bbb=st.sampled_from([D(0), D(200), D(500), D(800)]),
    cash=st.sampled_from([D(100000), D(50000), D(12000), D(3000), D(1200)]),
    open_buy=st.sampled_from([None, "AAA", "BBB"]),
    open_notional=st.sampled_from([D(500), D(8000), D(20000)]),
    pnl=st.sampled_from([D(0), D(-1999)]),
    hw_bump=st.sampled_from([D(0), D(1000), D(2900)]),
    critic_cap=st.sampled_from([None, None, D(150), D(2500), D(9000)]),
    broker_drift=st.sampled_from([D(0)] * 5 + [D(1), D("0.01")]),
)


@SETTINGS
@given(**NEAR_VALID)
def test_sizing_and_exposure_invariants_near_valid(**kw):
    event(f"verdict={_near_valid_case(**kw).value}")


def test_near_valid_generator_mostly_reaches_approval():
    """Guards against a vacuous property test: a fixed sample must produce many approvals."""
    import random
    rng = random.Random(0)
    choices = {"side": [Side.BUY, Side.BUY, Side.SELL], "held": [D(0), D(20), D(120), D(199), D(260)],
               "bbb": [D(0), D(200), D(500), D(800)], "cash": [D(100000), D(50000), D(12000), D(3000), D(1200)],
               "open_buy": [None, "AAA", "BBB"], "open_notional": [D(500), D(8000), D(20000)],
               "pnl": [D(0), D(-1999)], "hw_bump": [D(0), D(1000), D(2900)],
               "critic_cap": [None, None, D(150), D(2500), D(9000)]}
    verdicts = []
    for _ in range(300):
        kw = {k: rng.choice(v) for k, v in choices.items()}
        kw["notional"] = D(rng.randint(1, 2000000)) / 100
        kw["offset_bps"] = rng.randint(-14, 13)
        kw["broker_drift"] = D(0)
        verdicts.append(_near_valid_case(**kw))
    approved = sum(v is not RiskVerdict.DENY for v in verdicts)
    assert approved >= 90, f"only {approved}/300 approved"
    assert any(v is RiskVerdict.REDUCE for v in verdicts) and any(v is RiskVerdict.APPROVE for v in verdicts)


# ------------------------------------------------- each control denies alone
def _with(**kw):
    return dict(kw)


def _sector_policy():
    return risk_policy(instruments={"AAA": {"sector": "TECH"}, "BBB": {"sector": "ENERGY"},
                                    "CCC": {"sector": "TECH"}},
                       max_concentration_pct=D(1))


def _ccc_market(**kw):
    return market(quotes={"AAA": quote("100.00"), "BBB": quote("50.00"), "CCC": quote("100.00")}, **kw)


SAT = datetime(2026, 1, 10, 15, 0, tzinfo=timezone.utc)
HOLIDAY = datetime(2026, 1, 19, 15, 0, tzinfo=timezone.utc)
NEAR_CLOSE = datetime(2026, 1, 5, 20, 57, tzinfo=timezone.utc)   # 15:57 ET, inside avoid-close window
PRE_OPEN = datetime(2026, 1, 5, 14, 29, tzinfo=timezone.utc)     # 09:29 ET


def _at(t):
    m = market(quotes={"AAA": quote("100.00", ts=t), "BBB": quote("50.00", ts=t)}, as_of=t)
    a = account(as_of=t)
    return dict(m=m, a=a, broker=broker_for(a, as_of=t), gw=gateway(clock=SimulatedClock(t)))


CASES = {
    "OUTSIDE_UNIVERSE": lambda: dict(m=market(quotes={"AAA": quote("100.00"), "ZZZ": quote("10.00")}),
                                     symbol="ZZZ", limit=D("10.00")),
    "MARKET_NOT_OPEN": lambda: dict(m=market(venue=VenueStatus.HALTED)),
    "MARKET_STATE_UNKNOWN": lambda: dict(m=market(venue=VenueStatus.UNKNOWN)),
    "OUTSIDE_TRADING_SESSION@saturday": lambda: _at(SAT),
    "OUTSIDE_TRADING_SESSION@holiday": lambda: _at(HOLIDAY),
    "OUTSIDE_TRADING_SESSION@near_close": lambda: _at(NEAR_CLOSE),
    "OUTSIDE_TRADING_SESSION@pre_open": lambda: _at(PRE_OPEN),
    "INSTRUMENT_HALTED": lambda: dict(m=market(status={"AAA": InstrumentStatus.HALTED, "BBB": InstrumentStatus.TRADING})),
    "INSTRUMENT_STATUS_UNKNOWN": lambda: dict(m=market(status={"BBB": InstrumentStatus.TRADING})),
    "RESTRICTED_INSTRUMENT": lambda: dict(gw=gateway(policy=risk_policy(restricted=frozenset({"AAA"})))),
    "POSITION_LIMIT": lambda: dict(a=account(held=D(250), cash=D(75000)),
                                   gw=gateway(policy=risk_policy(max_concentration_pct=D(1)))),
    "GROSS_EXPOSURE_LIMIT": lambda: dict(a=account(held=D(240), bbb=D(520)),
                                         gw=gateway(policy=risk_policy(max_concentration_pct=D(1),
                                                                       max_net_exposure=D(50000)))),
    "NET_EXPOSURE_LIMIT": lambda: dict(a=account(held=D(200), bbb=D(400)),
                                       gw=gateway(policy=risk_policy(max_concentration_pct=D(1)))),
    "SECTOR_EXPOSURE_LIMIT": lambda: dict(m=_ccc_market(), a=account(held=D(150), extra={"CCC": D(150)}),
                                          gw=gateway(policy=_sector_policy())),
    "CONCENTRATION_LIMIT": lambda: dict(a=account(held=D(200), cash=D(80000))),
    "INSUFFICIENT_BUYING_POWER": lambda: dict(a=account(cash=D(900))),
    "DUPLICATE_OR_CONFLICTING_ORDER": lambda: dict(a=account(open_orders=[OpenOrder(
        order_id="o1", symbol="AAA", side=Side.SELL, notional=D(100), limit_price=D(101))])),
    "PRICE_OUTSIDE_COLLAR": lambda: dict(limit=D("101.00")),
    "SPREAD_TOO_WIDE": lambda: dict(m=market(quotes={"AAA": quote("100.00", "0.20")})),
    "SLIPPAGE_EXCEEDS_POLICY": lambda: dict(limit=D("100.20")),     # inside 50bp collar, 21bp worst-case cost
    "STALE_QUOTE": lambda: dict(m=market(quotes={"AAA": quote("100.00", ts=T0 - timedelta(seconds=4))})),
    "STALE_DATA": lambda: dict(m=market(as_of=T0 - timedelta(seconds=30))),
    "DAILY_LOSS_LIMIT": lambda: dict(a=account(pnl=-POLICY.daily_loss_limit)),
    "DRAWDOWN_LIMIT": lambda: dict(a=account(hw=D(110000))),
    "DRAWDOWN_UNKNOWN": lambda: dict(a=account(hw=None)),
    "UNMARKABLE_POSITION": lambda: dict(m=market(quotes={"AAA": quote("100.00")}), a=account(bbb=D(10))),
    "ORDER_RATE_LIMIT": lambda: dict(a=account(orders=POLICY.max_orders_per_day)),
    "BROKER_VIEW_MISSING": lambda: dict(broker=None),
    "BROKER_VIEW_STALE": lambda: dict(broker=broker_for(account(), as_of=T0 - timedelta(seconds=30))),
    "BROKER_ACCOUNT_MISMATCH@position": lambda: dict(broker=broker_for(account(), positions={"AAA": D(5)})),
    "BROKER_ACCOUNT_MISMATCH@orders": lambda: dict(broker=broker_for(account(), open_order_count=2)),
    "TRADING_HALTED": lambda: dict(state=TradingState.HALTED),
    "CRITIC_DENY": lambda: dict(c=critic(kind=CriticVerdictKind.DENY)),
    "CRITIC_MISSING": lambda: dict(c=None),
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_each_control_denies(case):
    code = case.split("@")[0]
    kw = CASES[case]()
    decision, order = run(**kw)
    assert order is None and decision.verdict is RiskVerdict.DENY
    assert code in decision.reason_codes, decision.reason_codes


def test_same_scenarios_pass_without_the_fault():
    """Sanity: the clean baseline used by CASES is approved, so each denial is caused by its fault."""
    decision, order = run()
    assert order is not None and decision.verdict is RiskVerdict.APPROVE


def test_exits_still_allowed_under_loss_drawdown_and_reduce_only():
    a = account(held=D(10), pnl=-POLICY.daily_loss_limit, hw=D(200000))
    for state in (TradingState.NORMAL, TradingState.REDUCE_ONLY):
        _, order = run(a=a, side=Side.SELL, limit=D("99.99"), notional=D(500), state=state)
        assert order is not None


def test_restricted_and_halted_block_exits_too():
    a = account(held=D(10))
    for kw in (dict(gw=gateway(policy=risk_policy(restricted=frozenset({"AAA"})))),
               dict(m=market(status={"AAA": InstrumentStatus.HALTED, "BBB": InstrumentStatus.TRADING}))):
        _, order = run(a=a, side=Side.SELL, limit=D("99.99"), notional=D(500), **kw)
        assert order is None


# -------------------------------------------------------- rate limits (stateful)
@settings(max_examples=150, deadline=None)
@given(steps=st.lists(st.tuples(st.integers(0, 40), st.sampled_from(["AAA", "BBB"])), min_size=1, max_size=30))
def test_order_rate_windows_never_exceeded(steps):
    clock = SimulatedClock(T0)
    gw = gateway(clock=clock)
    approved = []
    for gap, sym in steps:
        clock.advance(gap)
        now = clock.now()
        m = market(quotes={"AAA": quote("100.00", ts=now), "BBB": quote("50.00", ts=now)}, as_of=now)
        a = account(as_of=now)
        limit = D("100.01") if sym == "AAA" else D("50.01")
        _, order = gw.evaluate(proposal(m, a, sym, limit=limit, notional=D(1000)), critic(), m, a,
                               TradingState.NORMAL, None, broker_for(a, as_of=now))
        if order is not None:
            approved.append((now, sym))
    for t, _ in approved:
        window = [(u, s) for u, s in approved if t - timedelta(minutes=1) < u <= t]
        assert len(window) <= POLICY.max_orders_per_minute
        for sym in ("AAA", "BBB"):
            assert sum(1 for _, s in window if s == sym) <= POLICY.max_orders_per_instrument_per_minute


# ------------------------------------------------------------- monotonicity
@SETTINGS
@given(notional=money, loss=st.decimals(0, 5000, places=2), extra=st.decimals(0, 5000, places=2))
def test_more_loss_never_means_more_size(notional, loss, extra):
    """No martingale: more realized loss never increases approved size."""
    sizes = [run(a=account(pnl=-x), notional=notional)[0].approved_notional for x in (loss, loss + extra)]
    assert sizes[1] <= sizes[0]


@SETTINGS
@given(notional=money, held=st.decimals(0, 300, places=0), more=st.decimals(0, 300, places=0))
def test_more_existing_exposure_never_means_more_size(notional, held, more):
    sizes = []
    for h in (held, held + more):
        a = account(held=h, cash=D(100000) - h * 100)
        sizes.append(run(a=a, notional=notional)[0].approved_notional)
    assert sizes[1] <= sizes[0]


@SETTINGS
@given(notional=money, cap=money)
def test_critic_reduce_is_an_upper_bound(notional, cap):
    d, order = run(notional=notional, c=critic(kind=CriticVerdictKind.REDUCE, max_notional=cap))
    assert d.approved_notional <= min(notional, cap)


# ------------------------------------------------------------- fail closed
def _raises(ctx):
    raise RuntimeError("bug in rule")


@pytest.mark.parametrize("where", ["gate", "cap"])
def test_raising_rule_denies(where):
    kw = {"gate_rules": GATE_RULES + (_raises,)} if where == "gate" else {"caps": CAPS + (_raises,)}
    d, order = run(gw=gateway(**kw))
    assert order is None and "RULE_ERROR" in d.reason_codes


@pytest.mark.parametrize("bad", [lambda ctx: True, lambda ctx: None])
def test_rule_returning_garbage_denies(bad):
    d, order = run(gw=gateway(gate_rules=GATE_RULES + (bad,)))
    assert order is None and "RULE_ERROR" in d.reason_codes


def test_cap_returning_nan_denies():
    from trading_agent.risk.rules.context import Cap, ok
    d, order = run(gw=gateway(caps=CAPS + (lambda ctx: Cap(D("NaN"), ok("x")),)))
    assert order is None and "RULE_ERROR" in d.reason_codes


def test_no_trade_is_never_executable():
    m, a = market(), account()
    p = OrderProposal(decision=Decision.NO_TRADE, public_rationale="r", proposal_id="p", market_snapshot_id=m.snapshot_id,
                      account_snapshot_id=a.snapshot_id, model_version="m", prompt_version="p",
                      prompt_sha256="0" * 64, strategy_version="s")
    d, order = gateway().evaluate(p, None, m, a, TradingState.NORMAL, None, broker_for(a))
    assert order is None and d.verdict is RiskVerdict.DENY


def test_snapshot_and_critic_mismatch_denied():
    m, a = market(), account()
    other = market(as_of=T0 - timedelta(seconds=1))
    assert "MARKET_SNAPSHOT_MISMATCH" in run(m=m, a=a, p=proposal(other, a))[0].reason_codes
    assert "CRITIC_PROPOSAL_MISMATCH" in run(c=critic("someone-else"))[0].reason_codes


# ------------------------------------------------------------------ AT-12
def _sim(signer):
    m = market()
    return SimulatedBroker(signer, CostModel(), lambda s: m.quotes.get(s), SimulatedClock(T0), IdFactory("b"))


def _approved(signer):
    _, order = run(gw=gateway(signer=signer))
    assert order is not None
    return order


def test_tampered_order_is_rejected():
    signer = OrderSigner()
    order = _approved(signer)
    for change in ({"notional": order.notional * 10}, {"side": Side.SELL}, {"symbol": "BBB"},
                   {"limit_price": D("1")}, {"signature": "0" * 64}):
        with pytest.raises(ForgedOrder):
            _sim(signer).submit(order.model_copy(update=change))


def test_order_signed_by_another_signer_is_rejected():
    with pytest.raises(ForgedOrder):
        _sim(OrderSigner()).submit(_approved(OrderSigner()))


def test_non_order_objects_are_rejected():
    m, a = market(), account()
    with pytest.raises(ForgedOrder):
        _sim(OrderSigner()).submit(proposal(m, a))


def test_replayed_order_is_rejected():
    signer = OrderSigner()
    order, sim = _approved(signer), _sim(signer)
    sim.submit(order)
    with pytest.raises(DuplicateOrder):
        sim.submit(order)
