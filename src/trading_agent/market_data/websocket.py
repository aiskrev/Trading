"""Minimal RFC 6455 WebSocket client for read-only market-data streams (text frames only).

Why not a library: one small, reviewed module instead of a new dependency. What it does:
  * TLS with certificate and hostname verification (ssl.create_default_context), host from an allow-list
    passed by the vendor transport; no proxies, no redirects;
  * opening handshake with Sec-WebSocket-Accept check;
  * client frames masked (as the RFC requires); server frames must be unmasked;
  * text messages, fragmentation, ping -> pong, close; binary frames are refused;
  * non-blocking `poll()` so the adapter's clock-driven loop never stalls;
  * hard size limits on frames and on the message buffer.

Every failure is a TransportError whose message carries no payload or credential.
"""
import base64
import hashlib
import os
import select
import socket
import ssl
import struct
from typing import Callable, List, Optional

from trading_agent.core.errors import TransportError

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_MESSAGE_BYTES = 4 * 1024 * 1024
MAX_HANDSHAKE_BYTES = 16 * 1024
OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA

SocketFactory = Callable[[str, int, float], socket.socket]


def tls_socket(host: str, port: int, timeout: float) -> socket.socket:
    """A TLS socket with the system trust store, hostname check and TLS >= 1.2."""
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    raw = socket.create_connection((host, port), timeout=timeout)
    try:
        return ctx.wrap_socket(raw, server_hostname=host)
    except BaseException:
        raw.close()
        raise


