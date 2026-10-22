"""Startup check: a risk policy is only usable in a mode it was explicitly approved for.

Approvals live in config/approvals.yaml (append-only, CODEOWNERS-protected) and are bound to the
pinned SHA-256 of the policy and restricted-list files.
"""
from datetime import date
from pathlib import Path
from typing import Dict, List, Union

import yaml
from pydantic import Field, ValidationError

from trading_agent.contracts.base import Contract
from trading_agent.core.errors import ConfigError
from trading_agent.core.mode import Mode
from trading_agent.risk.policy import RiskPolicy, file_sha256

APPROVALS_FILE = "approvals.yaml"


class Approval(Contract):
    id: str = Field(pattern=r"^APR-\d{4}$")
    date: date
    approver: str = Field(min_length=1)
    approved_via: str
    statement: str = Field(min_length=1)
    modes: List[Mode] = Field(min_length=1)
    files: Dict[str, str]
    code_scope: str
    not_approved_for: List[Mode] = Field(default_factory=list)


def load_approvals(config_dir: Union[str, Path]) -> List[Approval]:
    path = Path(config_dir) / APPROVALS_FILE
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        approvals = [Approval.model_validate(a) for a in raw.get("approvals", [])]
    except (OSError, yaml.YAMLError, ValidationError, AttributeError) as exc:
        raise ConfigError(f"invalid approvals file {path}: {exc}") from exc
    ids = [a.id for a in approvals]
    if len(ids) != len(set(ids)):
        raise ConfigError("duplicate approval id")
    return approvals


def require_approval(policy: RiskPolicy, mode: Mode, config_dir: Union[str, Path]) -> Approval:
    """Return the approval covering this exact policy (and restricted list) in `mode`, else raise."""
    config_dir = Path(config_dir)
    policy_file = policy.version.replace("-", "_") + ".yaml"
    restricted_file = policy.restricted_list_version.replace("-", "_") + ".yaml"
    restricted_hash = file_sha256(config_dir / restricted_file)
    for a in load_approvals(config_dir):
        if (mode in a.modes and mode not in a.not_approved_for
                and a.files.get(policy_file) == policy.source_sha256
                and a.files.get(restricted_file) == restricted_hash):
            return a
    raise ConfigError(f"{policy.version} ({policy.source_sha256[:12]}) has no approval for {mode.value}")
