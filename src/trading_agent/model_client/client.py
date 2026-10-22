"""`LLMClient` over a `ModelTransport`: text in, text out, no tools, every failure fails closed.

Failure mapping (planner/critic already turn these into NO_TRADE / DENY):
  timeout, rate limit, 5xx (after bounded retries), auth, bad request  -> LLMUnavailable
  a reply that is not exactly one complete text block (tool use, thinking, several blocks, empty, truncated
  by max_tokens, refusal)                                               -> SchemaFailure
  the answering model differs from the pinned one                       -> ModelVersionMismatch (MODEL_FAILURE)

The reply text is returned byte for byte, so recording and replaying it is exact. Logs carry only metadata
(prompt id/version, attempt, outcome code, latency): never the system prompt, the payload or the reply.
"""
import random
import time
from typing import Any, Callable, Dict, NamedTuple, Optional

from trading_agent.core.errors import LLMUnavailable, SchemaFailure, TradingAgentError
from trading_agent.core.hashing import canonical_json
from trading_agent.model_client.transport import (ModelReply, ModelRequest, ModelTransport, TransportError,
                                                  TransportRateLimited)
from trading_agent.observability.logging import get_logger

log = get_logger("trading_agent.model_client")
COMPLETE_STOP_REASONS = frozenset({"end_turn", "stop_sequence"})


class ModelVersionMismatch(TradingAgentError):
    """The reply came from a different model than the pinned one: attribution would be wrong."""


class RetryPolicy(NamedTuple):
    """Budgeted so a slow model cannot outlive the data it was shown (quotes go stale in seconds)."""
    max_attempts: int = 2
    attempt_timeout_s: float = 10.0
    total_deadline_s: float = 15.0
    backoff_base_s: float = 0.5
    backoff_cap_s: float = 4.0


class SdkLLMClient:
    def __init__(self, transport: ModelTransport, model_id: str, max_tokens: int = 2000,
                 retry: RetryPolicy = RetryPolicy(), sleep: Callable[[float], None] = time.sleep,
                 monotonic: Callable[[], float] = time.monotonic,
                 jitter: Callable[[], float] = random.random) -> None:
        if not model_id or model_id.endswith("-latest"):
            raise ValueError("pin an exact model id (no alias): the model version is part of every audit record")
        self._transport = transport
        self.model_version = model_id
        self._max_tokens = max_tokens
        self._retry = retry
        self._sleep, self._monotonic, self._jitter = sleep, monotonic, jitter

    def complete(self, system_prompt: str, prompt_id: str, prompt_version: str, payload: Dict[str, Any]) -> str:
        user = canonical_json(payload)
        started = self._monotonic()
        for attempt in range(1, self._retry.max_attempts + 1):
            remaining = self._retry.total_deadline_s - (self._monotonic() - started)
            if remaining <= 0:
                self._note(prompt_id, prompt_version, attempt, "DEADLINE", started)
                raise LLMUnavailable("DEADLINE")
            request = ModelRequest(model=self.model_version, system=system_prompt, user=user,
                                   max_tokens=self._max_tokens,
                                   timeout_s=min(self._retry.attempt_timeout_s, remaining))
            try:
                reply = self._transport.send(request)
            except TransportError as exc:
                self._note(prompt_id, prompt_version, attempt, exc.code, started)
                delay = self._delay(exc, attempt)
                if (not exc.retryable or attempt == self._retry.max_attempts
                        or self._monotonic() - started + delay >= self._retry.total_deadline_s):
                    raise LLMUnavailable(exc.code) from None
                self._sleep(delay)
                continue
            try:
                text = self._text_of(reply)
            except TradingAgentError as exc:   # a malformed reply is not retried: it would be paid for twice
                self._note(prompt_id, prompt_version, attempt, type(exc).__name__, started)
                raise
            self._note(prompt_id, prompt_version, attempt, "OK", started)
            return text
        raise LLMUnavailable("NO_ATTEMPT")     # max_attempts < 1

    # ------------------------------------------------------------------ helpers
    def _text_of(self, reply: ModelReply) -> str:
        if reply.model != self.model_version:
            raise ModelVersionMismatch(f"asked {self.model_version}, answered by {reply.model}")
        if not reply.blocks:
            raise SchemaFailure("EMPTY_REPLY")
        for block in reply.blocks:
            if block.type != "text":           # tool_use, thinking, server tools, anything else
                raise SchemaFailure(f"NON_TEXT_BLOCK:{block.type}")
        if len(reply.blocks) != 1:
            raise SchemaFailure("MULTIPLE_BLOCKS")
        if reply.stop_reason not in COMPLETE_STOP_REASONS:
            raise SchemaFailure(f"INCOMPLETE_REPLY:{reply.stop_reason}")
        text = reply.blocks[0].text
        if not text or not text.strip():
            raise SchemaFailure("EMPTY_TEXT")
        return text

    def _delay(self, exc: TransportError, attempt: int) -> float:
        policy = self._retry
        if isinstance(exc, TransportRateLimited) and exc.retry_after_s is not None:
            return min(max(exc.retry_after_s, 0.0), policy.backoff_cap_s)
        return min(policy.backoff_cap_s, policy.backoff_base_s * 2 ** (attempt - 1)) * (0.5 + self._jitter())

    def _note(self, prompt_id: str, prompt_version: str, attempt: int, outcome: str, started: float) -> None:
        log.info("model_call prompt=%s.%s model=%s attempt=%d outcome=%s ms=%d", prompt_id, prompt_version,
                 self.model_version, attempt, outcome, round((self._monotonic() - started) * 1000))
