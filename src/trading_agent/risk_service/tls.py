"""TLS 1.3 contexts for the risk-service link, both sides authenticated (mutual TLS).

Server: requires a client certificate from the configured CA; OpenSSL checks its EKU is clientAuth; the
service then answers only the identity "orchestrator" (service.py). Client: requires a server certificate
from the same CA with EKU serverAuth whose DNS name is "risk-service" (hostname check), whatever address
it connects to.
"""
import ssl
from pathlib import Path
from typing import Any, Dict, Optional, Union

PathLike = Union[str, Path]


def server_context(ca_file: PathLike, cert_file: PathLike, key_file: PathLike) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.load_verify_locations(cafile=str(ca_file))
    ctx.load_cert_chain(str(cert_file), str(key_file))
    return ctx


def client_context(ca_file: PathLike, cert_file: PathLike, key_file: PathLike) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)   # check_hostname=True, CERT_REQUIRED
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.load_verify_locations(cafile=str(ca_file))
    ctx.load_cert_chain(str(cert_file), str(key_file))
    return ctx


def peer_identity(cert: Optional[Dict[str, Any]]) -> str:
    """The subject common name of a verified peer certificate, or "" if there is none."""
    if not cert:
        return ""
    names = [value for rdn in cert.get("subject", ()) for key, value in rdn if key == "commonName"]
    return names[0] if len(names) == 1 else ""
