"""Deterministic pre-evaluation gate. Runs before the model on EVERY evaluation.

Order (first failure wins; every failure is a NO_TRADE with a reason_code, the model is not called):
  0. versions:        runtime strategy_version / risk_policy_version are the loaded AND approved ones
  1. market data:     snapshot and account fresh; at least one quote          MARKET_DATA_UNKNOWN / _STALE
  2. reconciliation:  broker view present, fresh, equal to our book           RECONCILIATION_UNKNOWN / _MISMATCH
  3. mode & kill:     mode permitted; kill switch readable and off; not HALTED; venue OPEN
  4. universe:        eligible = approved universe x quoted x TRADING x not restricted
  5. trigger:         a deterministic strategy trigger is active on an eligible symbol   NO_TRIGGER
"""
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import List, NamedTuple, Optional, Sequence, Union

from trading_agent.backtest.dataset import file_sha256
from trading_agent.contracts.account import AccountSnapshot, BrokerAccountView
from trading_agent.contracts.features import FeatureSet
from trading_agent.contracts.market import InstrumentStatus, MarketSnapshot, VenueStatus
from trading_agent.core.mode import PERMITTED_MODES_THIS_STAGE, Mode, RuntimeConfig
from trading_agent.core.trading_state import TradingState
from trading_agent.risk.approvals import load_approvals
from trading_agent.risk.policy import RiskPolicy
from trading_agent.strategy.triggers import StrategyDefinition, TriggerHit, active_triggers, strategy_file

ROOT = Path(__file__).resolve().parents[3]
TRIGGER_CODE = "src/trading_agent/strategy/triggers.py"


class KillSwitchState(str, Enum):
    OFF = "OFF"
    ACTIVE = "ACTIVE"
    UNKNOWN = "UNKNOWN"


class KillSwitch:
    """External kill switch: the presence of any configured file stops new evaluations.

    An operator creates the file (e.g. `ops/KILL_SWITCH`); nothing in the code creates or removes it.
    If its state cannot be determined, it counts as UNKNOWN (=> NO_TRADE).
    """

    def __init__(self, paths: Sequence[Union[str, Path]]) -> None:
        self.paths = [Path(p) for p in paths]

    def state(self) -> KillSwitchState:
        try:
            return KillSwitchState.ACTIVE if any(p.exists() for p in self.paths) else KillSwitchState.OFF
        except OSError:
            return KillSwitchState.UNKNOWN


class GateResult(NamedTuple):
    ok: bool
    reason_code: Optional[str]
    detail: str
    triggers: List[TriggerHit]


def strategy_approval(version: str, mode: Mode, config_dir: Path = ROOT / "config",
                      code_root: Path = ROOT) -> Optional[str]:
    """Approval id covering this strategy file AND the trigger code in `mode`, else None."""
    try:
        wanted = {f"config/strategies/{version}.yaml": file_sha256(strategy_file(version, Path(config_dir) / "strategies")),
                  TRIGGER_CODE: file_sha256(Path(code_root) / TRIGGER_CODE)}
        approvals = load_approvals(config_dir)
    except Exception:
        return None  # unreadable => not approved (fail closed)
    for a in approvals:
        if mode in a.modes and mode not in a.not_approved_for and all(a.files.get(k) == v for k, v in wanted.items()):
            return a.id
    return None


def build_gate(config: RuntimeConfig, policy: RiskPolicy, kill_paths: Sequence[Union[str, Path]],
               config_dir: Path = ROOT / "config", code_root: Path = ROOT) -> "EvaluationGate":
    """Gate for the runtime config. A missing/invalid strategy or approval yields a gate that always says NO_TRADE."""
    from trading_agent.core.errors import ConfigError
    from trading_agent.risk.approvals import require_approval
    from trading_agent.strategy.triggers import load_strategy
    try:
        strategy = load_strategy(config.strategy_version, Path(config_dir) / "strategies")
    except ConfigError:
        strategy = None
    try:
        risk_id = require_approval(policy, config.mode, config_dir).id
    except ConfigError:
        risk_id = None
    strat_id = strategy_approval(config.strategy_version, config.mode, config_dir, code_root) if strategy else None
    return EvaluationGate(config, policy, strategy, KillSwitch(kill_paths), strat_id, risk_id)


