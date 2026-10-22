from enum import Enum
from typing import Dict, NamedTuple, Optional


class SeqStatus(str, Enum):
    FIRST = "FIRST"
    OK = "OK"
    GAP = "GAP"
    DUPLICATE = "DUPLICATE"
    OUT_OF_ORDER = "OUT_OF_ORDER"


class SeqCheck(NamedTuple):
    status: SeqStatus
    expected: Optional[int]
    got: int


class SequenceTracker:
    """Detects gaps, duplicates and reordering for feeds that publish sequence numbers.

    Keys are "*" for a feed-wide sequence or an instrument for per-instrument sequences.
    After a GAP tracking continues from the new number; the caller decides how to resync.
    """

    def __init__(self) -> None:
        self._last: Dict[str, int] = {}

    def observe(self, key: str, seq: int) -> SeqCheck:
        last = self._last.get(key)
        if last is None:
            self._last[key] = seq
            return SeqCheck(SeqStatus.FIRST, None, seq)
        expected = last + 1
        if seq == expected:
            self._last[key] = seq
            return SeqCheck(SeqStatus.OK, expected, seq)
        if seq == last:
            return SeqCheck(SeqStatus.DUPLICATE, expected, seq)
        if seq < last:
            return SeqCheck(SeqStatus.OUT_OF_ORDER, expected, seq)
        self._last[key] = seq
        return SeqCheck(SeqStatus.GAP, expected, seq)

    def reset(self) -> None:
        self._last.clear()
