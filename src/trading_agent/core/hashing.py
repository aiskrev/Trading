import hashlib
import json
from decimal import Decimal
from typing import Any

from pydantic import BaseModel


def _default(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    if hasattr(obj, "value"):  # Enum
        return obj.value
    raise TypeError(f"not canonicalisable: {type(obj).__name__}")


def canonical_json(obj: Any) -> str:
    if isinstance(obj, BaseModel):
        obj = obj.model_dump(mode="json")
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=_default, ensure_ascii=True)


def sha256_hex(obj: Any) -> str:
    data = obj if isinstance(obj, (bytes, bytearray)) else canonical_json(obj).encode("utf-8")
    return hashlib.sha256(data).hexdigest()
