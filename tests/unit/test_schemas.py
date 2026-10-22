"""AT-05: checked-in JSON Schemas match the pydantic contracts."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_no_schema_drift():
    proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "export_schemas.py"), "--check"],
                          cwd=ROOT, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_contracts_forbid_extra_fields():
    import json
    for path in (ROOT / "schemas").glob("*.json"):
        schema = json.loads(path.read_text(encoding="utf-8"))
        assert schema.get("additionalProperties") is False, path.name
