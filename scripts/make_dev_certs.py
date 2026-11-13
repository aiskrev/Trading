"""Make development certificates for the risk-service mutual-TLS link.

    python scripts/make_dev_certs.py DIR [--days 30]

DIR must be new or empty and outside any git work tree. The CA private key is never written, so to rotate,
run this again into a new directory and restart both sides. See src/trading_agent/risk_service/pki.py.
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trading_agent.core.errors import ConfigError  # noqa: E402
from trading_agent.risk_service.pki import generate_dev_pki  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("directory")
    ap.add_argument("--days", type=int, default=30)
    args = ap.parse_args(argv)
    try:
        files = generate_dev_pki(args.directory, args.days)
    except ConfigError as exc:
        print(f"refusing: {exc}")
        return 2
    for name, path in sorted(files.items()):
        print(f"{name:20} {path}")
    print("the CA private key was not saved; keep the .key files private")
    return 0


if __name__ == "__main__":
    sys.exit(main())
