"""Deterministic strategy triggers: the model is consulted only when one of these fires.

A trigger is computed from the strategy definition, the feature set and the account alone.
Missing inputs never produce a trigger (fail closed).
"""
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Iterable, List, NamedTuple, Union

import yaml
from pydantic import Field, ValidationError

from trading_agent.contracts.account import AccountSnapshot
from trading_agent.contracts.base import Contract
from trading_agent.contracts.features import FeatureSet
from trading_agent.core.errors import ConfigError

STRATEGIES_DIR = Path(__file__).resolve().parents[3] / "config" / "strategies"


class TriggerSpec(Contract):
    feature: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    entry_at_or_below_bps: Decimal
    exit_at_or_above_bps: Decimal
    min_position_notional: Decimal = Field(ge=0)


class StrategyDefinition(Contract):
    strategy_version: str = Field(pattern=r"^[a-z0-9][a-z0-9.\-]{2,63}$")
    description: str
    trigger: TriggerSpec
    max_notional_per_order: Decimal = Field(gt=0)


def strategy_file(version: str, root: Union[str, Path] = STRATEGIES_DIR) -> Path:
    return Path(root) / f"{version}.yaml"


def load_strategy(version: str, root: Union[str, Path] = STRATEGIES_DIR) -> StrategyDefinition:
    path = strategy_file(version, root)
    try:
        s = StrategyDefinition.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise ConfigError(f"unknown or invalid strategy {version!r}: {exc}") from exc
    if s.strategy_version != version:
        raise ConfigError(f"{path.name} declares {s.strategy_version}")
    return s


class TriggerKind(str, Enum):
    ENTRY = "ENTRY"   # -> BUY
    EXIT = "EXIT"     # -> SELL


class TriggerHit(NamedTuple):
    symbol: str
    kind: TriggerKind
    feature_value: Decimal

    @property
    def side(self) -> str:
        return "BUY" if self.kind is TriggerKind.ENTRY else "SELL"


def active_triggers(strategy: StrategyDefinition, features: FeatureSet, account: AccountSnapshot,
                    marks: dict, symbols: Iterable[str]) -> List[TriggerHit]:
    """`marks`: symbol -> current mid. A symbol without a feature value or a mark never triggers."""
    t = strategy.trigger
    hits = []
    for sym in sorted(symbols):
        value = features.values.get(sym, {}).get(t.feature)
        mid = marks.get(sym)
        if value is None or mid is None:
            continue
        held = account.quantity(sym)
        flat = held * mid < t.min_position_notional
        if flat and value <= t.entry_at_or_below_bps:
            hits.append(TriggerHit(sym, TriggerKind.ENTRY, value))
        elif not flat and held > 0 and value >= t.exit_at_or_above_bps:
            hits.append(TriggerHit(sym, TriggerKind.EXIT, value))
    return hits
