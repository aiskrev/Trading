"""Offline LLM clients. BACKTEST never calls a live model: responses are scripted or replayed."""
from typing import Any, Callable, Dict

from trading_agent.core.errors import LLMUnavailable
from trading_agent.core.hashing import sha256_hex


def request_key(prompt_id: str, prompt_version: str, system_prompt: str, payload: Dict[str, Any]) -> str:
    return sha256_hex({"prompt_id": prompt_id, "prompt_version": prompt_version,
                       "system_prompt_sha256": sha256_hex(system_prompt.encode("utf-8")), "payload": payload})


class ScriptedLLM:
    """Responds via a Python function. Used for tests, evals and the demo strategy."""

    def __init__(self, fn: Callable[[Dict[str, Any]], str], model_version: str = "scripted") -> None:
        self._fn = fn
        self.model_version = model_version

    def complete(self, system_prompt: str, prompt_id: str, prompt_version: str, payload: Dict[str, Any]) -> str:
        return self._fn(payload)


class RecordingLLM:
    """Wraps any client and records every response, keyed by the exact request."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.model_version = inner.model_version
        self.recordings: Dict[str, str] = {}

    def complete(self, system_prompt: str, prompt_id: str, prompt_version: str, payload: Dict[str, Any]) -> str:
        out = self._inner.complete(system_prompt, prompt_id, prompt_version, payload)
        self.recordings[request_key(prompt_id, prompt_version, system_prompt, payload)] = out
        return out


class RecordedLLM:
    """Replays recordings. An unseen request is a model failure (-> NO_TRADE), never a guess."""

    def __init__(self, recordings: Dict[str, str], model_version: str) -> None:
        self._recordings = dict(recordings)
        self.model_version = model_version

    def complete(self, system_prompt: str, prompt_id: str, prompt_version: str, payload: Dict[str, Any]) -> str:
        key = request_key(prompt_id, prompt_version, system_prompt, payload)
        if key not in self._recordings:
            raise LLMUnavailable(f"no recorded response for request {key[:12]}")
        return self._recordings[key]
