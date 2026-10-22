from typing import Any, Dict

from typing_extensions import Protocol


class LLMClient(Protocol):
    """Text in, text out. Has no tools: the model cannot call anything, only answer."""

    model_version: str

    def complete(self, system_prompt: str, prompt_id: str, prompt_version: str,
                 payload: Dict[str, Any]) -> str:
        """Raise trading_agent.core.errors.LLMUnavailable on timeout/transport failure."""
