"""In-house vault: encrypted secrets + sign-only Ed25519 keys + per-client ACL + hash-chained access log.

Design and limits: docs/adr/0003-risk-service-key-separation.md ("Vault"). Not an HSM.

File (JSON): {format, kdf, body{generation, entries, clients}, check, mac}
  * scrypt(passphrase, salt) -> 64 bytes: 32 for AES-256-GCM, 32 for the HMAC over everything unencrypted;
  * each entry is AES-GCM encrypted with its name/kind/version as authenticated data;
  * ACLs and key metadata are plaintext but covered by the MAC (tampering is detected, not hidden).

Rules enforced here (tests/security/test_vault.py):
  * a signing key has no export operation; only sign() and public_key();
  * an access is allowed only after its audit record is appended (log failure => deny);
  * values, tokens and passphrases never appear in the log, in repr(), or in exceptions;
  * wrong passphrase, flipped bits, swapped entries or a rolled-back file refuse to open.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets as _stdlib_secrets
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from pydantic import Field, SecretStr, field_validator
from typing_extensions import Literal

from trading_agent.audit.chain import ChainedLog
from trading_agent.contracts.base import Contract, require_aware
from trading_agent.core.clock import Clock, SystemClock
from trading_agent.core.errors import SecretUnavailable, VaultDenied, VaultError
from trading_agent.core.hashing import canonical_json

FORMAT = 1
SCRYPT_MIN_N = 2 ** 15            # production floor (32 MiB at r=8); tests may lower it explicitly
SCRYPT_MAX_N = 2 ** 20
MIN_PASSPHRASE_CHARS = 12
MAX_MESSAGE_BYTES = 64 * 1024
MAX_SECRET_BYTES = 16 * 1024
NAME = r"^[a-z0-9][a-z0-9._\-]{2,63}$"

_NAME = re.compile(NAME)
_CHECK_PLAINTEXT = b"trading-agent-vault-ok"


class VaultEvent(Contract):
    at: datetime
    generation: int = Field(ge=0)
    actor: str = Field(max_length=64)
    action: str = Field(max_length=32)
    target: str = Field(max_length=64)
    outcome: Literal["ALLOWED", "DENIED", "FAILED"]
    reason: str = Field(default="", max_length=120)
    detail: Dict[str, str] = Field(default_factory=dict)  # hashes and ids only, never values

    _aware = field_validator("at")(require_aware)


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _unb64(s: str) -> bytes:
    return base64.b64decode(s.encode("ascii"), validate=True)


def _check_name(name: str, what: str = "name") -> str:
    if not isinstance(name, str) or not _NAME.match(name):
        raise VaultError(f"invalid {what}: must match {NAME}")
    return name


def _passphrase_bytes(passphrase: Union[str, SecretStr]) -> bytes:
    raw = passphrase.get_secret_value() if isinstance(passphrase, SecretStr) else passphrase
    if not isinstance(raw, str) or len(raw) < MIN_PASSPHRASE_CHARS:
        raise VaultError(f"passphrase must be at least {MIN_PASSPHRASE_CHARS} characters")
    return raw.encode("utf-8")


def _derive(passphrase: bytes, salt: bytes, n: int) -> Tuple[bytes, bytes]:
    key = Scrypt(salt=salt, length=64, n=n, r=8, p=1).derive(passphrase)
    return key[:32], key[32:]


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)  # a crash leaves the old or the new file, never half of one
    try:
        os.chmod(path, 0o600)  # no effect on Windows; the operator must protect the directory
    except OSError:
        pass


class Vault:
    """Unlocked vault. Admin operations are methods here; clients get a restricted VaultClient."""

    def __init__(self, path: Path, doc: Dict[str, Any], enc_key: bytes, mac_key: bytes,
                 log: Optional[ChainedLog], clock: Clock) -> None:
        self._path = path
        self._doc = doc
        self.__enc = enc_key
        self.__mac = mac_key
        self._log = log
        self._clock = clock

    # ------------------------------------------------------------------ lifecycle
    @classmethod
    def create(cls, path: Union[str, Path], passphrase: Union[str, SecretStr], log_path: Union[str, Path],
               clock: Optional[Clock] = None, scrypt_n: int = SCRYPT_MIN_N,
               insecure_fast_kdf_for_tests: bool = False) -> "Vault":
        path = Path(path)
        if path.exists():
            raise VaultError(f"{path} already exists; refusing to overwrite")
        if scrypt_n < SCRYPT_MIN_N and not insecure_fast_kdf_for_tests:
            raise VaultError(f"scrypt n must be >= {SCRYPT_MIN_N}")
        if not (2 ** 4 <= scrypt_n <= SCRYPT_MAX_N):
            raise VaultError("scrypt n out of range")
        salt = os.urandom(16)
        enc, mac = _derive(_passphrase_bytes(passphrase), salt, scrypt_n)
        nonce = os.urandom(12)
        doc = {"format": FORMAT, "kdf": {"name": "scrypt", "n": scrypt_n, "r": 8, "p": 1, "salt": _b64(salt)},
               "body": {"generation": 0, "entries": {}, "clients": {}},
               "check": {"nonce": _b64(nonce), "ct": _b64(AESGCM(enc).encrypt(nonce, _CHECK_PLAINTEXT, b"check"))}}
        log, _ = ChainedLog.open(log_path, VaultEvent)
        vault = cls(path, doc, enc, mac, log, clock or SystemClock())
        vault._event("admin", "create_vault", path.name, "ALLOWED")
        path.parent.mkdir(parents=True, exist_ok=True)
        vault._save()
        return vault

    @classmethod
    def open(cls, path: Union[str, Path], passphrase: Union[str, SecretStr], log_path: Union[str, Path],
             clock: Optional[Clock] = None) -> "Vault":
        path = Path(path)
        try:
            doc = json.loads(path.read_bytes().decode("utf-8"))
            kdf = doc["kdf"]
            if doc["format"] != FORMAT or kdf["name"] != "scrypt" or kdf["r"] != 8 or kdf["p"] != 1 \
                    or not (2 ** 4 <= kdf["n"] <= SCRYPT_MAX_N):
                raise VaultError("unsupported vault format or kdf parameters")
            enc, mac = _derive(_passphrase_bytes(passphrase), _unb64(kdf["salt"]), kdf["n"])
            expected = hmac.new(mac, canonical_json({k: doc[k] for k in ("format", "kdf", "body", "check")})
                                .encode("utf-8"), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, doc["mac"]):
                raise VaultError("wrong passphrase or vault file modified")
            AESGCM(enc).decrypt(_unb64(doc["check"]["nonce"]), _unb64(doc["check"]["ct"]), b"check")
        except VaultError:
            raise
        except (OSError, ValueError, KeyError, TypeError, InvalidTag):
            raise VaultError("cannot open vault: wrong passphrase, missing or corrupted file") from None
        log, _ = ChainedLog.open(log_path, VaultEvent)
        last_gen = max((e.generation for e in log.items), default=0)
        if doc["body"]["generation"] < last_gen:
            raise VaultError("vault file is older than its access log: rolled back or restored from a stale copy")
        vault = cls(path, doc, enc, mac, log, clock or SystemClock())
        vault._event("admin", "open_vault", path.name, "ALLOWED")
        return vault

    def __repr__(self) -> str:
        return f"Vault(path={self._path.name!r}, entries={len(self._doc['body']['entries'])}, <locked secrets>)"

    def __reduce__(self):
        raise TypeError("Vault cannot be serialised")

    # ------------------------------------------------------------------ admin operations
    def put_secret(self, name: str, value: Union[str, SecretStr]) -> int:
        _check_name(name, "secret name")
        raw = (value.get_secret_value() if isinstance(value, SecretStr) else value)
        data = raw.encode("utf-8") if isinstance(raw, str) else bytes(raw)
        if not data or len(data) > MAX_SECRET_BYTES:
            raise VaultError("secret must be 1..16384 bytes")
        entries = self._doc["body"]["entries"]
        prior = entries.get(name)
        if prior and prior["kind"] != "secret":
            raise VaultError(f"{name} exists and is not a secret")
        version = (prior["version"] + 1) if prior else 1
        self._event("admin", "put_secret", name, "ALLOWED", detail={"version": str(version)})
        entries[name] = self._seal(name, "secret", version, data, {})
        self._commit()
        return version

    def create_signing_key(self, key_id: str, not_before: datetime, not_after: datetime) -> bytes:
        """Generate an Ed25519 key inside the vault. Returns the PUBLIC key only."""
        _check_name(key_id, "key id")
        require_aware(not_before), require_aware(not_after)
        if not_after <= not_before:
            raise VaultError("not_after must be after not_before")
        entries = self._doc["body"]["entries"]
        if key_id in entries:
            raise VaultError(f"{key_id} already exists; use a new key id to rotate")
        private = Ed25519PrivateKey.generate()
        public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        raw = private.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                    serialization.NoEncryption())
        meta = {"public_key": public.hex(), "not_before": not_before.isoformat(), "not_after": not_after.isoformat(),
                "revoked": False}
        self._event("admin", "create_key", key_id, "ALLOWED", detail={"public_key_sha256": _sha(public)})
        entries[key_id] = self._seal(key_id, "signing_key", 1, raw, meta)
        self._commit()
        return public

    def revoke_key(self, key_id: str) -> None:
        entry = self._entry(key_id, "signing_key")
        self._event("admin", "revoke_key", key_id, "ALLOWED")
        entry["meta"]["revoked"] = True
        self._commit()

    def grant(self, client_id: str, read: Iterable[str] = (), sign: Iterable[str] = ()) -> str:
        """Create (or replace) a client. Returns its token; the token is shown once and stored only hashed."""
        _check_name(client_id, "client id")
        read, sign = sorted(set(read)), sorted(set(sign))
        for n in read + sign:
            _check_name(n)
        token = f"{client_id}:{_stdlib_secrets.token_urlsafe(32)}"
        self._event("admin", "grant", client_id, "ALLOWED", detail={"read": ",".join(read), "sign": ",".join(sign)})
        self._doc["body"]["clients"][client_id] = {"token_sha256": _sha(token.encode("utf-8")), "read": read,
                                                   "sign": sign, "revoked": False}
        self._commit()
        return token

    def revoke_client(self, client_id: str) -> None:
        client = self._doc["body"]["clients"].get(client_id)
        if client is None:
            raise VaultError(f"unknown client {client_id}")
        self._event("admin", "revoke_client", client_id, "ALLOWED")
        client["revoked"] = True
        self._commit()

    def key_registry(self) -> Dict[str, Dict[str, Any]]:
        """Public key registry for verifiers (execution service): key_id -> public key + validity + revoked."""
        return {n: {"public_key": e["meta"]["public_key"], "not_before": e["meta"]["not_before"],
                    "not_after": e["meta"]["not_after"], "revoked": e["meta"]["revoked"]}
                for n, e in sorted(self._doc["body"]["entries"].items()) if e["kind"] == "signing_key"}

    def inventory(self) -> List[Dict[str, Any]]:
        """Names and metadata only; never values."""
        return [{"name": n, "kind": e["kind"], "version": e["version"], **({"revoked": e["meta"]["revoked"]}
                                                                         if e["kind"] == "signing_key" else {})}
                for n, e in sorted(self._doc["body"]["entries"].items())]

    # ------------------------------------------------------------------ client access
    def as_client(self, token: str) -> "VaultClient":
        client_id = token.split(":", 1)[0] if isinstance(token, str) else "?"
        client = self._doc["body"]["clients"].get(client_id) if _NAME.match(client_id or "") else None
        if client is None or not hmac.compare_digest(client["token_sha256"], _sha(str(token).encode("utf-8"))):
            self._event("unknown", "authenticate", client_id[:64] if _NAME.match(client_id or "") else "?",
                        "DENIED", "BAD_TOKEN")
            raise VaultDenied("authentication failed")
        if client["revoked"]:
            self._event(client_id, "authenticate", client_id, "DENIED", "CLIENT_REVOKED")
            raise VaultDenied("client revoked")
        return VaultClient(self, client_id)

    def _client_get(self, client_id: str, name: str) -> SecretStr:
        client = self._live_client(client_id, "get_secret", name)
        entry = self._doc["body"]["entries"].get(name)
        if name not in client["read"]:
            self._deny(client_id, "get_secret", name, "NOT_ALLOWED")
        if entry is None:
            self._deny(client_id, "get_secret", name, "UNKNOWN_NAME")
        if entry["kind"] != "secret":
            self._deny(client_id, "get_secret", name, "WRONG_KIND")  # signing keys can never be read
        self._event(client_id, "get_secret", name, "ALLOWED", detail={"version": str(entry["version"])})
        return SecretStr(self._unseal(name, entry).decode("utf-8"))

    def _client_sign(self, client_id: str, key_id: str, message: bytes) -> bytes:
        client = self._live_client(client_id, "sign", key_id)
        entry = self._doc["body"]["entries"].get(key_id)
        if not isinstance(message, (bytes, bytearray)) or not (0 < len(message) <= MAX_MESSAGE_BYTES):
            self._deny(client_id, "sign", key_id, "BAD_MESSAGE")
        if key_id not in client["sign"]:
            self._deny(client_id, "sign", key_id, "NOT_ALLOWED")
        if entry is None or entry["kind"] != "signing_key":
            self._deny(client_id, "sign", key_id, "UNKNOWN_KEY")
        meta, now = entry["meta"], self._clock.now()
        if meta["revoked"]:
            self._deny(client_id, "sign", key_id, "KEY_REVOKED")
        if now < datetime.fromisoformat(meta["not_before"]):
            self._deny(client_id, "sign", key_id, "KEY_NOT_YET_VALID")
        if now >= datetime.fromisoformat(meta["not_after"]):
            self._deny(client_id, "sign", key_id, "KEY_EXPIRED")
        self._event(client_id, "sign", key_id, "ALLOWED", detail={"message_sha256": _sha(bytes(message))})
        private = Ed25519PrivateKey.from_private_bytes(self._unseal(key_id, entry))
        return private.sign(bytes(message))

    def _client_public_key(self, client_id: str, key_id: str) -> bytes:
        client = self._live_client(client_id, "public_key", key_id)
        entry = self._doc["body"]["entries"].get(key_id)
        if key_id not in client["sign"] or entry is None or entry["kind"] != "signing_key":
            self._deny(client_id, "public_key", key_id, "NOT_ALLOWED")
        self._event(client_id, "public_key", key_id, "ALLOWED")
        return bytes.fromhex(entry["meta"]["public_key"])

    # ------------------------------------------------------------------ internals
    def _live_client(self, client_id: str, action: str, target: str) -> Dict[str, Any]:
        client = self._doc["body"]["clients"].get(client_id)
        if client is None or client["revoked"]:
            self._deny(client_id, action, target, "CLIENT_REVOKED" if client else "UNKNOWN_CLIENT")
        return client

    def _deny(self, actor: str, action: str, target: str, reason: str):
        self._event(actor, action, target, "DENIED", reason)
        raise VaultDenied(f"{action} denied: {reason}")

    def _event(self, actor: str, action: str, target: str, outcome: str, reason: str = "",
               detail: Optional[Dict[str, str]] = None) -> None:
        try:
            self._log.append(VaultEvent(at=self._clock.now(), generation=self._doc["body"]["generation"], actor=actor,
                                        action=action, target=target[:64], outcome=outcome, reason=reason,
                                        detail=detail or {}))
        except Exception as exc:  # an unrecorded access is not allowed
            if outcome == "DENIED":
                return
            raise VaultDenied("access refused: audit log unavailable") from exc

    def _entry(self, name: str, kind: str) -> Dict[str, Any]:
        entry = self._doc["body"]["entries"].get(name)
        if entry is None or entry["kind"] != kind:
            raise VaultError(f"no {kind} named {name}")
        return entry

    def _seal(self, name: str, kind: str, version: int, plaintext: bytes, meta: Dict[str, Any]) -> Dict[str, Any]:
        nonce = os.urandom(12)
        ct = AESGCM(self.__enc).encrypt(nonce, plaintext, _aad(name, kind, version))
        return {"kind": kind, "version": version, "nonce": _b64(nonce), "ct": _b64(ct), "meta": meta}

    def _unseal(self, name: str, entry: Dict[str, Any]) -> bytes:
        try:
            return AESGCM(self.__enc).decrypt(_unb64(entry["nonce"]), _unb64(entry["ct"]),
                                              _aad(name, entry["kind"], entry["version"]))
        except (InvalidTag, ValueError, KeyError):
            raise VaultError(f"entry {name} failed authentication") from None

    def _commit(self) -> None:
        self._doc["body"]["generation"] += 1
        try:
            self._save()
        except OSError as exc:
            self._doc["body"]["generation"] -= 1
            self._event("admin", "commit", self._path.name, "FAILED", "WRITE_ERROR")
            raise VaultError("could not write the vault file") from exc
        # records the new generation, so restoring an older copy of the file is detectable at the next open
        self._event("admin", "commit", self._path.name, "ALLOWED")

    def _save(self) -> None:
        signed = {k: self._doc[k] for k in ("format", "kdf", "body", "check")}
        mac = hmac.new(self.__mac, canonical_json(signed).encode("utf-8"), hashlib.sha256).hexdigest()
        _atomic_write(self._path, (json.dumps({**signed, "mac": mac}, indent=1, sort_keys=True) + "\n").encode("utf-8"))


class VaultClient:
    """What a service gets: read the secrets it is allowed, or sign with the keys it is allowed. Nothing else."""

    def __init__(self, vault: Vault, client_id: str) -> None:
        self.__vault = vault
        self.client_id = client_id

    def get(self, name: str) -> SecretStr:
        return self.__vault._client_get(self.client_id, name)

    def sign(self, key_id: str, message: bytes) -> bytes:
        return self.__vault._client_sign(self.client_id, key_id, message)

    def public_key(self, key_id: str) -> bytes:
        return self.__vault._client_public_key(self.client_id, key_id)

    def __repr__(self) -> str:
        return f"VaultClient(client_id={self.client_id!r})"

    def __reduce__(self):
        raise TypeError("VaultClient cannot be serialised")


class VaultSecretsProvider:
    """SecretsProvider over a VaultClient (what the adapters expect). A denial is 'unavailable'."""

    def __init__(self, client: VaultClient) -> None:
        self._client = client

    def get(self, name: str) -> SecretStr:
        try:
            return self._client.get(name)
        except VaultError:
            raise SecretUnavailable(f"secret {name!r} unavailable") from None


class VaultSigner:
    """Signer over a VaultClient for one key id."""

    def __init__(self, client: VaultClient, key_id: str) -> None:
        self._client = client
        self.key_id = key_id

    def sign(self, message: bytes) -> bytes:
        return self._client.sign(self.key_id, message)

    def public_key(self) -> bytes:
        return self._client.public_key(self.key_id)


def _aad(name: str, kind: str, version: int) -> bytes:
    return f"{name}|{kind}|{version}".encode("utf-8")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
