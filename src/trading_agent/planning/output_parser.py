import json
from decimal import Decimal
from typing import Type, TypeVar

from pydantic import BaseModel, ValidationError

from trading_agent.core.errors import SchemaFailure

M = TypeVar("M", bound=BaseModel)
MAX_OUTPUT_CHARS = 20_000


def _no_duplicate_keys(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise SchemaFailure(f"duplicate key {k!r}")
        out[k] = v
    return out


def _reject_constant(name):
    raise SchemaFailure(f"non-finite number {name}")


def parse_strict(raw: str, model: Type[M]) -> M:
    """Exactly one JSON object, nothing else: no markdown fences, no prose, no duplicate keys."""
    if not isinstance(raw, str):
        raise SchemaFailure("model output is not text")
    if len(raw) > MAX_OUTPUT_CHARS:
        raise SchemaFailure("model output too long")
    text = raw.strip()
    if not (text.startswith("{") and text.endswith("}")):
        raise SchemaFailure("output must be a single JSON object with no surrounding text")
    try:
        data = json.loads(text, parse_float=Decimal, object_pairs_hook=_no_duplicate_keys,
                          parse_constant=_reject_constant)
    except json.JSONDecodeError as exc:
        raise SchemaFailure(f"invalid JSON: {exc.msg}") from exc
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise SchemaFailure(f"schema validation failed: {exc.error_count()} error(s)") from exc