class WebSocketClient:
    def __init__(self, host: str, path: str, port: int = 443, timeout_s: float = 10.0,
                 socket_factory: SocketFactory = tls_socket, extra_headers: Optional[dict] = None) -> None:
        self._host, self._path, self._port, self._timeout = host, path, port, timeout_s
        self._factory = socket_factory
        self._extra = dict(extra_headers or {})
        self._sock: Optional[socket.socket] = None
        self._buf = b""
        self._fragments: List[bytes] = []
        self._pending: List[str] = []
        self.pongs = 0

    # ------------------------------------------------------------------ lifecycle
    def connect(self) -> None:
        try:
            self._sock = self._factory(self._host, self._port, self._timeout)
            self._sock.settimeout(self._timeout)
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            lines = [f"GET {self._path} HTTP/1.1", f"Host: {self._host}", "Upgrade: websocket",
                     "Connection: Upgrade", f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13"]
            lines += [f"{k}: {v}" for k, v in self._extra.items()]
            self._sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("ascii"))
            head = self._read_handshake()
        except TransportError:
            self.close()
            raise
        except (OSError, ValueError) as exc:
            self.close()
            raise TransportError(f"websocket connect failed: {type(exc).__name__}") from None
        status, headers = _parse_handshake(head)
        expected = base64.b64encode(hashlib.sha1((key + GUID).encode("ascii")).digest()).decode("ascii")
        if status != 101 or headers.get("upgrade", "").lower() != "websocket" \
                or headers.get("sec-websocket-accept") != expected:
            self.close()
            raise TransportError(f"websocket handshake refused (HTTP {status})")

    def close(self) -> None:
        sock, self._sock = self._sock, None
        self._buf, self._fragments, self._pending = b"", [], []
        if sock is None:
            return
        try:
            sock.settimeout(1.0)
            sock.sendall(_frame(OP_CLOSE, struct.pack("!H", 1000)))
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

    @property
    def connected(self) -> bool:
        return self._sock is not None

    # ------------------------------------------------------------------ I/O
    def send_text(self, text: str) -> None:
        self._send(_frame(OP_TEXT, text.encode("utf-8")))

    def ping(self) -> None:
        self._send(_frame(OP_PING, os.urandom(4)))

    def recv_blocking(self, timeout_s: float) -> str:
        """Next text message, waiting at most `timeout_s` (used during connect/auth/subscribe)."""
        messages = self.poll(wait_s=timeout_s)
        if not messages:
            raise TransportError("websocket: no answer in time")
        if len(messages) > 1:
            self._pending = messages[1:] + self._pending
        return messages[0]

    def poll(self, wait_s: float = 0.0) -> List[str]:
        """Complete text messages available now (waits up to `wait_s` for the first byte)."""
        if self._pending:
            pending, self._pending = self._pending, []
            return pending
        if self._sock is None:
            raise TransportError("websocket not connected")
        out: List[str] = []
        self._read_available(wait_s)
        while True:
            frame = self._next_frame()
            if frame is None:
                break
            fin, opcode, payload = frame
            if opcode == OP_PING:
                self._send(_frame(OP_PONG, payload))
            elif opcode == OP_PONG:
                self.pongs += 1
            elif opcode == OP_CLOSE:
                self.close()
                raise TransportError("websocket closed by server")
            elif opcode == OP_BINARY:
                self.close()
                raise TransportError("websocket: binary frames are not accepted")
            elif opcode in (OP_TEXT, OP_CONT):
                if opcode == OP_TEXT and self._fragments:
                    self.close()
                    raise TransportError("websocket: new message inside a fragmented one")
                if opcode == OP_CONT and not self._fragments:
                    self.close()
                    raise TransportError("websocket: continuation without a start")
                self._fragments.append(payload)
                if sum(len(f) for f in self._fragments) > MAX_MESSAGE_BYTES:
                    self.close()
                    raise TransportError("websocket: message too large")
                if fin:
                    data, self._fragments = b"".join(self._fragments), []
                    try:
                        out.append(data.decode("utf-8"))
                    except UnicodeDecodeError:
                        self.close()
                        raise TransportError("websocket: text frame is not utf-8") from None
            else:
                self.close()
                raise TransportError("websocket: unknown opcode")
        return out

    # ------------------------------------------------------------------ internals
    def _send(self, data: bytes) -> None:
        if self._sock is None:
            raise TransportError("websocket not connected")
        try:
            self._sock.settimeout(self._timeout)
            self._sock.sendall(data)
        except OSError as exc:
            self.close()
            raise TransportError(f"websocket send failed: {type(exc).__name__}") from None

    def _read_available(self, wait_s: float) -> None:
        sock = self._sock
        try:
            if not (isinstance(sock, ssl.SSLSocket) and sock.pending()):
                ready, _, _ = select.select([sock], [], [], max(wait_s, 0.0))
                if not ready:
                    return
            sock.settimeout(self._timeout)
            chunk = sock.recv(65536)
            while isinstance(sock, ssl.SSLSocket) and sock.pending():
                chunk += sock.recv(sock.pending())
        except (OSError, ValueError) as exc:
            self.close()
            raise TransportError(f"websocket read failed: {type(exc).__name__}") from None
        if not chunk:
            self.close()
            raise TransportError("websocket: connection closed")
        self._buf += chunk
        if len(self._buf) > MAX_MESSAGE_BYTES + 16:
            self.close()
            raise TransportError("websocket: receive buffer overflow")

    def _next_frame(self):
        b = self._buf
        if len(b) < 2:
            return None
        fin, opcode = b[0] & 0x80, b[0] & 0x0F
        if b[0] & 0x70:
            self.close()
            raise TransportError("websocket: reserved bits set (extensions are not negotiated)")
        if b[1] & 0x80:
            self.close()
            raise TransportError("websocket: server frames must not be masked")
        length, offset = b[1] & 0x7F, 2
        if length == 126:
            if len(b) < 4:
                return None
            length, offset = struct.unpack("!H", b[2:4])[0], 4
        elif length == 127:
            if len(b) < 10:
                return None
            length, offset = struct.unpack("!Q", b[2:10])[0], 10
        if length > MAX_MESSAGE_BYTES:
            self.close()
            raise TransportError("websocket: frame too large")
        if opcode >= 0x8 and (length > 125 or not fin):
            self.close()
            raise TransportError("websocket: invalid control frame")
        if len(b) < offset + length:
            return None
        payload, self._buf = b[offset:offset + length], b[offset + length:]
        return bool(fin), opcode, payload

    def _read_handshake(self) -> bytes:
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise TransportError("websocket handshake: connection closed")
            data += chunk
            if len(data) > MAX_HANDSHAKE_BYTES:
                raise TransportError("websocket handshake too large")
        head, _, rest = data.partition(b"\r\n\r\n")
        self._buf = rest
        return head


def _frame(opcode: int, payload: bytes) -> bytes:
    mask = os.urandom(4)
    n = len(payload)
    if n < 126:
        header = struct.pack("!BB", 0x80 | opcode, 0x80 | n)
    elif n < 65536:
        header = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, n)
    else:
        header = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, n)
    masked = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
    return header + mask + masked


def _parse_handshake(head: bytes):
    try:
        lines = head.decode("latin-1").split("\r\n")
        status = int(lines[0].split(" ", 2)[1])
    except (IndexError, ValueError):
        raise TransportError("websocket handshake: bad status line") from None
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    return status, headers
