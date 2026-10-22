"""Production mode is read once from deployment config and frozen.

There is deliberately no setter, no CLI flag, and no environment override.
Claude may NEVER change the active mode (CLAUDE.md).
"""
from enum import Enum
from pathlib import Path
from typing import Dict, FrozenSet, Union

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from trading_agent.core.errors import ConfigError, ModeNotPermitted


class Mode(str, Enum):
    BACKTEST = "BACKTEST"
    REPLAY = "REPLAY"
    PAPER = "PAPER"
    SHADOW = "SHADOW"
    CANARY_LIVE = "CANARY_LIVE"
    LIMITED_LIVE = "LIMITED_LIVE"
    FULL_CONFIGURED_LIVE = "FULL_CONFIGURED_LIVE"


# Stage 1 of the project: only BACKTEST may start. Widening this set touches live
# deployment and requires tests plus an explicit production approval.
PERMITTED_MODES_THIS_STAGE: FrozenSet[Mode] = frozenset({Mode.BACKTEST})


class RuntimeConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: Mode
    deployment_id: str
    code_commit: str
    strategy_version: str
    risk_policy_version: str
    model_version: str
    prompt_versions: Dict[str, str]


def load_runtime_config(path: Union[str, Path]) -> RuntimeConfig:
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        config = RuntimeConfig.model_validate(raw)
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise ConfigError(f"invalid runtime config {path}: {exc}") from exc
    assert_mode_permitted(config.mode)
    return config


def assert_mode_permitted(mode: Mode) -> None:
    if mode not in PERMITTED_MODES_THIS_STAGE:
        raise ModeNotPermitted(f"mode {mode.value} is not permitted at this stage")
