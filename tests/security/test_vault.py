"""In-house vault: encryption at rest, tamper/rollback detection, sign-only keys, ACL, audit, no leaks, CLI."""
import inspect
import json
import logging
import pickle
import secrets
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from trading_agent.audit.chain import ChainedLog
from trading_agent.core.clock import SimulatedClock
from trading_agent.core.errors import SecretUnavailable, VaultDenied, VaultError
from trading_agent.secret_store.vault import Vault, VaultClient, VaultEvent, VaultSecretsProvider, VaultSigner

T0 = datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc)
FAST = dict(scrypt_n=2 ** 12, insecure_fast_kdf_for_tests=True)


def phrase() -> str:
    return "pw-" + secrets.token_hex(8)        # generated at runtime: no credential literal in source


class Env:
    def __init__(self, tmp_path):
        self.dir = tmp_path
        self.path, self.log = tmp_path / "t.vault", tmp_path / "t.vault.access.jsonl"
        self.pw = phrase()
        self.secret = "S3C-" + secrets.token_hex(12)
        self.clock = SimulatedClock(T0)
        self.vault = Vault.create(self.path, self.pw, self.log, clock=self.clock, **FAST)
        self.vault.put_secret("broker-paper-key", self.secret)
        self.pub = self.vault.create_signing_key("risk-signing-1", T0 - timedelta(hours=1), T0 + timedelta(days=1))
        self.token = self.vault.grant("risk-service", read=["broker-paper-key"], sign=["risk-signing-1"])
        self.client = self.vault.as_client(self.token)

    def reopen(self):
        return Vault.open(self.path, self.pw, self.log, clock=self.clock)

    def events(self):
        return ChainedLog.open(self.log, VaultEvent)[0].items

    def raw_private(self, key_id="risk-signing-1") -> bytes:
        v = self.vault
        return v._unseal(key_id, v._doc["body"]["entries"][key_id])


@pytest.fixture()
def env(tmp_path):
    return Env(tmp_path)


# ------------------------------------------------------------------ at rest
def test_nothing_sensitive_is_in_the_file_or_log(env):
    blob = env.path.read_bytes() + env.log.read_bytes()
    for needle in (env.secret.encode(), env.pw.encode(), env.token.split(":", 1)[1].encode(),
                   env.raw_private(), env.raw_private().hex().encode()):
        assert needle not in blob


def test_passphrase_is_never_stored_and_wrong_one_fails(env):
    with pytest.raises(VaultError):
        Vault.open(env.path, phrase(), env.log)
    with pytest.raises(VaultError, match="at least 12"):
        Vault.open(env.path, "short", env.log)
    assert env.reopen().inventory()[0]["name"] == "broker-paper-key"


def test_create_refuses_overwrite_weak_kdf_and_short_passphrase(tmp_path, env):
    with pytest.raises(VaultError, match="already exists"):
        Vault.create(env.path, phrase(), env.log, **FAST)
    with pytest.raises(VaultError, match="scrypt n"):
        Vault.create(tmp_path / "x.vault", phrase(), tmp_path / "x.log", scrypt_n=2 ** 12)
    with pytest.raises(VaultError, match="at least 12"):
        Vault.create(tmp_path / "y.vault", "tiny", tmp_path / "y.log", **FAST)


# ------------------------------------------------------------------ tamper / rollback
def _edit(env, fn):
    doc = json.loads(env.path.read_text(encoding="utf-8"))
    fn(doc)
    env.path.write_text(json.dumps(doc), encoding="utf-8")


def test_acl_tampering_is_detected(env):
    _edit(env, lambda d: d["body"]["clients"]["risk-service"]["read"].append("signing-secret"))
    with pytest.raises(VaultError, match="modified"):
        env.reopen()


def test_ciphertext_bit_flip_is_detected(env):
    def flip(d):
        ct = d["body"]["entries"]["broker-paper-key"]["ct"]
        d["body"]["entries"]["broker-paper-key"]["ct"] = ("A" if ct[0] != "A" else "B") + ct[1:]
    _edit(env, flip)
    with pytest.raises(VaultError):
        env.reopen()


def test_ciphertext_cannot_be_moved_under_another_name(env):
    """AAD binds ciphertext to name/kind/version, so a swapped entry fails authentication on read."""
    v = env.vault
    v.put_secret("other-secret", "x" * 20)
    entries = v._doc["body"]["entries"]
    entries["other-secret"]["ct"], entries["other-secret"]["nonce"] = (entries["broker-paper-key"]["ct"],
                                                                      entries["broker-paper-key"]["nonce"])
    reader = v.as_client(v.grant("reader", read=["other-secret"]))
    with pytest.raises(VaultError, match="failed authentication"):
        reader.get("other-secret")


def test_old_file_restored_over_a_newer_one_is_refused(env):
    stale = env.path.read_bytes()
    env.vault.put_secret("later-secret", "y" * 20)
    env.reopen()                                     # fine
    env.path.write_bytes(stale)                      # attacker/backup restores the older file
    with pytest.raises(VaultError, match="older than its access log"):
        env.reopen()


