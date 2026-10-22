"""Append-only, hash-chained JSONL log that survives crashes.

Each line: {"seq", "prev_hash", "record_hash", "body"}. `open()` resumes an existing file:
the chain is verified; a torn final line (crash mid-write) is moved aside to `<file>.torn`
and reported, never silently repaired; any other corruption refuses to open.
"""
import json
from pathlib import Path
from typing import Any, Dict, Generic, List, NamedTuple, Optional, Type, TypeVar, Union

from pydantic import BaseModel

from trading_agent.core.errors import AuditWriteFailure, TradingAgentError
from trading_agent.core.hashing import canonical_json, sha256_hex

GENESIS = "0" * 64
M = TypeVar("M", bound=BaseModel)


class ChainCorrupted(TradingAgentError):
    pass


class OpenResult(NamedTuple):
    torn_line_moved_to: Optional[Path]
    entries: int


class ChainedLog(Generic[M]):
    def __init__(self, path: Union[str, Path], model: Type[M]) -> None:
        self.path = Path(path)
        self.model = model
        self._bodies: List[M] = []
        self._head = GENESIS
        self.fail_writes = False  # fault-injection hook

    @classmethod
    def open(cls, path: Union[str, Path], model: Type[M]) -> "tuple":
        log = cls(path, model)
        return log, log._load()

    @property
    def head_hash(self) -> str:
        return self._head

    @property
    def items(self) -> List[M]:
        return list(self._bodies)

    def append(self, body: M) -> str:
        if self.fail_writes:
            raise AuditWriteFailure("log sink unavailable (injected)")
        payload = body.model_dump(mode="json")
        seq = len(self._bodies)
        record_hash = sha256_hex({"seq": seq, "prev_hash": self._head, "body": payload})
        line = canonical_json({"seq": seq, "prev_hash": self._head, "record_hash": record_hash, "body": payload})
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("ab") as fh:
                fh.write((line + "\n").encode("utf-8"))
                fh.flush()
        except OSError as exc:
            raise AuditWriteFailure(str(exc)) from exc
        self._bodies.append(body)
        self._head = record_hash
        return record_hash

    def _load(self) -> OpenResult:
        if not self.path.exists():
            return OpenResult(None, 0)
        raw = self.path.read_bytes()
        lines = raw.split(b"\n")
        complete, tail = lines[:-1], lines[-1]   # a well-formed file ends with "\n" => tail == b""
        torn: Optional[Path] = None
        if tail.strip():
            torn = self.path.with_suffix(self.path.suffix + ".torn")
            with torn.open("ab") as fh:
                fh.write(tail + b"\n")
            self.path.write_bytes(b"".join(l + b"\n" for l in complete))
        for i, line in enumerate(complete):
            if not line.strip():
                raise ChainCorrupted(f"{self.path}: empty line {i}")
            try:
                row: Dict[str, Any] = json.loads(line)
            except ValueError as exc:
                raise ChainCorrupted(f"{self.path}: unreadable line {i}") from exc
            expected = sha256_hex({"seq": i, "prev_hash": self._head, "body": row["body"]})
            if row.get("seq") != i or row.get("prev_hash") != self._head or row.get("record_hash") != expected:
                raise ChainCorrupted(f"{self.path}: hash chain broken at line {i}")
            self._bodies.append(self.model.model_validate(row["body"]))
            self._head = expected
        return OpenResult(torn, len(self._bodies))
