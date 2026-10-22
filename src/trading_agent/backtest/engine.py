import bisect
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple, Union

from pydantic import Field

from trading_agent.audit.auditor import AuditReport, build_report
from trading_agent.audit.ledger import AuditLedger
from trading_agent.backtest.dataset import ActionType, CorporateAction, Listing
from trading_agent.backtest.portfolio import Portfolio
from trading_agent.contracts.audit import AuditEventType
from trading_agent.contracts.base import Contract
from trading_agent.contracts.execution import Fill
from trading_agent.contracts.market import InstrumentStatus, MarketSnapshot, NewsItem, Quote, VenueStatus
from trading_agent.core.clock import SimulatedClock
from trading_agent.core.errors import ConfigError
from trading_agent.core.mode import Mode, RuntimeConfig, assert_mode_permitted
from trading_agent.core.signing import BACKTEST_ACCOUNT_ALIAS, EphemeralSigner
from trading_agent.core.trading_state import TradingState, TradingStateMachine
from trading_agent.core.types import IdFactory
from trading_agent.execution.cost_model import CostModel, ExecutionAssumptions
from trading_agent.execution.registry import create_execution_port
from trading_agent.execution.verifier import OrderVerifier
from trading_agent.features.pipeline import FeaturePipeline
from trading_agent.market_data.ports import MarketDataSource, RawTick
from trading_agent.normalization.normalizer import Normalizer
from trading_agent.observability.metrics import Metrics
from trading_agent.orchestration.evaluation_gate import build_gate
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


class DailyMark(Contract):
    day: date
    cash: Decimal
    positions_value: Decimal
    equity: Decimal
    unmarked_positions: List[str] = Field(default_factory=list)


