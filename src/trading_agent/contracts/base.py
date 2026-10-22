from datetime import datetime

from pydantic import BaseModel, ConfigDict

from trading_agent.core.hashing import sha256_hex


class Contract(BaseModel):
    """Immutable, closed-world base: unknown fields are a schema failure, not ignored."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=False)


def require_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return value


def content_id(model: BaseModel, id_field: str) -> str:
    """Hash of every field except the id itself."""
    return sha256_hex(model.model_dump(mode="json", exclude={id_field}))


COMPUTING_ID = "computing_content_id"


def build_with_content_id(cls, id_field: str, **fields):
    """Validate `fields`, then return an instance whose `id_field` is the hash of the *validated* content
    (so dicts and model instances for the same data give the same id)."""
    draft = cls.model_validate({**fields, id_field: ""}, context={COMPUTING_ID: True})
    return cls.model_validate({**fields, id_field: content_id(draft, id_field)})


def computing_id(info) -> bool:
    return bool(info.context and info.context.get(COMPUTING_ID))