def test_tampered_access_log_refuses_to_open(env):
    lines = env.log.read_bytes().split(b"\n")
    lines[1] = lines[1].replace(b"ALLOWED", b"DENIED", 1)
    env.log.write_bytes(b"\n".join(lines))
    with pytest.raises(Exception, match="chain"):
        env.reopen()


# ------------------------------------------------------------------ sign-only keys
def test_signatures_verify_with_the_public_key_only(env):
    sig = env.client.sign("risk-signing-1", b"order-payload")
    pub = Ed25519PublicKey.from_public_bytes(env.pub)
    pub.verify(sig, b"order-payload")
    with pytest.raises(InvalidSignature):
        pub.verify(sig, b"order-payload!")
    assert env.client.public_key("risk-signing-1") == env.pub
    assert env.vault.key_registry()["risk-signing-1"]["public_key"] == env.pub.hex()


def test_no_api_returns_private_key_material(env):
    private = env.raw_private()
    assert len(private) == 32
    surface = {n for cls in (Vault, VaultClient, VaultSigner, VaultSecretsProvider)
               for n in dir(cls) if not n.startswith("_")}
    assert not {n for n in surface if "private" in n or "export" in n or "secret_key" in n}
    returned = [env.client.sign("risk-signing-1", b"m"), env.client.public_key("risk-signing-1"),
                env.vault.key_registry(), env.vault.inventory(), repr(env.vault), repr(env.client)]
    assert all(private.hex() not in str(r) and private not in (r if isinstance(r, bytes) else b"") for r in returned)
    with pytest.raises(VaultDenied):
        env.client.get("risk-signing-1")             # a signing key can never be read as a secret
    greedy = env.vault.as_client(env.vault.grant("greedy", read=["risk-signing-1"]))
    with pytest.raises(VaultDenied):
        greedy.get("risk-signing-1")                 # even if an ACL mistakenly lists a signing key


def test_key_validity_window_and_revocation(env, tmp_path):
    env.clock.advance(2 * 86400)
    with pytest.raises(VaultDenied):
        env.client.sign("risk-signing-1", b"m")      # expired
    early = Vault.create(tmp_path / "e.vault", phrase(), tmp_path / "e.log", clock=SimulatedClock(T0), **FAST)
    early.create_signing_key("future-key-1", T0 + timedelta(hours=1), T0 + timedelta(days=1))
    c = early.as_client(early.grant("svc", sign=["future-key-1"]))
    with pytest.raises(VaultDenied):
        c.sign("future-key-1", b"m")                 # not yet valid


def test_revoked_key_cannot_sign(tmp_path):
    (tmp_path / "r").mkdir()
    fresh = Env(tmp_path / "r")
    fresh.client.sign("risk-signing-1", b"before")
    fresh.vault.revoke_key("risk-signing-1")
    with pytest.raises(VaultDenied):
        fresh.client.sign("risk-signing-1", b"after")
    assert fresh.vault.key_registry()["risk-signing-1"]["revoked"] is True


def test_rotation_overlap_with_a_new_key_id(env):
    new_pub = env.vault.create_signing_key("risk-signing-2", T0, T0 + timedelta(days=30))
    c = env.vault.as_client(env.vault.grant("risk-service", read=["broker-paper-key"],
                                            sign=["risk-signing-1", "risk-signing-2"]))
    Ed25519PublicKey.from_public_bytes(env.pub).verify(c.sign("risk-signing-1", b"a"), b"a")
    Ed25519PublicKey.from_public_bytes(new_pub).verify(c.sign("risk-signing-2", b"a"), b"a")
    with pytest.raises(VaultError, match="already exists"):
        env.vault.create_signing_key("risk-signing-2", T0, T0 + timedelta(days=1))
    for bad in (b"", b"x" * (64 * 1024 + 1), "text"):
        with pytest.raises(VaultDenied):
            c.sign("risk-signing-1", bad)


# ------------------------------------------------------------------ ACL
def test_acl_least_privilege(env):
    assert env.client.get("broker-paper-key").get_secret_value() == env.secret
    planner = env.vault.as_client(env.vault.grant("planner-process"))          # no grants at all
    for call in (lambda: planner.get("broker-paper-key"), lambda: planner.sign("risk-signing-1", b"m"),
                 lambda: planner.public_key("risk-signing-1")):
        with pytest.raises(VaultDenied):
            call()
    with pytest.raises(VaultDenied):
        env.client.get("nope-secret")
    with pytest.raises(VaultDenied):
        env.client.sign("nope-key", b"m")


def test_bad_tokens_and_revoked_clients(env):
    for bad in ("", "risk-service:wrong", "ghost-client:abc", None, "no-colon", env.token + "x"):
        with pytest.raises(VaultDenied):
            env.vault.as_client(bad)
    env.vault.revoke_client("risk-service")
    with pytest.raises(VaultDenied):
        env.client.get("broker-paper-key")           # already-open handle is re-checked on every call
    with pytest.raises(VaultDenied):
        env.vault.as_client(env.token)


