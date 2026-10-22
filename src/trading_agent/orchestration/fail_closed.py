"""Fail-closed escalation policy. Mirrors docs/failure-mode-register.md; keep them in sync."""
from typing import Iterable, Optional, Tuple

from trading_agent.core.trading_state import TradingState
from trading_agent.normalization.validators import DataIssue, DataIssueCode

# None = the affected symbol is dropped from the snapshot (no new risk on it) but trading continues.
DATA_ISSUE_ESCALATION = {
    DataIssueCode.CLOCK_SKEW: TradingState.HALTED,
    DataIssueCode.SOURCE_DIVERGENCE: TradingState.REDUCE_ONLY,
    DataIssueCode.BAD_PRICE: None,
    DataIssueCode.CROSSED_QUOTE: None,
    DataIssueCode.MISSING_PRIMARY: None,
    DataIssueCode.BAD_SYMBOL: None,
    # Adapter-level issues: the affected quotes/account never reach a snapshot, and the
    # gateway's freshness/NO_QUOTE rules deny any order that would need them.
    DataIssueCode.MALFORMED_MESSAGE: None,
    DataIssueCode.UNKNOWN_INSTRUMENT: None,
    DataIssueCode.FUTURE_TIMESTAMP: None,
    DataIssueCode.LATE_MESSAGE: None,
    DataIssueCode.OUT_OF_ORDER: None,
    DataIssueCode.DUPLICATE_MESSAGE: None,
    DataIssueCode.SEQUENCE_GAP: None,
    DataIssueCode.STALE_INSTRUMENT: None,
    DataIssueCode.STALE_FEED: None,
    DataIssueCode.DISCONNECTED: None,
    DataIssueCode.RATE_LIMITED: None,
    DataIssueCode.AUTH_UNAVAILABLE: None,
    DataIssueCode.AUTH_REJECTED: None,
    DataIssueCode.UPSTREAM_ERROR: None,
    DataIssueCode.ACCOUNT_STALE: None,
}

CONSECUTIVE_REJECTS_BEFORE_REDUCE_ONLY = 3

_SEVERITY = {TradingState.REDUCE_ONLY: 1, TradingState.HALTED: 2}


def escalation_for(issues: Iterable[DataIssue]) -> Optional[Tuple[TradingState, str]]:
    worst: Optional[Tuple[TradingState, str]] = None
    for issue in issues:
        target = DATA_ISSUE_ESCALATION.get(issue.code, TradingState.HALTED)  # unknown issue => HALT
        if target is not None and (worst is None or _SEVERITY[target] > _SEVERITY[worst[0]]):
            worst = (target, f"{getattr(issue.code, 'value', issue.code)}:{issue.symbol or '-'}")
    return worst
