"""Live run loop: streaming market data -> pipeline -> simulated execution, one DecisionPacket per cycle.

Built for PAPER (live data, simulated fills, no execution credentials). It runs only in a permitted
mode, so today it runs in BACKTEST against mock feeds; it becomes PAPER only after the owner changes
the mode and approves it (CLAUDE.md: Claude may never change the mode).

Run directory (survives restarts):
  manifest.json      run id, starting cash, versions, segments (one per process start), recoveries
  ledger.jsonl       hash-chained audit ledger (write-ahead DECISION records)
  packets.jsonl      hash-chained DecisionPackets, one per cycle (incl. NO_TRADE and skipped)
  broker_book.json   the simulated broker's own position book (independent of the ledger)
  halt_ack.json      optional human acknowledgement that clears a HALT for the NEXT session

Fail closed for new risk on operational failures (ops-policy):
  feed outage > max  => REDUCE_ONLY      consecutive model errors >= max => REDUCE_ONLY
  restart in session => >= REDUCE_ONLY   book/broker mismatch at restart  => HALTED
  packet/ledger/broker-book write fails, clock goes backwards, unexpected exception => HALTED
"""
import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

import yaml
from pydantic import Field, ValidationError

from trading_agent.audit.chain import ChainedLog
from trading_agent.audit.ledger import AuditLedger
from trading_agent.backtest.portfolio import Portfolio
from trading_agent.contracts.audit import Attribution, AuditEventType
from trading_agent.contracts.base import Contract
from trading_agent.contracts.decision_packet import DecisionPacket
from trading_agent.contracts.market import InstrumentStatus, MarketSnapshot, VenueStatus
from trading_agent.contracts.proposal import Decision
from trading_agent.core.clock import Clock
from trading_agent.core.errors import AuditWriteFailure, ConfigError
from trading_agent.core.mode import RuntimeConfig, assert_mode_permitted
from trading_agent.core.signing import OrderSigner
from trading_agent.core.trading_state import TradingState, TradingStateMachine
from trading_agent.core.types import IdFactory
from trading_agent.execution.cost_model import CostModel
from trading_agent.execution.registry import create_execution_port
from trading_agent.features.pipeline import FeaturePipeline
from trading_agent.market_data.streaming import StreamingQuoteAdapter
from trading_agent.normalization.normalizer import Normalizer
from trading_agent.observability.metrics import Metrics
from trading_agent.orchestration.evaluation_gate import build_gate
from trading_agent.orchestration.tick import TickResult, TradingPipeline
from trading_agent.planning.critic import ClaudeCritic
from trading_agent.planning.planner import ClaudePlanner
from trading_agent.planning.prompts import load_prompt
from trading_agent.reconciliation.reconciler import Reconciler
from trading_agent.risk.gateway import RiskGateway
from trading_agent.risk.policy import RiskPolicy

ROOT = Path(__file__).resolve().parents[3]
_SEVERITY = {TradingState.NORMAL: 0, TradingState.REDUCE_ONLY: 1, TradingState.HALTED: 2}
MODEL_ERRORS = {"LLM_UNAVAILABLE", "MODEL_FAILURE"}
CRITIC_ERRORS = {"CRITIC_UNAVAILABLE", "CRITIC_FAILURE"}


class OpsPolicy(Contract):
    version: str
    max_feed_outage_seconds: int = Field(gt=0, le=3600)
    max_consecutive_model_errors: int = Field(gt=0, le=100)
    restart_state_floor: TradingState


def load_ops_policy(path) -> OpsPolicy:
    try:
        return OpsPolicy.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise ConfigError(f"invalid ops policy {path}: {exc}") from exc


