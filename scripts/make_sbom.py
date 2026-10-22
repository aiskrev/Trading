"""Software bill of materials from the hash-locked requirements: what exactly runs, with hashes.

  python scripts/make_sbom.py            write requirements/sbom.cdx.json (CycloneDX 1.5) from requirements/model.lock
  python scripts/make_sbom.py --check    fail if the committed SBOM no longer matches the lock (used by CI and the tests)

The lock files are produced with pip-tools on Python 3.12 (see requirements/README.md); the SBOM is derived from
`model.lock` (the runtime plus the model client), so it lists every package the trading process can import.
"""
import json
import re
import sys
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "requirements" / "model.lock"
SBOM = ROOT / "requirements" / "sbom.cdx.json"

PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s\\;]+)")
HASH = re.compile(r"--hash=sha256:([0-9a-f]{64})")


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_lock(text: str) -> List[Dict]:
    """Every pinned package with its sha256 hashes. Anything that is not `name==version` plus hashes is an error."""
    packages: List[Dict] = []
    for line in text.splitlines():
        stripped = line.strip().rstrip("\\").strip()
        if not stripped or stripped.startswith("#"):
            continue
        pin = PIN.match(stripped)
        if pin:
            packages.append({"name": normalize(pin.group(1)), "version": pin.group(2), "hashes": []})
        elif stripped.startswith("--hash=") and packages:
            match = HASH.fullmatch(stripped)
            if not match:
                raise ValueError(f"malformed hash line: {stripped[:60]}")
            packages[-1]["hashes"].append(match.group(1))
        elif stripped.startswith("via ") or stripped.startswith("# via"):
            continue
        else:
            raise ValueError(f"unexpected line in a lock file (editable, URL, VCS or unpinned?): {stripped[:80]}")
    return packages


def build_sbom(packages: List[Dict]) -> Dict:
    return {
        "bomFormat": "CycloneDX", "specVersion": "1.5", "version": 1,
        "metadata": {"component": {"type": "application", "name": "trading-agent", "version": "0.1.0"},
                     "properties": [{"name": "source", "value": "requirements/model.lock"}]},
        "components": [{"type": "library", "name": p["name"], "version": p["version"],
                       "purl": f"pkg:pypi/{p['name']}@{p['version']}",
                       "hashes": [{"alg": "SHA-256", "content": h} for h in sorted(p["hashes"])]}
                      for p in sorted(packages, key=lambda p: p["name"])],
    }


def render(packages: List[Dict]) -> str:
    return json.dumps(build_sbom(packages), indent=2, sort_keys=True) + "\n"


def main(argv: List[str]) -> int:
    expected = render(parse_lock(LOCK.read_text(encoding="utf-8")))
    if "--check" in argv:
        current = SBOM.read_text(encoding="utf-8").replace("\r\n", "\n") if SBOM.exists() else ""
        if current != expected:
            print("SBOM is out of date with requirements/model.lock (run scripts/make_sbom.py)")
            return 1
        print("SBOM matches the lock")
        return 0
    SBOM.write_bytes(expected.encode("utf-8"))
    print(f"wrote {SBOM.relative_to(ROOT)} ({expected.count('pkg:pypi')} components)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
