"""The pre-push gate detects secrets and forbidden files, and the hooks are wired correctly."""
import importlib.util
import secrets
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("pre_push_check", ROOT / "scripts" / "pre_push_check.py")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def _fake(prefix: str, n: int) -> str:
    return prefix + secrets.token_hex(n)  # generated at runtime: no secret literal in source


@pytest.mark.parametrize("line", [
    lambda: "key = " + _fake("sk-" + "ant-api03-", 16),
    lambda: "AK" + "IA" + secrets.token_hex(8).upper(),
    lambda: "-----BEGIN " + "OPENSSH PRIVATE KEY-----",
    lambda: "remote = https://x:" + _fake("gh" + "p_", 18) + "@github.com",
    lambda: 'password = "' + secrets.token_hex(6) + '"',
    lambda: "user: aiskrev  pass: " + secrets.token_hex(6) + "!",
    lambda: "API_KEY='" + secrets.token_hex(10) + "'",
])
def test_secret_lines_are_caught(line):
    assert gate.scan_text([line()])


@pytest.mark.parametrize("line", [
    "credential_name: MARKET_DATA_VENDOR_A_TOKEN",
    "pass_filenames: false",
    'log.info("password=%s", value)',
    "def get(self, name: str) -> SecretStr:",
    "max_order_notional: \"10000\"",
])
def test_ordinary_lines_pass(line):
    assert gate.scan_text([line]) == []


@pytest.mark.parametrize("name,bad", [
    (".env", True), ("config/.env.local", True), ("keys/broker.pem", True), ("id_ed25519", True),
    ("secrets.yaml", True), ("src/trading_agent/secret_store/ports.py", False), ("README.md", False),
])
def test_forbidden_file_names(name, bad):
    assert bool(gate.scan_names([name])) is bad


def test_hook_git_variables_are_stripped_from_subprocesses():
    env = gate.clean_env({"GIT_INDEX_FILE": "/x/index", "GIT_DIR": "/x", "GIT_TERMINAL_PROMPT": "0", "PATH": "p"})
    assert "GIT_INDEX_FILE" not in env and "GIT_DIR" not in env
    assert env["GIT_TERMINAL_PROMPT"] == "0" and env["PATH"] == "p"


def test_gate_errors_block_with_a_message(capsys, monkeypatch):
    monkeypatch.setattr(gate, "_main", lambda lines: 1 / 0)
    assert gate.main(["x"]) == 1
    assert "BLOCKED: gate error ZeroDivisionError" in capsys.readouterr().out


def test_branch_deletion_needs_no_checks():
    assert gate.main([f"(delete) {gate.ZERO} refs/heads/old {'a' * 40}"]) == 0


def test_hooks_are_versioned_and_call_the_gate():
    pre_push = (ROOT / ".githooks" / "pre-push").read_text(encoding="utf-8")
    post_commit = (ROOT / ".githooks" / "post-commit").read_text(encoding="utf-8")
    assert "scripts/pre_push_check.py" in pre_push and pre_push.startswith("#!/bin/sh")
    assert "git push" in post_commit and "--no-verify" not in post_commit + pre_push
    assert "TRADING_AGENT_NO_AUTOPUSH" in post_commit
