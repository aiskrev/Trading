"""In-memory HTTP transport for tests. Opens no sockets."""
from collections import deque
from typing import Deque, Dict, List, Sequence, Union

from trading_agent.account_data.http import HttpResponse

Step = Union[HttpResponse, Exception]


class ScriptedHttpTransport:
    def __init__(self, steps: Sequence[Step] = ()) -> None:
        self._steps: Deque[Step] = deque(steps)
        self.calls: List[str] = []
        self.auth_header_present: List[bool] = []
        self._last_auth = None

    def push(self, *steps: Step) -> None:
        self._steps.extend(steps)

    def get(self, path: str, headers: Dict[str, str], timeout_s: float) -> HttpResponse:
        self.calls.append(path)
        self.auth_header_present.append("Authorization" in headers)
        self._last_auth = headers.get("Authorization")
        if not self._steps:
            raise TimeoutError("no scripted response")
        step = self._steps.popleft()
        if isinstance(step, Exception):
            raise step
        return step

    def last_auth_matches(self, value: str) -> bool:
        return self._last_auth == value