class EvaluationGate:
    def __init__(self, config: RuntimeConfig, policy: RiskPolicy, strategy: Optional[StrategyDefinition],
                 kill_switch: KillSwitch, strategy_approval_id: Optional[str], risk_approval_id: Optional[str]) -> None:
        self.config, self.policy, self.strategy = config, policy, strategy
        self.kill_switch = kill_switch
        self.strategy_approval_id, self.risk_approval_id = strategy_approval_id, risk_approval_id

    def check(self, market: MarketSnapshot, account: Optional[AccountSnapshot], broker: Optional[BrokerAccountView],
              features: Optional[FeatureSet], state: TradingState, now: datetime) -> GateResult:
        p = self.policy
        # 0. only the approved versions
        if self.strategy is None or self.config.strategy_version != self.strategy.strategy_version                 or not self.strategy_approval_id:
            return _no("STRATEGY_NOT_APPROVED", f"{self.config.strategy_version} has no approval for "
                                                f"{self.config.mode.value}")
        if self.config.risk_policy_version != p.version or not self.risk_approval_id:
            return _no("RISK_POLICY_NOT_APPROVED", f"{self.config.risk_policy_version} not approved/loaded")
        # 1. market-data freshness
        max_age, skew = timedelta(seconds=p.max_snapshot_age_seconds), timedelta(seconds=p.max_future_skew_seconds)
        if not market.quotes:
            return _no("MARKET_DATA_UNKNOWN", "no usable quotes")
        if market.as_of - now > skew or now - market.as_of > max_age:
            return _no("MARKET_DATA_STALE", f"snapshot age {now - market.as_of}")
        if account is None:
            return _no("ACCOUNT_UNKNOWN", "no account snapshot")
        if now - account.as_of > max_age or account.as_of - now > skew:
            return _no("ACCOUNT_STALE", f"account age {now - account.as_of}")
        # 2. broker/account reconciliation
        if broker is None:
            return _no("RECONCILIATION_UNKNOWN", "no broker view")
        if now - broker.as_of > timedelta(seconds=p.max_broker_view_age_seconds) or broker.as_of - now > skew:
            return _no("RECONCILIATION_UNKNOWN", f"broker view age {now - broker.as_of}")
        for sym in sorted(set(broker.positions) | set(account.positions)):
            if abs(account.quantity(sym) - broker.positions.get(sym, Decimal(0))) > p.position_tolerance:
                return _no("RECONCILIATION_MISMATCH", f"{sym}: book {account.quantity(sym)} broker "
                                                      f"{broker.positions.get(sym, Decimal(0))}")
        # 3. mode and kill switch
        if self.config.mode not in PERMITTED_MODES_THIS_STAGE:
            return _no("MODE_NOT_PERMITTED", self.config.mode.value)
        ks = self.kill_switch.state()
        if ks is KillSwitchState.UNKNOWN:
            return _no("KILL_SWITCH_UNKNOWN", "kill switch state unreadable")
        if ks is KillSwitchState.ACTIVE:
            return _no("KILL_SWITCH_ACTIVE", "operator kill switch is set")
        if state is TradingState.HALTED:
            return _no("TRADING_HALTED", "trading state HALTED")
        if market.venue_status is VenueStatus.UNKNOWN:
            return _no("MARKET_STATUS_UNKNOWN", "venue status unknown")
        if market.venue_status is not VenueStatus.OPEN:
            return _no("MARKET_CLOSED", market.venue_status.value)
        # 4. allowed instrument universe
        eligible, unknown = [], []
        for sym in sorted(p.universe - p.restricted):
            if sym not in market.quotes:
                continue
            status = market.status_of(sym)
            if status is InstrumentStatus.TRADING:
                eligible.append(sym)
            elif status is InstrumentStatus.UNKNOWN:
                unknown.append(sym)
        if not eligible:
            return _no("INSTRUMENT_STATUS_UNKNOWN" if unknown else "NO_ELIGIBLE_INSTRUMENT",
                       f"unknown status: {unknown}" if unknown else "no quoted, tradable universe symbol")
        # 5. deterministic trigger
        if features is None or features.snapshot_id != market.snapshot_id:
            return _no("FEATURES_UNKNOWN", "features missing or for another snapshot")
        marks = {s: market.quotes[s].mid for s in eligible}
        hits = active_triggers(self.strategy, features, account, marks, eligible)
        if not hits:
            return _no("NO_TRIGGER", f"no {self.strategy.trigger.feature} trigger on {eligible}")
        return GateResult(True, None, "", hits)


def _no(code: str, detail: str) -> GateResult:
    return GateResult(False, code, detail, [])
