"""Pin released risk config files in config/risk_config.lock.yaml.

  python scripts/lock_risk_config.py config/risk_policy.v3.yaml config/restricted.v2.yaml
  python scripts/lock_risk_config.py --check

Adds new files only. An already-pinned file whose content changed is refused: released
versions are immutable, so publish a new version instead.
"""
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / ".deps")]

from trading_agent.risk.policy import LOCK_FILE, file_sha256  # noqa: E402

CONFIG = ROOT / "config"


def main(argv) -> int:
    lock_path = CONFIG / LOCK_FILE
    lock = yaml.safe_load(lock_path.read_text(encoding="utf-8")) if lock_path.exists() else {}
    lock = lock or {}
    pinned = dict(lock.get("files", {}))
    if argv == ["--check"]:
        bad = [n for n, h in pinned.items() if not (CONFIG / n).exists() or file_sha256(CONFIG / n) != h]
        print("modified or missing: " + ", ".join(bad) if bad else "all pinned files intact")
        return 1 if bad else 0
    for arg in argv:
        path = Path(arg).resolve()
        name = path.name
        digest = file_sha256(path)
        if name in pinned and pinned[name] != digest:
            print(f"refusing: {name} is already released with a different hash; publish a new version")
            return 1
        pinned[name] = digest
    lock_path.write_text(
        "# SHA-256 of released (immutable) risk configuration files. Managed by scripts/lock_risk_config.py.\n"
        + yaml.safe_dump({"files": dict(sorted(pinned.items()))}, sort_keys=True), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