class LiveLoopRunner:
    def __init__(self, run_dir, config: RuntimeConfig, policy: RiskPolicy, ops: OpsPolicy, planner_llm,
                 critic_llm, adapters: Sequence[StreamingQuoteAdapter], clock: Clock, session_id: str,
                 instrument_status: Callable[[str], InstrumentStatus] = lambda s: InstrumentStatus.UNKNOWN,
                 venue_status: Callable[[datetime], VenueStatus] = lambda t: VenueStatus.UNKNOWN,
                 cost_model: Optional[CostModel] = None, starting_cash: Decimal = Decimal(100000),
                 run_id: str = "paper", prompts_dir: Path = ROOT / "prompts",
                 config_dir: Path = ROOT / "config") -> None:
        assert_mode_permitted(config.mode)  # PAPER is refused until the owner permits and approves it
        self.dir = Path(run_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.config, self.policy, self.ops = config, policy, ops
        self.adapters = list(adapters)
        self.clock = clock
        self.session_id = session_id
        self.instrument_status, self.venue_status = instrument_status, venue_status
        self.metrics = Metrics()
        self._last_now: Optional[datetime] = None
        self._outage_since: Optional[datetime] = None
        self._model_errors = 0
        self.stopped: Optional[str] = None

        self.manifest = self._load_manifest(run_id, starting_cash)
        self.segment = self.manifest["segments"][-1]["segment"]
        self.ids = IdFactory(self.segment)

        self.ledger, ledger_torn = AuditLedger.resume(self.dir / "ledger.jsonl")
        self.packets, packets_open = ChainedLog.open(self.dir / "packets.jsonl", DecisionPacket)
        self.portfolio = Portfolio(Decimal(self.manifest["starting_cash"]))
        self.state = TradingStateMachine()
        self.features = FeaturePipeline()
        primary = self.adapters[0].source_id
        secondary = self.adapters[1].source_id if len(self.adapters) > 1 else None
        self.normalizer = Normalizer(primary, secondary)
        self._current: Optional[MarketSnapshot] = None

        signer = OrderSigner()
        self.gateway = RiskGateway(policy, config.mode, signer, self.ids, clock)
        self.broker = create_execution_port(config.mode, signer=signer, cost_model=cost_model or CostModel(),
                                            quote_for=lambda s: self._current.quotes.get(s) if self._current else None,
                                            clock=clock, ids=self.ids, partial_fills=True)
        planner = ClaudePlanner(planner_llm, load_prompt(prompts_dir, "trading_planner",
                                                         config.prompt_versions["trading_planner"]),
                                config.strategy_version, {"name": config.strategy_version},
                                sorted(policy.universe), self.ids)
        critic = ClaudeCritic(critic_llm, load_prompt(prompts_dir, "risk_critic",
                                                      config.prompt_versions["risk_critic"])) if critic_llm else None
        self.gate = build_gate(config, policy, [Path(config_dir).parent / "ops" / "KILL_SWITCH",
                                                self.dir / "KILL_SWITCH"], config_dir=config_dir)
        self.pipeline = TradingPipeline(config, planner, critic, self.gateway, self.broker, self.ledger, self.state,
                                        Reconciler(), self.portfolio, clock, self.metrics, gate=self.gate)
        self.recovery = self._recover(ledger_torn, packets_open.torn_line_moved_to)

    # ------------------------------------------------------------------ manifest / recovery
    def _load_manifest(self, run_id: str, starting_cash: Decimal) -> Dict[str, Any]:
        path = self.dir / "manifest.json"
        if path.exists():
            m = json.loads(path.read_text(encoding="utf-8"))
        else:
            m = {"run_id": run_id, "created_at": self.clock.now().isoformat(), "starting_cash": str(starting_cash),
                 "mode": self.config.mode.value, "risk_policy": [self.policy.version, self.policy.source_sha256],
                 "ops_policy": self.ops.version, "segments": [], "recoveries": []}
        m["segments"].append({"segment": f"{m['run_id']}.s{len(m['segments'])}", "session_id": self.session_id,
                              "started_at": self.clock.now().isoformat()})
        self._write_json(path, m)
        return m

    def _recover(self, ledger_torn: Optional[Path], packets_torn: Optional[Path]) -> Dict[str, Any]:
        records = self.ledger.records
        info: Dict[str, Any] = {"segment": self.segment, "at": self.clock.now().isoformat(),
                                "ledger_records": len(records), "packets": len(self.packets.items),
                                "torn_ledger_line": bool(ledger_torn), "torn_packet_line": bool(packets_torn)}
        if not records:
            self.portfolio.start_session()
            info["kind"] = "fresh_start"
            return info
        # 1. rebuild the internal book from the ledger (the only source of truth for our own fills)
        for r in records:
            if r.event_type is AuditEventType.EXECUTION:
                self.portfolio.record_order()
                for f in r.fills:
                    self.portfolio.apply_fill(f)
        last_packet = self.packets.items[-1] if self.packets.items else None
        same_session = last_packet is not None and last_packet.session_id == self.session_id
        if last_packet and last_packet.account and last_packet.account.equity_high_water:
            self.portfolio.equity_high_water = last_packet.account.equity_high_water
            self.portfolio.last_equity = last_packet.account.equity_high_water
        if not same_session:
            self.portfolio.start_session()
        # 2. restore the broker's own book from its independent file
        book = self._read_broker_book()
        _restore_simulated_book(self.broker, book)
        # 3. restore trading state; never resume better than the floor inside a session
        restored = records[-1].trading_state
        info["restored_state"] = restored.value
        target = restored
        if same_session and _SEVERITY[self.ops.restart_state_floor] > _SEVERITY[target]:
            target = self.ops.restart_state_floor
        if not same_session and restored is TradingState.REDUCE_ONLY:
            target = TradingState.NORMAL  # REDUCE_ONLY is per session; HALT is not
        if not same_session and restored is TradingState.HALTED and self._halt_acknowledged(records):
            target = TradingState.NORMAL
            info["halt_acknowledged"] = True
        if target is not TradingState.NORMAL:
            self.state.trip(target, "RESTART_RECOVERY" if target is not restored else f"RESTORED:{restored.value}",
                            self.clock.now())
        market = self._empty_market()
        self.pipeline.record_event(AuditEventType.STATE_CHANGE, market, {
            "reason": "RESTART_RECOVERY", "segment": self.segment, "restored": restored.value,
            "state": self.state.state.value, "torn_ledger_line": str(bool(ledger_torn))})
        # 4. reconcile rebuilt book against the broker's book before doing anything else
        report = Reconciler().reconcile(self.portfolio.quantities(), self.broker.broker_positions())
        info["reconciled"] = report.matched
        if not report.matched:
            self.state.trip(TradingState.HALTED, "RESTART_RECONCILIATION_MISMATCH", self.clock.now())
            self.pipeline.record_event(AuditEventType.RECONCILIATION, market, {
                "reason": "RESTART_RECONCILIATION_MISMATCH",
                "mismatches": ";".join(f"{m.symbol}:{m.internal}!={m.broker}" for m in report.mismatches)})
        info["kind"] = "restart_same_session" if same_session else "new_session"
        info["state_after"] = self.state.state.value
        self.manifest["recoveries"].append(info)
        self._write_json(self.dir / "manifest.json", self.manifest)
        return info

    def _halt_acknowledged(self, records) -> bool:
        ack_path = self.dir / "halt_ack.json"
        halts = [r for r in records if r.event_type is AuditEventType.STATE_CHANGE and r.details.get("to") == "HALTED"]
        if not ack_path.exists() or not halts:
            return False
        ack = json.loads(ack_path.read_text(encoding="utf-8"))
        return ack.get("halt_record_hash") == halts[-1].record_hash and bool(ack.get("acknowledged_by"))

    # ------------------------------------------------------------------ one cycle
    def step(self) -> DecisionPacket:
        """Poll feeds, run one decision cycle, persist its packet. Never raises; failures fail closed."""
        errors: List[str] = []
        result = TickResult(skipped="NOT_RUN")
        market = self._empty_market()
        features = None
        try:
            now = self.clock.now()
            if self._last_now is not None and now < self._last_now:
                self._halt("CLOCK_WENT_BACKWARDS", market)
                errors.append("CLOCK_WENT_BACKWARDS")
            self._last_now = now if self._last_now is None else max(now, self._last_now)
            issues, ticks = [], []
            for a in self.adapters:
                batch = a.poll()
                issues += batch.issues
                ticks += a.fresh_ticks()
            symbols = {t.symbol for t in ticks}
            res = self.normalizer.build(ticks, as_of=now, received_at=now, venue_status=self.venue_status(now),
                                        instrument_status={s: self.instrument_status(s) for s in symbols})
            market = self._current = res.snapshot
            issues += res.issues
            self._watch_feed(market, now)
            features = self.features.update(market)
            result = self.pipeline.tick(market, features, issues)
            self._watch_model(result)
            self._save_broker_book()
        except Exception as exc:  # unknown operational state => no new risk, stop the loop
            errors.append(f"OPERATIONAL_ERROR:{type(exc).__name__}:{str(exc)[:200]}")
            self._halt(f"OPERATIONAL_ERROR:{type(exc).__name__}", market)
            self.stopped = errors[-1]
        packet = self._packet(market, features, result, errors)
        try:
            self.packets.append(packet)
        except Exception as exc:
            self._halt(f"PACKET_WRITE_FAILURE:{type(exc).__name__}", market)
            self.stopped = "PACKET_WRITE_FAILURE"
        return packet

    def run(self, steps: int, advance: Callable[[], None]) -> List[DecisionPacket]:
        out = []
        for _ in range(steps):
            if self.stopped:
                break
            out.append(self.step())
            advance()
        return out

    # ------------------------------------------------------------------ fail-closed watchers
    def _watch_feed(self, market: MarketSnapshot, now: datetime) -> None:
        if market.quotes:
            self._outage_since = None
            return
        self._outage_since = self._outage_since or now
        if (now - self._outage_since).total_seconds() > self.ops.max_feed_outage_seconds:
            self._trip(TradingState.REDUCE_ONLY, "FEED_OUTAGE", market)

    def _watch_model(self, result: TickResult) -> None:
        tr = result.trace
        if tr is None:
            return
        critic_err = bool(result.critic and set(result.critic.reason_codes) & CRITIC_ERRORS)
        if (tr.planner_failure in MODEL_ERRORS) or critic_err:
            self._model_errors += 1
            self.metrics.inc("model_errors")
            if self._model_errors >= self.ops.max_consecutive_model_errors:
                self._trip(TradingState.REDUCE_ONLY, "CONSECUTIVE_MODEL_ERRORS", self._current)
        elif tr.planner_failure is None:
            self._model_errors = 0

    def _trip(self, to: TradingState, reason: str, market) -> None:
        if self.state.trip(to, reason, self.clock.now()):
            self.pipeline.record_event(AuditEventType.STATE_CHANGE, market or self._empty_market(),
                                       {"reason": reason, "to": to.value})

    def _halt(self, reason: str, market) -> None:
        try:
            self._trip(TradingState.HALTED, reason, market)
        except Exception:
            self.state.trip(TradingState.HALTED, reason, self.clock.now())

    # ------------------------------------------------------------------ persistence
    def _save_broker_book(self) -> None:
        self._write_json(self.dir / "broker_book.json",
                         {"positions": {s: str(q) for s, q in self.broker.broker_positions().items()},
                          "segment": self.segment, "at": self.clock.now().isoformat()})

    def _read_broker_book(self) -> Dict[str, Decimal]:
        path = self.dir / "broker_book.json"
        if not path.exists():
            return {}
        return {s: Decimal(q) for s, q in json.loads(path.read_text(encoding="utf-8"))["positions"].items()}

    @staticmethod
    def _write_json(path: Path, obj) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes((json.dumps(obj, indent=1, sort_keys=True) + "\n").encode("utf-8"))
        os.replace(tmp, path)  # atomic: a crash leaves the old or the new file, never half of one

    def _empty_market(self) -> MarketSnapshot:
        now = self.clock.now()
        return self._current or MarketSnapshot.build(as_of=now, received_at=now, venue_status=VenueStatus.UNKNOWN,
                                                     quotes={}, source_ids=[a.source_id for a in self.adapters])

    def _packet(self, market, features, result: TickResult, errors: List[str]) -> DecisionPacket:
        tr, d = result.trace, result.decision
        if result.skipped:
            outcome = f"SKIPPED_{result.skipped}"
        elif d is None:
            outcome = "NO_DECISION"
        elif result.proposal is not None and result.proposal.decision is Decision.NO_TRADE:
            outcome = "NO_TRADE"
        else:
            outcome = {"DENY": "DENIED", "APPROVE": "APPROVED", "REDUCE": "REDUCED"}[d.verdict.value]
        execu = result.execution
        broker_result = "NOT_SUBMITTED" if execu is None else execu.ack.status.value
        p = result.proposal
        return DecisionPacket(
            packet_id=self.ids.next("packet"), session_id=self.session_id, run_segment=self.segment,
            at=self.clock.now(), outcome=outcome,
            attribution=Attribution(model_version=p.model_version if p else self.config.model_version,
                                    prompt_version=p.prompt_version if p else "n/a",
                                    strategy_version=self.config.strategy_version,
                                    market_snapshot_id=market.snapshot_id, risk_policy_version=self.policy.version,
                                    code_commit=self.config.code_commit, deployment_id=self.config.deployment_id,
                                    broker_result=broker_result),
            state_before=tr.state_before if tr else self.state.state, state_after=self.state.state,
            market=market, features=features, account=tr.account if tr else None,
            data_issues=tr.data_issues if tr else [], planner_raw_output=tr.planner_raw if tr else None,
            planner_failure=tr.planner_failure if tr else None, proposal=p,
            critic_raw_output=tr.critic_raw if tr else None, critic=result.critic, risk_decision=d,
            approved_order=result.order.signing_payload() if result.order else None,
            acks=[execu.ack] if execu else [], fills=list(execu.fills) if execu else [],
            timings_ms=dict(tr.timings_ms) if tr else {},
            signal_to_proposal_ms=tr.signal_to_proposal_ms if tr else None, errors=errors)


def _restore_simulated_book(broker, positions: Dict[str, Decimal]) -> None:
    """Restore the BACKTEST/PAPER simulator's position book after a restart.

    The simulator has no public restore method and /execution is frozen under APR-0002, so this
    sets its book directly. Follow-up (needs an /execution change + approval): add
    SimulatedBroker.restore_positions() and use it here.
    """
    book = getattr(broker, "_positions", None)
    if book is None:
        raise ConfigError("execution port does not support book restore")
    book.clear()
    book.update(positions)
