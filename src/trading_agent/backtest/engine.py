from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple, Union

from pydantic import Field

from trading_agent.audit.auditor import AuditReport, build_report
from trading_agent.audit.ledger import AuditLedger
from trading_agent.backtest.portfolio import Portfolio
from trading_agent.contracts.base import Contract
from trading_agent.contracts.execution import Fill
from trading_agent.contracts.market import InstrumentStatus, MarketSnapshot, NewsItem, VenueStatus
from trading_agent.core.clock import SimulatedClock
from trading_agent.core.errors import ConfigError
from trading_agent.core.mode import Mode, RuntimeConfig, assert_mode_permitted
from trading_agent.core.signing import OrderSigner
from trading_agent.core.trading_state import TradingState, TradingStateMachine
from trading_agent.core.types import IdFactory
from trading_agent.execution.cost_model import CostModel
from trading_agent.execution.registry import create_execution_port
from trading_agent.features.pipeline import FeaturePipeline
from trading_agent.market_data.ports import MarketDataSource, RawTick
from trading_agent.normalization.normalizer import Normalizer
from trading_agent.observability.metrics import Metrics
from trading_agent.orchestration.tick import TradingPipeline
from trading_agent.planning.critic import ClaudeCritic
from trading_agent.planning.planner import ClaudePlanner
from trading_agent.planning.ports import LLMClient
from trading_agent.planning.prompts import load_prompt
from trading_agent.reconciliation.reconciler import Reconciler
from trading_agent.risk.approvals import require_approval
from trading_agent.risk.gateway import RiskGateway
from trading_agent.risk.policy import RiskPolicy

DEFAULT_PROMPTS_DIR = Path(__file__).resolve().parents[3] / "prompts"
DEFAULT_CONFIG_DIR = Path(__file__).resolve().parents[3] / "config"

DEMO_STRATEGY_RULES = {
    "name": "mean-reversion-demo",
    "entry": "BUY when dev_from_sma_10_bps <= -30 and flat",
    "exit": "SELL full position when dev_from_sma_10_bps >= 0",
    "order_type": "LIMIT at touch",
}


class BacktestReport(Contract):
    run_id: str
    ticks: int
    fills: List[Fill]
    final_cash: Decimal
    final_positions: Dict[str, Decimal]
    realized_pnl: Decimal
    trading_state: TradingState
    ledger_head: str
    metrics: Dict[str, int]
    audit: AuditReport
    state_history: List[str] = Field(default_factory=list)


