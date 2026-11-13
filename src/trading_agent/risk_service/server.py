"""HTTPS listener for the risk service: loopback only, mutual TLS 1.3, one request at a time.

* binds only to a loopback address (the host decision for a networked deployment is still open, ADR 0003);
* the TLS handshake runs on the accepted socket under a timeout, so a stalled peer cannot block the service;
  a failed handshake (no certificate, another CA, wrong key usage) is logged as a refusal;
* requests are served one at a time (the gateway keeps rate-limit state);
* only `GET /v1/info` and `POST /v1/evaluate`; `Content-Type: application/json` and a `Content-Length` of at
  most MAX_BODY_BYTES; no other route, no CORS, no cookies, no stack traces, no server version banner.
"""
import socket
import ssl
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional, Tuple

from trading_agent.core.errors import ConfigError
from trading_agent.risk_service.protocol import EVALUATE_PATH, INFO_PATH, LOOPBACK_HOSTS, MAX_BODY_BYTES
from trading_agent.risk_service.service import RiskService
from trading_agent.risk_service.tls import peer_identity

SOCKET_TIMEOUT_S = 5.0


class _Handler(BaseHTTPRequestHandler):
    server_version = "risk-service"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 (http.server naming)
        if self.path != INFO_PATH:
            return self._send(*self.server.service.refuse("get", self._peer(), 404, "NOT_FOUND"))
        self._send(*self.server.service.handle_info(self._peer()))

    def do_POST(self) -> None:  # noqa: N802
        service: RiskService = self.server.service
        peer = self._peer()
        if self.path != EVALUATE_PATH:
            return self._send(*service.refuse("post", peer, 404, "NOT_FOUND"))
        if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
            return self._send(*service.refuse("evaluate", peer, 415, "UNSUPPORTED_MEDIA_TYPE"))
        try:
            length = int(self.headers.get("Content-Length") or "-1")
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY_BYTES:
            return self._send(*service.refuse("evaluate", peer, 413, "BODY_TOO_LARGE"))
        body = self.rfile.read(length)
        if len(body) != length:
            return self._send(*service.refuse("evaluate", peer, 400, "BAD_JSON"))
        self._send(*service.handle_evaluate(body, peer))

    def _peer(self) -> str:
        try:
            return peer_identity(self.connection.getpeercert())
        except (AttributeError, ValueError):
            return ""

    def _send(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def log_message(self, format, *args) -> None:  # the hash-chained request log is the record
        pass


class _TLSServer(HTTPServer):
    allow_reuse_address = False

    def __init__(self, address, service: RiskService, context: ssl.SSLContext) -> None:
        self.service = service
        self.context = context
        family = socket.AF_INET6 if ":" in address[0] else socket.AF_INET
        self.address_family = family
        super().__init__(address, _Handler)

    def get_request(self):
        raw, addr = self.socket.accept()
        raw.settimeout(SOCKET_TIMEOUT_S)
        try:
            tls = self.context.wrap_socket(raw, server_side=True)
        except (ssl.SSLError, OSError):
            raw.close()
            self.service.refuse("tls", "", 401, "TLS_HANDSHAKE_FAILED")
            raise
        return tls, addr

    def handle_error(self, request, client_address) -> None:  # no tracebacks on stderr
        pass


class RiskServiceServer:
    def __init__(self, service: RiskService, context: ssl.SSLContext, host: str = "127.0.0.1",
                 port: int = 0) -> None:
        if host not in LOOPBACK_HOSTS:
            raise ConfigError(f"the risk service listens on loopback only, not {host!r}")
        bind = "127.0.0.1" if host == "localhost" else host
        self._httpd = _TLSServer((bind, port), service, context)
        self._thread: Optional[threading.Thread] = None

    @property
    def address(self) -> Tuple[str, int]:
        host, port = self._httpd.server_address[:2]
        return host, port

    def serve_forever(self) -> None:
        self._httpd.serve_forever(poll_interval=0.2)

    def start(self) -> "RiskServiceServer":
        if self._thread is not None:   # one serving thread only (the gateway is not shared between threads)
            return self
        self._thread = threading.Thread(target=self.serve_forever, name="risk-service", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def __enter__(self) -> "RiskServiceServer":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()
