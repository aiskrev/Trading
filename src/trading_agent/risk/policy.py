"""Immutable, versioned risk configuration.

Every threshold the gateway uses comes from a RiskPolicy loaded here from a released config
file whose SHA-256 is pinned in config/risk_config.lock.yaml. The model is frozen, the gateway
receives it once at construction, and nothing derived from model (Claude) output can reach it.
"""
import hashlib
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Union

import yaml
from pydantic import Field, ValidationError, field_validator, model_validator
from typing_extensions import Literal

from trading_agent.contracts.base import Contract
from trading_agent.core.errors import ConfigError

LOCK_FILE = "risk_config.lock.yaml"
Weekday = Literal["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
_WEEKDAYS = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
Money = Decimal


class InstrumentRisk(Contract):
    sector: str = Field(pattern=r"^[A-Z_]{2,32}$")


class SessionPolicy(Contract):
    timezone: str
    weekdays: List[Weekday] = Field(min_length=1)
    open: time
    close: time
    avoid_open_minutes: int = Field(ge=0, le=120)
    avoid_close_minutes: int = Field(ge=0, le=120)
    holidays: List[date] = Field(default_factory=list)

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        from zoneinfo import ZoneInfo
        try:
            ZoneInfo(v)
        except Exception as exc:
            raise ValueError(f"unknown timezone {v}") from exc
        return v

    @model_validator(mode="after")
    def _window(self):
        if self.open >= self.close:
            raise ValueError("session open must be before close")
        return self

    def is_open(self, now: datetime) -> bool:
        from zoneinfo import ZoneInfo
        local = now.astimezone(ZoneInfo(self.timezone))
        if _WEEKDAYS[local.weekday()] not in self.weekdays or local.date() in self.holidays:
            return False
        minutes = local.hour * 60 + local.minute + local.second / 60
        start = self.open.hour * 60 + self.open.minute + self.avoid_open_minutes
        end = self.close.hour * 60 + self.close.minute - self.avoid_close_minutes
        return start <= minutes < end


class RiskPolicy(Contract):
    version: str = Field(pattern=r"^risk-policy\.v\d+$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    restricted_list_version: str
    restricted: FrozenSet[str]
    instruments: Dict[str, InstrumentRisk] = Field(min_length=1)
    session: SessionPolicy

    max_snapshot_age_seconds: int = Field(gt=0, le=3600)
    max_quote_age_seconds: int = Field(gt=0, le=3600)
    max_future_skew_seconds: int = Field(ge=0, le=60)

    min_order_notional: Money = Field(gt=0)
    max_order_notional: Money = Field(gt=0)
    max_position_notional: Money = Field(gt=0)
    max_gross_exposure: Money = Field(gt=0)
    max_net_exposure: Money = Field(gt=0)
    max_sector_exposure: Dict[str, Money]
    max_concentration_pct: Decimal = Field(gt=0, le=1)
    min_cash_buffer: Money = Field(ge=0)

    price_collar_bps: Decimal = Field(gt=0, le=1000)
    max_spread_bps: Decimal = Field(gt=0, le=1000)
    max_slippage_bps: Decimal = Field(gt=0, le=1000)
    commission_bps: Decimal = Field(ge=0, le=100)

    daily_loss_limit: Money = Field(gt=0)
    max_drawdown_pct: Decimal = Field(gt=0, lt=1)

    max_orders_per_day: int = Field(gt=0)
    max_orders_per_minute: int = Field(gt=0)
    max_orders_per_instrument_per_minute: int = Field(gt=0)

    require_instrument_status: bool
    require_broker_view: bool
    max_broker_view_age_seconds: int = Field(gt=0, le=3600)
    position_tolerance: Decimal = Field(ge=0, le=1)

    allow_short: bool
    critic_required: bool

    @model_validator(mode="after")
    def _coherent(self):
        if self.min_order_notional > self.max_order_notional:
            raise ValueError("min_order_notional > max_order_notional")
        if self.max_order_notional > self.max_position_notional:
            raise ValueError("max_order_notional > max_position_notional")
        if self.max_position_notional > self.max_gross_exposure:
            raise ValueError("max_position_notional > max_gross_exposure")
        if self.max_orders_per_instrument_per_minute > self.max_orders_per_minute:
            raise ValueError("per-instrument rate exceeds overall rate")
        sectors = {i.sector for i in self.instruments.values()}
        missing = sectors - set(self.max_sector_exposure)
        if missing:
            raise ValueError(f"no sector limit for {sorted(missing)}")
        if any(v <= 0 for v in self.max_sector_exposure.values()):
            raise ValueError("sector limits must be positive")
        if self.allow_short:
            raise ValueError("short selling is not supported at this stage")
        return self

    @property
    def universe(self) -> FrozenSet[str]:
        return frozenset(self.instruments)

    def sector_of(self, symbol: str) -> str:
        return self.instruments[symbol].sector

    def summary(self) -> Dict[str, Any]:
        """Read-only copy handed to the critic LLM. Mutating it cannot affect the gateway."""
        return self.model_dump(mode="json")


def file_sha256(path: Union[str, Path]) -> str:
    """Hash with normalized newlines so a CRLF checkout does not look like tampering."""
    data = Path(path).read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


def _pinned(config_dir: Path) -> Dict[str, str]:
    lock = yaml.safe_load((config_dir / LOCK_FILE).read_text(encoding="utf-8")) or {}
    return dict(lock.get("files", {}))


def _verify(path: Path, pinned: Dict[str, str]) -> str:
    digest = file_sha256(path)
    if path.name not in pinned:
        raise ConfigError(f"{path.name} is not a released (pinned) config file")
    if pinned[path.name] != digest:
        raise ConfigError(f"{path.name} was modified after release; publish a new version instead")
    return digest


def load_risk_policy(path: Union[str, Path]) -> RiskPolicy:
    path = Path(path)
    try:
        pinned = _pinned(path.parent)
        digest = _verify(path, pinned)
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        expected_name = raw["version"].replace("-", "_") + ".yaml"
        if path.name != expected_name:
            raise ConfigError(f"file {path.name} does not match declared version {raw['version']}")
        restricted_path = path.parent / raw.pop("restricted_list_file")
        _verify(restricted_path, pinned)
        restricted = yaml.safe_load(restricted_path.read_text(encoding="utf-8"))
        return RiskPolicy.model_validate({
            **raw, "source_sha256": digest, "restricted_list_version": restricted["version"],
            "restricted": restricted["symbols"] or []})
    except ConfigError:
        raise
    except (OSError, KeyError, TypeError, AttributeError, yaml.YAMLError, ValidationError) as exc:
        raise ConfigError(f"invalid risk policy {path}: {exc}") from exc
