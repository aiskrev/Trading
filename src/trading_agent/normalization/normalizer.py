import re
from datetime import datetime
from decimal import Decimal
from typing import Dict, Iterable, List, Mapping, NamedTuple, Optional

from trading_agent.contracts.market import InstrumentStatus, MarketSnapshot, NewsItem, Quote, VenueStatus
from trading_agent.market_data.ports import RawTick
from trading_agent.normalization.validators import DataIssue, DataIssueCode, divergence_bps, validate_tick

_SYMBOL = re.compile(r"^[A-Z][A-Z0-9.\-]{0,14}$")


class NormalizationResult(NamedTuple):
    snapshot: MarketSnapshot
    issues: List[DataIssue]


class Normalizer:
    """Turns raw multi-source ticks into one immutable, content-hashed MarketSnapshot.

    A symbol with any data issue is dropped from the snapshot (fail closed) and reported.
    """

    def __init__(self, primary_source: str, secondary_source: Optional[str] = None,
                 max_divergence_bps: Decimal = Decimal(50)) -> None:
        self.primary = primary_source
        self.secondary = secondary_source
        self.max_divergence_bps = max_divergence_bps

    def build(self, ticks: Iterable[RawTick], as_of: datetime, received_at: datetime,
              venue_status: VenueStatus, news: Iterable[NewsItem] = (),
              instrument_status: Optional[Mapping[str, InstrumentStatus]] = None) -> NormalizationResult:
        """instrument_status: per-symbol halt state from the feed. Symbols without one are UNKNOWN."""
        issues: List[DataIssue] = []
        latest: Dict[str, Dict[str, RawTick]] = {}
        for t in ticks:
            if t.ts > as_of:
                issues.append(DataIssue(code=DataIssueCode.CLOCK_SKEW, symbol=t.symbol,
                                        detail=f"tick ts {t.ts.isoformat()} after as_of {as_of.isoformat()}"))
                continue
            by_source = latest.setdefault(t.symbol, {})
            if t.source_id not in by_source or by_source[t.source_id].ts <= t.ts:
                by_source[t.source_id] = t

        quotes: Dict[str, Quote] = {}
        for symbol in sorted(latest):
            by_source = latest[symbol]
            if not _SYMBOL.match(symbol):
                issues.append(DataIssue(code=DataIssueCode.BAD_SYMBOL, symbol=None, detail=symbol[:32]))
                continue
            primary = by_source.get(self.primary)
            if primary is None:
                issues.append(DataIssue(code=DataIssueCode.MISSING_PRIMARY, symbol=symbol))
                continue
            bad = validate_tick(primary)
            if bad:
                issues.append(bad)
                continue
            secondary = by_source.get(self.secondary) if self.secondary else None
            if secondary is not None and validate_tick(secondary) is None:
                div = divergence_bps(primary, secondary)
                if div > self.max_divergence_bps:
                    issues.append(DataIssue(code=DataIssueCode.SOURCE_DIVERGENCE, symbol=symbol,
                                            detail=f"{div:.2f}bps > {self.max_divergence_bps}bps"))
                    continue
            quotes[symbol] = Quote(bid=primary.bid, ask=primary.ask, bid_size=primary.bid_size,
                                   ask_size=primary.ask_size, last=primary.last, ts=primary.ts)

        sources = sorted({self.primary} | ({self.secondary} if self.secondary else set()))
        statuses = {s: instrument_status[s] for s in quotes if instrument_status and s in instrument_status}
        snapshot = MarketSnapshot.build(as_of=as_of, received_at=received_at, venue_status=venue_status,
                                        quotes=quotes, instrument_status=statuses, news=list(news),
                                        source_ids=sources)
        return NormalizationResult(snapshot, issues)
