"""Strict decoding of vendor payloads shared by market-data and account adapters."""
import json
from decimal import Decimal
from typing import Any, Dict, Union

from trading_agent.contracts.data_quality import DataIssueCode

DEFAULT_MAX_BYTES = 64 * 1024


class DecodeError(Exception):
    """A payload we refuse to use. `detail` never echoes payload content."""

    def __init__(self, code: DataIssueCode, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail[:200]


def _no_duplicate_keys(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise DecodeError(DataIssueCode.MALFORMED_MESSAGE, "duplicate key")
        out[k] = v
    return out


def _reject_constant(_):
    raise DecodeError(DataIssueCode.MALFORMED_MESSAGE, "non-finite number")


def strict_json_object(raw: Union[bytes, str], max_bytes: int = DEFAULT_MAX_BYTES) -> Dict[str, Any]:
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if not isinstance(raw, (bytes, bytearray)):
        raise DecodeError(DataIssueCode.MALFORMED_MESSAGE, "payload is not bytes")
    if len(raw) > max_bytes:
        raise DecodeError(DataIssueCode.MALFORMED_MESSAGE, f"payload exceeds {max_bytes} bytes")
    try:
        text = bytes(raw).decode("utf-8")
    except UnicodeDecodeError:
        raise DecodeError(DataIssueCode.MALFORMED_MESSAGE, "invalid utf-8") from None
    try:
        data = json.loads(text, parse_float=Decimal, object_pairs_hook=_no_duplicate_keys,
                          parse_constant=_reject_constant)
    except json.JSONDecodeError as exc:
        raise DecodeError(DataIssueCode.MALFORMED_MESSAGE, f"invalid json at pos {exc.pos}") from None
    if not isinstance(data, dict):
        raise DecodeError(DataIssueCode.MALFORMED_MESSAGE, "top level is not an object")
    return data
