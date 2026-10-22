"""Versioned, hash-verified backtest datasets.

data/datasets/<dataset_version>/
  manifest.yaml            dataset_version, description, synthetic flag, sha256 per file
  quotes.csv               ts,symbol,bid,ask,bid_size,ask_size,last   RAW (unadjusted) prices as traded
  corporate_actions.csv    ex_date,symbol,type,value                   SPLIT (new shares per old) | CASH_DIVIDEND (per share)
  listings.csv             symbol,listed_from,delisted_on,delist_price  delisted_on/delist_price empty if still listed

Prices are never back-adjusted in the file. Adjustments are applied point-in-time by the engine on
the ex-date, so nothing before an action can see it. Delisted securities stay in the data and in
the report.
"""
import csv
import hashlib
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Union

import yaml
from pydantic import Field, ValidationError, model_validator

from trading_agent.contracts.base import Contract
from trading_agent.core.errors import ConfigError
from trading_agent.market_data.ports import RawTick

DATASETS_DIR = Path(__file__).resolve().parents[3] / "data" / "datasets"
FILES = ("quotes.csv", "corporate_actions.csv", "listings.csv")


class ActionType(str, Enum):
    SPLIT = "SPLIT"
    CASH_DIVIDEND = "CASH_DIVIDEND"


class CorporateAction(Contract):
    ex_date: date
    symbol: str
    type: ActionType
    value: Decimal = Field(gt=0)  # split: new shares per old share; dividend: cash per share

    @model_validator(mode="after")
    def _sane(self):
        if self.type is ActionType.SPLIT and self.value == 1:
            raise ValueError("split ratio of 1 is not an action")
        return self


class Listing(Contract):
    symbol: str
    listed_from: date
    delisted_on: Optional[date] = None
    delist_price: Optional[Decimal] = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _sane(self):
        if (self.delisted_on is None) != (self.delist_price is None):
            raise ValueError("delisted_on and delist_price go together")
        if self.delisted_on is not None and self.delisted_on <= self.listed_from:
            raise ValueError("delisting before listing")
        return self


class DatasetManifest(Contract):
    dataset_version: str = Field(pattern=r"^[a-z0-9][a-z0-9.\-]{2,63}$")
    description: str
    synthetic: bool
    source: str
    files: Dict[str, str]


class Dataset(NamedTuple):
    manifest: DatasetManifest
    ticks: List[RawTick]
    actions: List[CorporateAction]
    listings: Dict[str, Listing]
    path: Path


class DatasetSource:
    """MarketDataSource over a loaded dataset's ticks."""

    def __init__(self, ticks: List[RawTick], source_id: str = "primary") -> None:
        self.source_id = source_id
        self._ticks = ticks

    def ticks(self):
        return iter(self._ticks)


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def load_dataset(dataset_version: str, root: Union[str, Path] = DATASETS_DIR, source_id: str = "primary") -> Dataset:
    path = Path(root) / dataset_version
    try:
        manifest = DatasetManifest.model_validate(yaml.safe_load((path / "manifest.yaml").read_text(encoding="utf-8")))
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise ConfigError(f"unknown or invalid dataset {dataset_version!r}: {exc}") from exc
    if manifest.dataset_version != dataset_version:
        raise ConfigError("manifest dataset_version does not match directory")
    for name in FILES:
        if manifest.files.get(name) != file_sha256(path / name):
            raise ConfigError(f"dataset {dataset_version}: {name} does not match its manifest hash")
    ticks = [RawTick(source_id=source_id, symbol=r["symbol"], ts=datetime.fromisoformat(r["ts"]),
                     bid=Decimal(r["bid"]), ask=Decimal(r["ask"]), bid_size=Decimal(r["bid_size"]),
                     ask_size=Decimal(r["ask_size"]), last=Decimal(r["last"])) for r in _rows(path / "quotes.csv")]
    actions = [CorporateAction(ex_date=date.fromisoformat(r["ex_date"]), symbol=r["symbol"], type=r["type"],
                               value=Decimal(r["value"])) for r in _rows(path / "corporate_actions.csv")]
    listings = {}
    for r in _rows(path / "listings.csv"):
        listings[r["symbol"]] = Listing(symbol=r["symbol"], listed_from=date.fromisoformat(r["listed_from"]),
                                        delisted_on=date.fromisoformat(r["delisted_on"]) if r["delisted_on"] else None,
                                        delist_price=Decimal(r["delist_price"]) if r["delist_price"] else None)
    ticks.sort(key=lambda t: (t.ts, t.symbol))
    _check_listings(ticks, listings)
    return Dataset(manifest, ticks, sorted(actions, key=lambda a: (a.ex_date, a.symbol)), listings, path)


def _rows(path: Path):
    with path.open(newline="", encoding="utf-8") as fh:
        yield from csv.DictReader(fh)


def _check_listings(ticks: List[RawTick], listings: Dict[str, Listing]) -> None:
    for t in ticks:
        lst = listings.get(t.symbol)
        if lst is None:
            raise ConfigError(f"quote for {t.symbol} has no listing record")
        d = t.ts.date()
        if d < lst.listed_from or (lst.delisted_on is not None and d >= lst.delisted_on):
            raise ConfigError(f"quote for {t.symbol} on {d} outside its listing period")


def calendar_violations(ticks: List[RawTick], session) -> List[str]:
    """Quotes stamped outside the trading calendar (weekends, holidays, outside hours)."""
    bad = sorted({t.ts.isoformat() for t in ticks if not _in_session_hours(t.ts, session)})
    return bad


def _in_session_hours(ts: datetime, session) -> bool:
    from zoneinfo import ZoneInfo
    local = ts.astimezone(ZoneInfo(session.timezone))
    weekday = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"][local.weekday()]
    t = local.time()
    return weekday in session.weekdays and local.date() not in session.holidays and session.open <= t < session.close