class FillCost(Contract):
    fill_id: str
    day: date
    symbol: str
    notional: Decimal
    mid: Decimal
    spread_and_slippage: Decimal   # |fill price - mid| x qty (half-spread paid + modelled slippage)
    fees: Decimal


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
    starting_cash: Decimal = Decimal(0)
    dataset_version: Optional[str] = None
    execution_version: Optional[str] = None
    cost_model: Optional[CostModel] = None
    daily: List[DailyMark] = Field(default_factory=list)
    fill_costs: List[FillCost] = Field(default_factory=list)
    events: List[str] = Field(default_factory=list)
    dividends: Decimal = Decimal(0)


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
                 config_dir: Union[str, Path] = DEFAULT_CONFIG_DIR,
                 execution: Optional[ExecutionAssumptions] = None,
                 corporate_actions: Sequence[CorporateAction] = (),
                 listings: Optional[Dict[str, Listing]] = None,
                 dataset_version: Optional[str] = None) -> None:
        assert_mode_permitted(config.mode)
        if config.mode is not Mode.BACKTEST:
            raise ValueError("BacktestEngine runs only in BACKTEST mode")
        if config.risk_policy_version != policy.version:
            raise ConfigError(f"runtime config expects {config.risk_policy_version}, loaded {policy.version}")
        self.approval = require_approval(policy, config.mode, config_dir)
        self._config_dir = Path(config_dir)
        # Historical fixtures carry no halt flags; the default declares every quoted symbol TRADING.
        # Replace it with real halt data when backtesting periods that contain halts.
        self.instrument_status = instrument_status
        self.config, self.policy = config, policy
        self.primary, self.secondary = primary, secondary
        self.news = sorted(news, key=lambda n: n.ts)
        self.venue_status = venue_status
        self.run_id = run_id
        self.starting_cash = starting_cash

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
        self.execution = execution
        self._cost = execution.costs if execution else (cost_model or CostModel())
        self._prompts_dir = prompts_dir
        self._strategy_rules = strategy_rules or DEMO_STRATEGY_RULES
        self._actions = sorted(corporate_actions, key=lambda a: (a.ex_date, a.symbol))
        self._listings = listings or {}
        self._dataset_version = dataset_version
        self._index: Dict[str, Tuple[List[datetime], List[RawTick]]] = {}
        self._events: List[str] = []

    # ------------------------------------------------------------------ wiring
    def _build_pipeline(self, start: datetime) -> TradingPipeline:
        self.clock = SimulatedClock(start)
        signer = EphemeralSigner()  # private half: gateway only; the execution port gets the public key
        verifier = OrderVerifier([signer.trusted_key()], self.config.mode, BACKTEST_ACCOUNT_ALIAS,
                                 {self.policy.source_sha256}, self.clock)
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
        extra = {}
        if self.execution is not None:
            extra = dict(latency_ms=self.execution.latency_ms, fill_quote_after=self._quote_after,
                         partial_fills=self.execution.partial_fills,
                         max_participation=self.execution.max_participation)
        broker = create_execution_port(self.config.mode, verifier=verifier, cost_model=self._cost,
                                       quote_for=lambda s: self._current.quotes.get(s) if self._current else None,
                                       clock=self.clock, ids=self.ids, **extra)
        self.broker = broker
        self.gate = build_gate(self.config, self.policy, [self._config_dir.parent / "ops" / "KILL_SWITCH"],
                               config_dir=self._config_dir)
        return TradingPipeline(self.config, planner, critic, gateway, broker, self.ledger, self.state,
                               Reconciler(), self.portfolio, self.clock, self.metrics, gate=self.gate)

    def _quote_after(self, symbol: str, ts: datetime) -> Optional[Quote]:
        """First quote at or after `ts` on the same day (fills never cross the close)."""
        times, ticks = self._index.get(symbol, ([], []))
        i = bisect.bisect_left(times, ts)
        if i >= len(ticks) or ticks[i].ts.date() != ts.date():
            return None
        t = ticks[i]
        try:
            return Quote(bid=t.bid, ask=t.ask, bid_size=t.bid_size, ask_size=t.ask_size, last=t.last, ts=t.ts)
        except ValueError:
            return None

    def _quote_at(self, symbol: str, ts: datetime) -> Optional[RawTick]:
        times, ticks = self._index.get(symbol, ([], []))
        i = bisect.bisect_left(times, ts)
        return ticks[i] if i < len(ticks) and times[i] == ts else None

    # -------------------------------------------------------- corporate actions
    def _event_market(self, t: datetime) -> MarketSnapshot:
        return self._current or MarketSnapshot.build(as_of=t, received_at=t, venue_status=VenueStatus.CLOSED,
                                                     quotes={}, source_ids=[self.primary.source_id])

    def _start_of_day(self, pipeline: TradingPipeline, day: date, t: datetime, latest) -> None:
        """Apply every action effective on or before `day` that has not been applied yet (point in time)."""
        self.portfolio.start_session()  # daily loss / order-count / intraday drawdown limits reset per day
        while self._actions and self._actions[0].ex_date <= day:
            a = self._actions.pop(0)
            for key in [k for k in latest if k[1] == a.symbol]:
                del latest[key]                     # yesterday's unadjusted quote must not be reused
            if a.type is ActionType.SPLIT:
                self.portfolio.apply_split(a.symbol, a.value)
                self.broker.apply_corporate_action(a.symbol, split_ratio=a.value)
                self.features.adjust_history(a.symbol, 1 / a.value, a.value)
                note = f"{a.ex_date} SPLIT {a.symbol} {a.value}:1 applied to position and price history"
            else:
                prev = self.features.last_mid(a.symbol)
                amount = self.portfolio.apply_dividend(a.symbol, a.value)
                if prev:
                    self.features.adjust_history(a.symbol, 1 - a.value / prev)
                note = f"{a.ex_date} CASH_DIVIDEND {a.symbol} {a.value}/share credited {amount}"
            self._events.append(note)
            pipeline.record_event(AuditEventType.CORPORATE_ACTION, self._event_market(t), {"reason": note})
        for sym, lst in sorted(self._listings.items()):
            if lst.delisted_on is not None and lst.delisted_on <= day and f"delisted:{sym}" not in self._events:
                held = self.portfolio.quantities().get(sym, Decimal(0))
                pnl = self.portfolio.delist(sym, lst.delist_price)
                self.broker.apply_corporate_action(sym, delisted=True)
                for key in [k for k in latest if k[1] == sym]:
                    del latest[key]
                note = (f"{lst.delisted_on} DELISTED {sym} at {lst.delist_price}: closed {held} shares, "
                        f"P&L {pnl.quantize(Decimal('0.01'))}")
                self._events += [note, f"delisted:{sym}"]
                pipeline.record_event(AuditEventType.CORPORATE_ACTION, self._event_market(t), {"reason": note})

    def _mark(self, day: date, latest) -> DailyMark:
        mids = {k[1]: (v.bid + v.ask) / 2 for k, v in latest.items() if k[0] == self.primary.source_id}
        value, unmarked = Decimal(0), []
        for sym, qty in self.portfolio.quantities().items():
            if sym in mids:
                value += qty * mids[sym]
            else:
                unmarked.append(sym)
        return DailyMark(day=day, cash=self.portfolio.cash, positions_value=value,
                         equity=self.portfolio.cash + value, unmarked_positions=unmarked)

    # ------------------------------------------------------------------- run
    def run(self) -> BacktestReport:
        events: List[RawTick] = list(self.primary.ticks()) + (list(self.secondary.ticks()) if self.secondary else [])
        if not events:
            raise ValueError("no market data")
        events.sort(key=lambda t: (t.ts, t.source_id, t.symbol))
        for e in events:
            if e.source_id == self.primary.source_id:
                times, ticks = self._index.setdefault(e.symbol, ([], []))
                times.append(e.ts)
                ticks.append(e)
        timeline = sorted({t.ts for t in events if t.source_id == self.primary.source_id})
        if self.execution is not None and len(timeline) > 1:
            gap_ms = min((b - a).total_seconds() for a, b in zip(timeline, timeline[1:])) * 1000
            if self.execution.latency_ms >= gap_ms:
                raise ConfigError(f"latency {self.execution.latency_ms}ms must be below the data's bar spacing "
                                  f"{gap_ms:.0f}ms, or a fill could be visible before it happened")
        pipeline = self._build_pipeline(timeline[0])

        latest: Dict[Tuple[str, str], RawTick] = {}
        daily: List[DailyMark] = []
        day: Optional[date] = None
        i = 0
        for n, t in enumerate(timeline):
            self.clock.advance_to(t)
            if t.date() != day:
                day = t.date()
                self._start_of_day(pipeline, day, t, latest)
            while i < len(events) and events[i].ts <= t:
                latest[(events[i].source_id, events[i].symbol)] = events[i]
                i += 1
            news = [x for x in self.news if x.ts <= t][-5:]
            symbols = {sym for _, sym in latest}
            result = self.normalizer.build(latest.values(), as_of=t, received_at=t,
                                           venue_status=self.venue_status(t), news=news,
                                           instrument_status={s: self.instrument_status(t, s) for s in symbols})
            self._current = result.snapshot
            features = self.features.update(result.snapshot)
            pipeline.tick(result.snapshot, features, result.issues)
            if n + 1 == len(timeline) or timeline[n + 1].date() != day:
                daily.append(self._mark(day, latest))

        records = self.ledger.records
        fills = [f for r in records for f in r.fills]
        return BacktestReport(
            run_id=self.run_id, ticks=len(timeline), fills=fills, final_cash=self.portfolio.cash,
            final_positions=self.portfolio.quantities(), realized_pnl=self.portfolio.realized_pnl,
            trading_state=self.state.state, ledger_head=self.ledger.head_hash, metrics=self.metrics.snapshot(),
            audit=build_report(records),
            state_history=[f"{h.at.isoformat()} {h.from_state.value}->{h.to_state.value} {h.reason}"
                           for h in self.state.history],
            starting_cash=self.starting_cash, dataset_version=self._dataset_version,
            execution_version=self.execution.version if self.execution else None, cost_model=self._cost,
            daily=daily, fill_costs=[self._fill_cost(f) for f in fills],
            events=[e for e in self._events if not e.startswith("delisted:")], dividends=self.portfolio.dividends,
        )

    def _fill_cost(self, f: Fill) -> FillCost:
        q = self._quote_at(f.symbol, f.at)
        mid = (q.bid + q.ask) / 2 if q is not None else f.price
        return FillCost(fill_id=f.fill_id, day=f.at.date(), symbol=f.symbol, notional=f.quantity * f.price, mid=mid,
                        spread_and_slippage=abs(f.price - mid) * f.quantity, fees=f.fee)
