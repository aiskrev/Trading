"""AT-04: no credentials in the repo; redaction; secrets provider unavailable; signer key not exportable."""
import logging
import pickle
import re
import subprocess
from pathlib import Path

import pytest

from trading_agent.core.errors import SecretUnavailable
from trading_agent.core.signing import OrderSigner
from trading_agent.observability.logging import REDACTED, get_logger, redact
from trading_agent.secret_store.unavailable import UnavailableSecretsProvider

ROOT = Path(__file__).resolve().parents[2]
SKIP_DIRS = {".deps", ".git", ".venv", "__pycache__", ".pytest_cache", ".hypothesis", ".import_linter_cache"}
TEXT_SUFFIXES = {".py", ".md", ".yaml", ".yml", ".toml", ".json", ".csv", ".txt", ".cfg", ".ini", ""}

# Built by concatenation so this file does not match its own patterns.
SECRET_PATTERNS = [
    re.compile("sk-" + r"ant-[A-Za-z0-9_\-]{20,}"),
    re.compile("AK" + r"IA[0-9A-Z]{16}"),
    re.compile("-----BEGIN " + r"[A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)(api[_-]?key|secret|password|refresh[_-]?token)\s*[:=]\s*['\"][^'\"\s]{12,}['\"]"),
    re.compile("xox" + r"[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile("gh" + r"p_[A-Za-z0-9]{30,}"),
]


def _repo_files():
    for path in ROOT.rglob("*"):
        if path.is_file() and not (set(path.relative_to(ROOT).parts) & SKIP_DIRS) and path.suffix in TEXT_SUFFIXES:
            yield path


def test_no_secret_material_in_repo_files():
    hits = []
    for path in _repo_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        for pat in SECRET_PATTERNS:
            if pat.search(text):
                hits.append(f"{path.relative_to(ROOT)}: {pat.pattern[:30]}")
    assert hits == []


def test_no_secret_material_in_git_history():
    if not (ROOT / ".git").exists():
        pytest.skip("not a git repository yet; CI runs gitleaks over full history")
    log = subprocess.run(["git", "log", "-p", "--all"], cwd=ROOT, capture_output=True, text=True,
                         encoding="utf-8", errors="ignore").stdout
    assert not any(p.search(log) for p in SECRET_PATTERNS)


def test_no_env_or_key_files_present():
    banned = [p for p in _repo_files() if p.name.startswith(".env") or p.suffix in {".pem", ".key", ".p12"}]
    assert banned == []


@pytest.mark.parametrize("raw", [
    "calling with api_key=" + "abcd1234efgh5678",
    "Authorization: Bearer " + "eyJhbGciOiJIUzI1NiJ9.payload.sig",
    "token: " + "s3cr3t-t0k3n-value",
    "key " + "sk-" + "ant-api03-ABCDEFGHIJKLMNOP",
])
def test_redaction(raw):
    out = redact(raw)
    assert REDACTED in out
    assert raw.split()[-1] not in out


def test_logger_redacts(caplog):
    log = get_logger("trading_agent.test")
    with caplog.at_level(logging.INFO, logger="trading_agent.test"):
        log.info("password=%s", "hunter2-hunter2")
    assert "hunter2" not in caplog.text
    assert REDACTED in caplog.text


def test_secrets_provider_is_unavailable_at_this_stage():
    with pytest.raises(SecretUnavailable):
        UnavailableSecretsProvider().get("BROKER_API_KEY")


def test_signer_key_cannot_be_exported():
    signer = OrderSigner()
    assert "ephemeral" in repr(signer)
    with pytest.raises(TypeError):
        pickle.dumps(signer)
    assert not any("key" in name.lower() for name in vars(signer) if not name.startswith("_OrderSigner"))
