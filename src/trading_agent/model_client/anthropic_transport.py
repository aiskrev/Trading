"""`ModelTransport` over the Anthropic Messages API (the `anthropic` SDK). The only module that touches it.

Why the Messages API and not the Claude Agent SDK: the planner is text in, JSON out. The Agent SDK is Claude Code
as a library with built-in Bash/Read/Write/WebFetch tools that would have to be switched off; here no tool can be
offered at all, because `messages.create` is called without a `tools` argument and `ModelRequest` has no field for
one. See docs/model-client.md.

Rules this module keeps:
  * the API key comes from a `SecretsProvider` (the vault), is held only in the SDK client, and appears in no
    message, log or exception; the host is the SDK default (no `base_url` option, so a payload cannot be redirected);
  * the SDK's own retries are off (`max_retries=0`): `SdkLLMClient` owns the retry policy and the deadline;
  * every SDK exception becomes a code-only `TransportError`: no response body or request detail is carried on;
  * no `temperature`/`top_p`/`top_k` (rejected on current models), no `tools`, no `tool_choice`, no `thinking`
    override (adaptive thinking is the model's default) and no server-side fallback model: a refusal ends in
    NO_TRADE, and an answer from an unpinned model would break the audit trail (`ModelVersionMismatch`).

The SDK is imported lazily and can be injected, so the tests run without it and without a network.
"""
from typing import Any, Optional

from trading_agent.core.errors import SecretUnavailable
from trading_agent.model_client.transport import (ContentBlock, ModelReply, ModelRequest, TransportAuthError,
                                                  TransportBadRequest, TransportConnectionError,
                                                  TransportRateLimited, TransportServerError, TransportTimeout)
from trading_agent.secret_store.ports import SecretsProvider

API_KEY_NAME = "anthropic-api-key"


def _load_sdk() -> Any:
    import anthropic  # noqa: PLC0415  the single SDK import in src/ (allow-listed in test_no_network_code)
    return anthropic


class AnthropicMessagesTransport:
    def __init__(self, secrets: SecretsProvider, secret_name: str = API_KEY_NAME, sdk: Optional[Any] = None) -> None:
        self._secrets = secrets
        self._secret_name = secret_name
        self._sdk = sdk
        self._client: Optional[Any] = None

    def send(self, request: ModelRequest) -> ModelReply:
        sdk = self._sdk if self._sdk is not None else _load_sdk()
        client = self._client_for(sdk)
        try:
            message = client.with_options(timeout=request.timeout_s).messages.create(
                model=request.model, max_tokens=request.max_tokens, system=request.system,
                messages=[{"role": "user", "content": request.user}],
                output_config={"effort": request.effort})
        except sdk.APITimeoutError:                       # before APIConnectionError: it is a subclass
            raise TransportTimeout() from None
        except sdk.RateLimitError as exc:
            raise TransportRateLimited(retry_after_s=_retry_after(exc)) from None
        except (sdk.AuthenticationError, sdk.PermissionDeniedError):
            raise TransportAuthError() from None
        except sdk.InternalServerError:                   # 5xx, including 529 overloaded
            raise TransportServerError() from None
        except sdk.APIStatusError as exc:
            status = getattr(exc, "status_code", 0)
            if status == 429:
                raise TransportRateLimited(retry_after_s=_retry_after(exc)) from None
            raise (TransportServerError() if status >= 500 else TransportBadRequest()) from None
        except sdk.APIConnectionError:
            raise TransportConnectionError() from None
        return _reply_of(message)

    def _client_for(self, sdk: Any) -> Any:
        if self._client is None:
            try:
                key = self._secrets.get(self._secret_name).get_secret_value()
            except SecretUnavailable:
                raise TransportAuthError("NO_API_KEY") from None
            self._client = sdk.Anthropic(api_key=key, max_retries=0)
        return self._client

    def __repr__(self) -> str:
        return f"AnthropicMessagesTransport(secret={self._secret_name!r})"

    def __reduce__(self):
        raise TypeError("AnthropicMessagesTransport cannot be serialised")


def _retry_after(exc: Any) -> Optional[float]:
    try:
        return float(exc.response.headers.get("retry-after"))
    except (AttributeError, TypeError, ValueError):
        return None


def _reply_of(message: Any) -> ModelReply:
    blocks = [ContentBlock(type=str(b.type), text=getattr(b, "text", None) if b.type == "text" else None)
              for b in (message.content or [])]          # only text is kept: reasoning is not copied anywhere
    usage = getattr(message, "usage", None)
    return ModelReply(model=str(message.model), blocks=blocks, stop_reason=getattr(message, "stop_reason", None),
                      input_tokens=getattr(usage, "input_tokens", None),
                      output_tokens=getattr(usage, "output_tokens", None))
