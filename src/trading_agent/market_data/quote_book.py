from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional, Set

from trading_agent.contracts.data_quality import DataIssue, DataIssueCode
from trading_agent.market_data.records import QuoteRecord


class QuoteBook:
    """Latest accepted quote per instrument for one source, with fail-closed acceptance rules.

    Rejected (never used for decisions): future-dated, late (latency > max_latency), older than
    the quote already held, or for an instrument quarantined after a sequence gap until a
    snapshot for it arrives. `fresh()` only returns quotes younger than `max_age`.
    """

    def __init__(self, source_id: str, max_age: timedelta, max_latency: timedelta,
                 max_future_skew: timedelta = timedelta(seconds=1)) -> None:
        self.source_id = source_id
        self.max_age, self.max_latency, self.max_future_skew = max_age, max_latency, max_future_skew
        self._latest: Dict[str, QuoteRecord] = {}
        self._quarantined: Set[str] = set()
        self._quarantine_all = False
        self._released: Set[str] = set()

    # ------------------------------------------------------------------ writes
    def apply(self, rec: QuoteRecord) -> Optional[DataIssue]:
        sym = rec.instrument
        if rec.source_ts - rec.received_at > self.max_future_skew:
            return self._issue(DataIssueCode.FUTURE_TIMESTAMP, sym, f"ahead by {rec.source_ts - rec.received_at}")
        if rec.latency > self.max_latency:
            return self._issue(DataIssueCode.LATE_MESSAGE, sym, f"latency {rec.latency}")
        prev = self._latest.get(sym)
        if prev is not None and rec.source_ts < prev.source_ts:
            return self._issue(DataIssueCode.OUT_OF_ORDER, sym, "older than held quote")
        if self.is_quarantined(sym):
            if not rec.is_snapshot:
                return self._issue(DataIssueCode.SEQUENCE_GAP, sym, "quarantined until snapshot")
            self._quarantined.discard(sym)
            if self._quarantine_all:
                self._released.add(sym)
        self._latest[sym] = rec
        return None

    def quarantine(self, instruments: Optional[Iterable[str]] = None) -> None:
        """Quarantine some instruments, or all (None), until a snapshot message for each arrives."""
        if instruments is None:
            self._quarantine_all, self._released = True, set()
        else:
            self._quarantined.update(instruments)

    def clear(self) -> None:
        """Invalidate everything (used on disconnect: held quotes may already be superseded)."""
        self._latest.clear()

    # ------------------------------------------------------------------- reads
    def is_quarantined(self, symbol: str) -> bool:
        return symbol in self._quarantined or (self._quarantine_all and symbol not in self._released)

    def fresh(self, now: datetime) -> List[QuoteRecord]:
        return [r for s, r in sorted(self._latest.items())
                if not self.is_quarantined(s) and now - r.source_ts <= self.max_age]

    def stale_issues(self, now: datetime) -> List[DataIssue]:
        return [self._issue(DataIssueCode.STALE_INSTRUMENT, s, f"age {now - r.source_ts}")
                for s, r in sorted(self._latest.items()) if now - r.source_ts > self.max_age]

    def _issue(self, code: DataIssueCode, symbol: str, detail: str) -> DataIssue:
        return DataIssue(code=code, symbol=symbol, detail=detail, source_id=self.source_id)