def test_secrets_provider_and_signer_adapters(env):
    provider = VaultSecretsProvider(env.client)
    assert provider.get("broker-paper-key").get_secret_value() == env.secret
    with pytest.raises(SecretUnavailable):
        provider.get("other-secret")
    signer = VaultSigner(env.client, "risk-signing-1")
    assert signer.key_id == "risk-signing-1" and signer.public_key() == env.pub
    Ed25519PublicKey.from_public_bytes(env.pub).verify(signer.sign(b"z"), b"z")


# ------------------------------------------------------------------ audit
def test_every_access_is_logged_without_values(env):
    env.client.get("broker-paper-key")
    env.client.sign("risk-signing-1", b"payload-1")
    with pytest.raises(VaultDenied):
        env.client.get("nope-secret")
    events = env.events()
    kinds = [(e.actor, e.action, e.outcome) for e in events]
    assert ("risk-service", "get_secret", "ALLOWED") in kinds and ("risk-service", "sign", "ALLOWED") in kinds
    assert ("risk-service", "get_secret", "DENIED") in kinds
    sign = next(e for e in events if e.action == "sign")
    assert len(sign.detail["message_sha256"]) == 64 and "payload-1" not in env.log.read_text(encoding="utf-8")
    assert ChainedLog.open(env.log, VaultEvent)[1].entries == len(events)


def test_access_is_refused_when_the_audit_log_cannot_be_written(env):
    env.vault._log.fail_writes = True
    with pytest.raises(VaultDenied, match="audit log"):
        env.client.get("broker-paper-key")
    with pytest.raises(VaultDenied, match="audit log"):
        env.client.sign("risk-signing-1", b"m")


# ------------------------------------------------------------------ no leaks
def test_no_secret_in_logging_repr_or_pickle(env, caplog):
    with caplog.at_level(logging.DEBUG):
        env.client.get("broker-paper-key")
        env.client.sign("risk-signing-1", b"m")
        text = " ".join([caplog.text, repr(env.vault), repr(env.client), str(env.client.get("broker-paper-key")),
                         repr(VaultSecretsProvider(env.client).get("broker-paper-key"))])
    for needle in (env.secret, env.pw, env.token.split(":", 1)[1]):
        assert needle not in text
    for obj in (env.vault, env.client):
        with pytest.raises(TypeError):
            pickle.dumps(obj)


def test_vault_is_not_importable_outside_composition_roots():
    import pathlib
    src = pathlib.Path(__file__).resolve().parents[2] / "src" / "trading_agent"
    offenders = [str(p.relative_to(src)) for p in src.rglob("*.py")
                 if "secret_store" not in p.parts and "secret_store.vault" in p.read_text(encoding="utf-8")]
    assert offenders == []


# ------------------------------------------------------------------ CLI
def test_cli_flow_and_refusals(tmp_path, capsys):
    import importlib.util
    import pathlib
    spec = importlib.util.spec_from_file_location("vault_cli", pathlib.Path(__file__).resolve().parents[2]
                                                  / "scripts" / "vault.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    pw, value = phrase(), "VAL-" + secrets.token_hex(10)
    answers = iter([pw, pw])
    out = []
    d = str(tmp_path / "home")
    assert cli.main(["--dir", d, "init"], prompt=lambda _: next(answers), out=out.append,
                    insecure_fast_kdf_for_tests=True) == 0
    for argv, extra in ((["put-secret", "alpaca-paper-key"], [pw, value]), (["create-key", "risk-signing-1"], [pw]),
                        (["grant", "risk-service", "--read", "alpaca-paper-key", "--sign", "risk-signing-1"], [pw]),
                        (["list"], [pw]), (["export-registry"], [pw])):
        it = iter(extra)
        assert cli.main(["--dir", d] + argv, prompt=lambda _: next(it), out=out.append) == 0
    printed = "\n".join(out)
    assert value not in printed and pw not in printed           # values are never echoed
    assert "risk-service:" in printed                            # the token is shown once, by design
    assert cli.main(["--dir", d, "verify-log"], out=out.append) == 0
    bad = iter(["wrong-pass-123456"])
    assert cli.main(["--dir", d, "list"], prompt=lambda _: next(bad), out=out.append) == 1
    assert cli.main(["--dir", str(pathlib.Path(__file__).resolve().parents[2] / "vault-here"), "list"],
                    out=out.append) == 2                         # inside the git work tree: refused


def test_vault_code_matches_its_owner_approval():
    """The approved vault is exactly this code: any edit voids APR-0004 until re-approved."""
    import pathlib
    from trading_agent.backtest.dataset import file_sha256
    from trading_agent.risk.approvals import load_approvals
    root = pathlib.Path(__file__).resolve().parents[2]
    apr = next(a for a in load_approvals(root / "config") if a.id == "APR-0004")
    assert [m.value for m in apr.modes] == ["BACKTEST"]
    for rel, digest in apr.files.items():
        assert file_sha256(root / rel) == digest, f"{rel} changed after approval"
