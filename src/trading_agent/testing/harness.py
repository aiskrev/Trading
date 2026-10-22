import json
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from decimal import Decimal
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

from trading_agent.audit.ledger import AuditLedger
from trading_agent.backtest.portfolio import Portfolio
from trading_agent.contracts.market import InstrumentStatus, MarketSnapshot, NewsItem, Quote, VenueStatus
from trading_agent.core.clock import SimulatedClock
from trading_agent.core.mode import RuntimeConfig, load_runtime_config
from trading_agent.core.signing import OrderSigner
from trading_agent.core.trading_state import TradingStateMachine
from trading_agent.core.types import IdFactory
from trading_agent.execution.cost_model import CostModel
from trading_agent.execution.registry import create_execution_port
from trading_agent.features.pipeline import FeaturePipeline
from trading_agent.normalization.validators import DataIssue
from trading_agent.orchestration.tick import TickResult, TradingPipeline
from trading_agent.planning.critic import ClaudeCritic
from trading_agent.planning.planner import ClaudePlanner
from trading_agent.planning.prompts import load_prompt
from trading_agent.planning.recorded_llm import ScriptedLLM
from trading_agent.reconciliation.reconciler import Reconciler
from trading_agent.risk.gateway import RiskGateway
from trading_agent.risk.policy import RiskPolicy, load_risk_policy

ROOT = Path(__file__).resolve().parents[3]
T0 = datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc)


def runtime_config() -> RuntimeConfig:
    return load_runtime_config(ROOT / "config" / "runtime.backtest.yaml")


POLICY_PATH = ROOT / "config" / "risk_policy.v2.yaml"


@lru_cache(maxsize=1)
def _base_policy() -> RiskPolicy:
    return load_risk_policy(POLICY_PATH)


def risk_policy(**overrides) -> RiskPolicy:
    """The released policy, or (tests only) a variant with overrides."""
    base = _base_policy()
    return RiskPolicy.model_validate({**base.model_dump(), **overrides}) if overrides else base


def quote(mid: str, half_spread: str = "0.01", ts: datetime = T0) -> Quote:
    m, h = Decimal(mid), Decimal(half_spread)
    return Quote(bid=m - h, ask=m + h, bid_size=Decimal(500), ask_size=Decimal(500), last=m, ts=ts)


def market(quotes: Optional[Dict[str, Quote]] = None, as_of: datetime = T0,
           venue: VenueStatus = VenueStatus.OPEN, news: Sequence[NewsItem] = (),
           status: Optional[Dict[str, InstrumentStatus]] = None) -> MarketSnapshot:
    if quotes is None:
        quotes = {"AAA": quote("100.00", ts=as_of), "BBB": quote("50.00", ts=as_of)}
    if status is None:
        status = {s: InstrumentStatus.TRADING for s in quotes}
    return MarketSnapshot.build(as_of=as_of, received_at=as_of, venue_status=venue, quotes=quotes,
                                instrument_status=status, news=list(news), source_ids=["primary"])


def trade_json(symbol: str = "AAA", side: str = "BUY", limit: str = "100.01", notional: str = "5000",
               refs: Optional[List[str]] = None, **extra) -> str:
    body = {
        "decision": "TRADE", "symbol": symbol, "side": side, "order_type": "LIMIT",
        "limit_price": limit, "requested_notional": notional,
        "invalidation_conditions": ["price moves 1% against entry"],
        "evidence_refs": refs if refs is not None else [f"quote:{symbol}", "venue:status"],
        "public_rationale": "test proposal",
    }
    body.update(extra)
    return json.dumps(body)


NO_TRADE_JSON = json.dumps({"decision": "NO_TRADE", "public_rationale": "nothing to do"})
PASS_JSON = json.dumps({"verdict": "PASS", "reason_codes": [], "evidence_refs": []})


class Harness:
    """A full single-tick pipeline with scripted LLMs and the BACKTEST SimulatedBroker."""

    def __init__(self, planner_fn: Callable[[dict], str], critic_fn: Optional[Callable[[dict], str]] = None,
                 policy: Optional[RiskPolicy] = None, starting_cash: Decimal = Decimal(100000),
                 start: datetime = T0, run_id: str = "t") -> None:
        self.config = runtime_config()
        self.policy = policy or risk_policy()
        self.clock = SimulatedClock(start)
        self.ids = IdFactory(run_id)
        self.signer = OrderSigner()
        self.ledger = AuditLedger()
        self.state = TradingStateMachine()
        self.portfolio = Portfolio(starting_cash)
        self.features = FeaturePipeline()
        self.current: Optional[MarketSnapshot] = None
        self.gateway = RiskGateway(self.policy, self.config.mode, self.signer, self.ids, self.clock)
        self.broker = create_execution_port(self.config.mode, signer=self.signer, cost_model=CostModel(),
                                            quote_for=lambda s: self.current.quotes.get(s) if self.current else None,
                                            clock=self.clock, ids=self.ids)
        planner = ClaudePlanner(ScriptedLLM(planner_fn, "scripted-planner"),
                                load_prompt(ROOT / "prompts", "trading_planner", "v1"),
                                self.config.strategy_version, {"name": "test"}, sorted(self.policy.universe), self.ids)
        critic = None
        if critic_fn is not None:
            critic = ClaudeCritic(ScriptedLLM(critic_fn, "scripted-critic"),
                                  load_prompt(ROOT / "prompts", "risk_critic", "v1"))
        self.pipeline = TradingPipeline(self.config, planner, critic, self.gateway, self.broker, self.ledger,
                                        self.state, Reconciler(), self.portfolio, self.clock)

    def tick(self, snapshot: Optional[MarketSnapshot] = None, issues: Sequence[DataIssue] = (),
             advance_seconds: int = 0) -> TickResult:
        if advance_seconds:
            self.clock.advance_to(self.clock.now() + timedelta(seconds=advance_seconds))
        snapshot = snapshot or market(as_of=self.clock.now())
        self.current = snapshot
        return self.pipeline.tick(snapshot, self.features.update(snapshot), list(issues))
