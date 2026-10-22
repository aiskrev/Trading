"""Fail-closed trading state, separate from the production Mode.

Failure handlers escalate NORMAL -> REDUCE_ONLY -> HALTED. There is no
programmatic de-escalation: a new run (after human review) starts NORMAL.
"""
from datetime import datetime
from enum import Enum
from typing import List, NamedTuple


class TradingState(str, Enum):
    NORMAL = "NORMAL"
    REDUCE_ONLY = "REDUCE_ONLY"
    HALTED = "HALTED"


_SEVERITY = {TradingState.NORMAL: 0, TradingState.REDUCE_ONLY: 1, TradingState.HALTED: 2}


class StateTransition(NamedTuple):
    at: datetime
    from_state: TradingState
    to_state: TradingState
    reason: str


class TradingStateMachine:
    def __init__(self) -> None:
        self._state = TradingState.NORMAL
        self._history: List[StateTransition] = []

    @property
    def state(self) -> TradingState:
        return self._state

    @property
    def history(self) -> List[StateTransition]:
        return list(self._history)

    def trip(self, to: TradingState, reason: str, at: datetime) -> bool:
        """Escalate if `to` is more severe. Returns True if the state changed."""
        if _SEVERITY[to] <= _SEVERITY[self._state]:
            return False
        self._history.append(StateTransition(at, self._state, to, reason))
        self._state = to
        return True