class BacktestEngine:
    def __init__(self, config: RuntimeConfig, policy: RiskPolicy, planner_llm: LLMClient,
                 critic_llm: Optional[LLMClient], primary: MarketDataSource,
                 secondary: Optional[MarketDataSource] = None, news: Sequence[NewsItem] = (),
                 venue_status: Callable[[datetime], VenueStatus] = lambda t: VenueStatus.OPEN,
                 instrument_status: Callable[[datetime, str], InstrumentStatus] = lambda t, s: InstrumentStatus.TRADING,
                 cost_model: Optional[CostModel] = None, starting_cash: Decimal = Decimal(100000),
                 run_id: str = "bt", ledger_path: Optional[Union[str, Path]] = None,
                 prompts_dir: Union[str, Path] = DEFAULT_PROMPTS_DIR,
                 strategy_rules: Optional[dict] = None,
                 config_dir: Union[str, Path] = DEFAULT_CONFIG_DIR) -> None:
        assert_mode_permitted(config.mode)
        if config.mode is not Mode.BACKTEST:
            raise ValueError("BacktestEngine runs only in BACKTEST mode")
        if config.risk_policy_version != policy.version:
            raise ConfigError(f"runtime config expects {config.risk_policy_version}, loaded {policy.version}")
        self.approval = require_approval(policy, config.mode, config_dir)
        # Historical fixtures carry no halt flags; the default declares every quoted symbol TRADING.
        # Replace it with real halt data when backtesting periods that contain halts.
        self.instrument_status = instrument_status
        self.config, self.policy = config, policy
        self.primary, self.secondary = primary, secondary
        self.news = sorted(news, key=lambda n: n.ts)
        self.venue_status = venue_status
        self.run_id = run_id

        self.ids = IdFactory(run_id)
        self.clock: Optional[SimulatedClock] = None
        self.ledger = AuditLedger(ledger_path)
        self.state = TradingStateMachine()
        self.metrics = Metrics()
        self.portfolio = Portfolio(starting_cash)
        self.features = FeaturePipeline()
        self.normalizer = Normalizer(primary.source_id, secondary.source_id if secondary else None)
        self._current: Optional[MarketSnapshot] = None
        self._llms = (planner_llm, critic_llm)
        self._cost = cost_model or CostModel()
        self._prompts_dir = prompts_dir
        self._strategy_rules = strategy_rules or DEMO_STRATEGY_RULES

    def _build_pipeline(self, start: datetime) -> TradingPipeline:
        self.clock = SimulatedClock(start)
        signer = OrderSigner()  # shared only by gateway (sign) and execution port (verify)
        planner_llm, critic_llm = self._llms
        planner = ClaudePlanner(planner_llm, load_prompt(self._prompts_dir, "trading_planner",
                                                         self.config.prompt_versions["trading_planner"]),
                                self.config.strategy_version, self._strategy_rules, sorted(self.policy.universe),
                                self.ids)
        critic = None
        if critic_llm is not None:
            critic = ClaudeCritic(critic_llm, load_prompt(self._prompts_dir, "risk_critic",
                                                          self.config.prompt_versions["risk_critic"]))
        gateway = RiskGateway(self.policy, self.config.mode, signer, self.ids, self.clock)
        broker = create_execution_port(self.config.mode, signer=signer, cost_model=self._cost,
                                       quote_for=lambda s: self._current.quotes.get(s) if self._current else None,
                                       clock=self.clock, ids=self.ids)
        self.broker = broker
        return TradingPipeline(self.config, planner, critic, gateway, broker, self.ledger, self.state,
                               Reconciler(), self.portfolio, self.clock, self.metrics)

    def run(self) -> BacktestReport:
        events: List[RawTick] = list(self.primary.ticks()) + (list(self.secondary.ticks()) if self.secondary else [])
        if not events:
            raise ValueError("no market data")
        events.sort(key=lambda t: (t.ts, t.source_id, t.symbol))
        timeline = sorted({t.ts for t in events if t.source_id == self.primary.source_id})
        pipeline = self._build_pipeline(timeline[0])

        latest: Dict[Tuple[str, str], RawTick] = {}
        i = 0
        for t in timeline:
            while i < len(events) and events[i].ts <= t:
                latest[(events[i].source_id, events[i].symbol)] = events[i]
                i += 1
            self.clock.advance_to(t)
            news = [n for n in self.news if n.ts <= t][-5:]
            symbols = {sym for _, sym in latest}
            result = self.normalizer.build(latest.values(), as_of=t, received_at=t,
                                           venue_status=self.venue_status(t), news=news,
                                           instrument_status={s: self.instrument_status(t, s) for s in symbols})
            self._current = result.snapshot
            features = self.features.update(result.snapshot)
            pipeline.tick(result.snapshot, features, result.issues)

        records = self.ledger.records
        fills = [f for r in records for f in r.fills]
        return BacktestReport(
            run_id=self.run_id, ticks=len(timeline), fills=fills, final_cash=self.portfolio.cash,
            final_positions=self.portfolio.quantities(), realized_pnl=self.portfolio.realized_pnl,
            trading_state=self.state.state, ledger_head=self.ledger.head_hash, metrics=self.metrics.snapshot(),
            audit=build_report(records),
            state_history=[f"{h.at.isoformat()} {h.from_state.value}->{h.to_state.value} {h.reason}"
                           for h in self.state.history],
        )
