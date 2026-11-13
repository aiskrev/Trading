"""Development certificates for the risk-service link (no network code).

`generate_dev_pki(directory)` makes a throwaway certificate authority and two leaf certificates:

  ca.pem                                   trust anchor for both sides
  risk-service.pem / risk-service.key      server certificate: CN/SAN "risk-service", EKU serverAuth only
  orchestrator.pem / orchestrator.key      client certificate: CN/SAN "orchestrator", EKU clientAuth only

The CA private key exists only in memory while this function runs: it is never written, so nobody can issue
another certificate under this CA later. To rotate, generate a new directory and restart both processes.
Keys are ECDSA P-256, written owner-read-only where the OS supports it. The directory must be outside any
git work tree (keys never go into git, CLAUDE.md). For PAPER and above the certificates come from the
operator's own CA; that is part of the host decision (ADR 0003, open question).
"""
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Union

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from trading_agent.core.errors import ConfigError
from trading_agent.risk_service.protocol import ORCHESTRATOR_IDENTITY, SERVICE_IDENTITY

MAX_DAYS = 90


def inside_git_worktree(path: Union[str, Path]) -> bool:
    p = Path(path).resolve()
    return any((parent / ".git").exists() for parent in (p, *p.parents))


def generate_dev_pki(directory: Union[str, Path], days: int = 30) -> Dict[str, Path]:
    if not 0 < days <= MAX_DAYS:
        raise ConfigError(f"certificate lifetime must be 1..{MAX_DAYS} days")
    directory = Path(directory)
    if inside_git_worktree(directory):
        raise ConfigError("refusing to write keys inside a git work tree; choose a directory outside the repository")
    directory.mkdir(parents=True, exist_ok=True)
    if any((directory / name).exists() for name in _NAMES):
        raise ConfigError("the directory already holds certificates; use a new directory to rotate")

    now = datetime.now(timezone.utc) - timedelta(minutes=5)
    until = now + timedelta(days=days)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "trading-agent dev CA")])
    ca_cert = (x509.CertificateBuilder()
               .subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
               .serial_number(x509.random_serial_number()).not_valid_before(now).not_valid_after(until)
               .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
               .add_extension(x509.KeyUsage(digital_signature=False, content_commitment=False,
                                            key_encipherment=False, data_encipherment=False, key_agreement=False,
                                            key_cert_sign=True, crl_sign=True, encipher_only=False,
                                            decipher_only=False), critical=True)
               .sign(ca_key, hashes.SHA256()))
    out = {"ca": directory / "ca.pem"}
    _write(out["ca"], ca_cert.public_bytes(serialization.Encoding.PEM), private=False)
    for identity, usage in ((SERVICE_IDENTITY, ExtendedKeyUsageOID.SERVER_AUTH),
                            (ORCHESTRATOR_IDENTITY, ExtendedKeyUsageOID.CLIENT_AUTH)):
        cert, key = issue_leaf(ca_cert, ca_key, identity, usage, now, until)
        out[f"{identity}.pem"] = directory / f"{identity}.pem"
        out[f"{identity}.key"] = directory / f"{identity}.key"
        _write(out[f"{identity}.pem"], cert, private=False)
        _write(out[f"{identity}.key"], key, private=True)
    return out


def issue_leaf(ca_cert, ca_key, identity: str, usage, not_before: datetime, not_after: datetime):
    """One leaf certificate and key as PEM bytes. Also used by tests to make deliberately wrong certificates."""
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, identity)]))
            .issuer_name(ca_cert.subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(not_before).not_valid_after(not_after)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=False,
                                         data_encipherment=False, key_agreement=False, key_cert_sign=False,
                                         crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([usage]), critical=False)
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(identity)]), critical=False)
            .sign(ca_key, hashes.SHA256()))
    key_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
    return cert.public_bytes(serialization.Encoding.PEM), key_pem


_NAMES = ("ca.pem", f"{SERVICE_IDENTITY}.pem", f"{SERVICE_IDENTITY}.key", f"{ORCHESTRATOR_IDENTITY}.pem",
          f"{ORCHESTRATOR_IDENTITY}.key")


def _write(path: Path, data: bytes, private: bool) -> None:
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                 0o600 if private else 0o644)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
