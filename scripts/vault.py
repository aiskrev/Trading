"""Operator CLI for the in-house vault. Run it yourself, in your own terminal.

  python scripts/vault.py init
  python scripts/vault.py put-secret alpaca-paper-key          (value typed at a hidden prompt)
  python scripts/vault.py create-key risk-signing-1 --days 30
  python scripts/vault.py grant risk-service --read alpaca-paper-key --sign risk-signing-1
  python scripts/vault.py list | export-registry | revoke-key ID | revoke-client ID | verify-log

Passphrase and secret values are only ever read from hidden prompts: never from arguments,
environment variables or files. `grant` prints the client token ONCE; store it in the service's own
environment. The vault lives OUTSIDE the repository (default ~/.trading-agent/); the CLI refuses
any path inside a git work tree.
"""
import argparse
import getpass
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / ".deps")]

from pydantic import SecretStr  # noqa: E402

from trading_agent.audit.chain import ChainedLog  # noqa: E402
from trading_agent.core.errors import TradingAgentError  # noqa: E402
from trading_agent.secret_store.vault import SCRYPT_MIN_N, Vault, VaultEvent  # noqa: E402

DEFAULT_DIR = Path.home() / ".trading-agent"
VAULT_NAME, LOG_NAME = "trading-agent.vault", "trading-agent.vault.access.jsonl"


def _inside_git_worktree(path: Path) -> bool:
    for parent in [path.resolve()] + list(path.resolve().parents):
        if (parent / ".git").exists():
            return True
    return False


def main(argv: Optional[List[str]] = None, prompt: Callable[[str], str] = getpass.getpass,
         out=print, insecure_fast_kdf_for_tests: bool = False) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default=str(DEFAULT_DIR), help="vault directory (outside any git repository)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    p = sub.add_parser("put-secret"); p.add_argument("name")
    p = sub.add_parser("create-key"); p.add_argument("key_id"); p.add_argument("--days", type=int, default=30)
    p = sub.add_parser("grant"); p.add_argument("client_id")
    p.add_argument("--read", nargs="*", default=[]); p.add_argument("--sign", nargs="*", default=[])
    p = sub.add_parser("revoke-key"); p.add_argument("key_id")
    p = sub.add_parser("revoke-client"); p.add_argument("client_id")
    sub.add_parser("list")
    sub.add_parser("export-registry")
    sub.add_parser("verify-log")
    a = ap.parse_args(argv)

    directory = Path(a.dir)
    if _inside_git_worktree(directory):
        out(f"refusing: {directory} is inside a git work tree; keep the vault outside the repository")
        return 2
    vault_path, log_path = directory / VAULT_NAME, directory / LOG_NAME
    try:
        if a.cmd == "init":
            pw = prompt("New vault passphrase (min 12 chars): ")
            if pw != prompt("Repeat passphrase: "):
                out("passphrases differ")
                return 1
            directory.mkdir(parents=True, exist_ok=True)
            kwargs = {"scrypt_n": 2 ** 12, "insecure_fast_kdf_for_tests": True} if insecure_fast_kdf_for_tests \
                else {"scrypt_n": SCRYPT_MIN_N}
            Vault.create(vault_path, pw, log_path, **kwargs)
            out(f"vault created at {vault_path}. Back up the file; keep the passphrase somewhere safe and offline.")
            return 0
        if a.cmd == "verify-log":
            log, opened = ChainedLog.open(log_path, VaultEvent)
            out(f"access log verified: {opened.entries} events, head {log.head_hash[:16]}...")
            return 0
        vault = Vault.open(vault_path, prompt("Vault passphrase: "), log_path)
        if a.cmd == "put-secret":
            version = vault.put_secret(a.name, SecretStr(prompt(f"Value for {a.name} (hidden): ")))
            out(f"stored {a.name} (version {version})")
        elif a.cmd == "create-key":
            now = datetime.now(timezone.utc)
            pub = vault.create_signing_key(a.key_id, now - timedelta(minutes=1), now + timedelta(days=a.days))
            out(f"created signing key {a.key_id}; public key {pub.hex()}")
        elif a.cmd == "grant":
            out(f"client token for {a.client_id} (shown once; store it in the service's environment):")
            out(vault.grant(a.client_id, a.read, a.sign))
        elif a.cmd == "revoke-key":
            vault.revoke_key(a.key_id)
            out(f"revoked {a.key_id}")
        elif a.cmd == "revoke-client":
            vault.revoke_client(a.client_id)
            out(f"revoked client {a.client_id}")
        elif a.cmd == "list":
            out(json.dumps(vault.inventory(), indent=1))
        elif a.cmd == "export-registry":
            out(json.dumps(vault.key_registry(), indent=1, sort_keys=True))
        return 0
    except TradingAgentError as exc:
        out(f"error: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
