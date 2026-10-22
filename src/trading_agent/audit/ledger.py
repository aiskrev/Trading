import json
from pathlib import Path
from typing import List, Optional, Tuple, Union

from trading_agent.contracts.audit import AuditRecord
from trading_agent.contracts.base import content_id
from trading_agent.core.errors import AuditWriteFailure
from trading_agent.core.hashing import canonical_json

GENESIS = "0" * 64


class AuditLedger:
    """Append-only, hash-chained audit log. Optional JSONL persistence.

    If a write fails the caller must HALT: no decision may proceed unaudited.
    """

    def __init__(self, path: Optional[Union[str, Path]] = None) -> None:
        self.path = Path(path) if path else None
        self._records: List[AuditRecord] = []
        self.fail_writes = False  # fault-injection hook

    @property
    def records(self) -> List[AuditRecord]:
        return list(self._records)

    @property
    def head_hash(self) -> str:
        return self._records[-1].record_hash if self._records else GENESIS

    def append(self, **fields) -> AuditRecord:
        if self.fail_writes:
            raise AuditWriteFailure("audit sink unavailable (injected)")
        draft = AuditRecord(seq=len(self._records), prev_hash=self.head_hash, record_hash="", **fields)
        record = draft.model_copy(update={"record_hash": content_id(draft, "record_hash")})
        if self.path is not None:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(canonical_json(record) + "\n")
            except OSError as exc:
                raise AuditWriteFailure(str(exc)) from exc
        self._records.append(record)
        return record

    @classmethod
    def resume(cls, path: Union[str, Path]) -> "Tuple[AuditLedger, Optional[Path]]":
        """Reopen an existing ledger after a restart and continue its hash chain.

        A torn final line (crash mid-write) is moved to `<file>.torn` and reported. Because the
        DECISION record is written before any submission, a torn record never hides an order.
        Any other corruption raises: the run must not continue on an unverifiable ledger.
        """
        ledger = cls(path)
        torn: Optional[Path] = None
        if ledger.path is None or not ledger.path.exists():
            return ledger, None
        raw = ledger.path.read_bytes()
        lines = raw.split(b"\n")
        complete, tail = lines[:-1], lines[-1]
        if tail.strip():
            torn = ledger.path.with_suffix(ledger.path.suffix + ".torn")
            with torn.open("ab") as fh:
                fh.write(tail + b"\n")
            ledger.path.write_bytes(b"".join(l + b"\n" for l in complete))
        records = [AuditRecord.model_validate(json.loads(l)) for l in complete if l.strip()]
        if not cls.verify(records):
            raise AuditWriteFailure(f"{ledger.path}: hash chain broken; refusing to resume")
        ledger._records = records
        return ledger, torn

    @staticmethod
    def verify(records: List[AuditRecord]) -> bool:
        prev = GENESIS
        for i, rec in enumerate(records):
            if rec.seq != i or rec.prev_hash != prev or rec.record_hash != content_id(rec, "record_hash"):
                return False
            prev = rec.record_hash
        return True

    @staticmethod
    def load(path: Union[str, Path]) -> List[AuditRecord]:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
        return [AuditRecord.model_validate(json.loads(line)) for line in lines if line.strip()]
