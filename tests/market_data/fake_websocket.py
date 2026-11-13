"""A scripted WebSocket server on one end of a socket pair, for tests of the WebSocket client and the Alpaca
stream transport. No network: the client gets the other end of the pair through its socket factory.

It speaks just enough of the Alpaca stream protocol: "connected" on open, "authenticated" (or an error) on
auth, "subscription" on subscribe, pong on ping, plus whatever frames a test pushes.
"""
import base64
import hashlib
import json
import socket
import struct
import threading
import time
from typing import Callable, List, Optional

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def server_frame(opcode: int, payload: bytes, fin: bool = True, masked: bool = False, rsv: int = 0) -> bytes:
    first = (0x80 if fin else 0) | rsv | opcode
    n = len(payload)
    mask_bit = 0x80 if masked else 0
    if n < 126:
        header = struct.pack("!BB", first, mask_bit | n)
    elif n < 65536:
        header = struct.pack("!BBH", first, mask_bit | 126, n)
    else:
        header = struct.pack("!BBQ", first, mask_bit | 127, n)
    if masked:
        mask = b"\x01\x02\x03\x04"
        return header + mask + bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
    return header + payload


class FakeAlpacaServer:
    def __init__(self, key: str = "", secret: str = "", auth_error: Optional[int] = None,
                 connect_error: Optional[int] = None, bad_accept: bool = False,
                 confirm: Optional[Callable[[List[str]], List[str]]] = None) -> None:
        self.client_sock, self._sock = socket.socketpair()
        self.key, self.secret = key, secret
        self.auth_error, self.connect_error, self.bad_accept = auth_error, connect_error, bad_accept
        self.confirm = confirm or (lambda symbols: symbols)
        self.received: List[str] = []       # text messages from the client
        self.pings = 0
        self._buf = b""
        self._stop = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def factory(self, host: str, port: int, timeout: float) -> socket.socket:
        self.host = host
        return self.client_sock

    def push_text(self, obj) -> None:
        self.push_raw(server_frame(0x1, (obj if isinstance(obj, str) else json.dumps(obj)).encode("utf-8")))

    def push_raw(self, data: bytes) -> None:
        self._sock.sendall(data)

    def close(self) -> None:
        self._stop = True
        try:
            self._sock.close()
        except OSError:
            pass

    # ------------------------------------------------------------------ server loop
    def _run(self) -> None:
        try:
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = self._sock.recv(4096)
                if not chunk:
                    return
                head += chunk
            key = [l.split(":", 1)[1].strip() for l in head.decode().split("\r\n") if l.lower().startswith(
                "sec-websocket-key")][0]
            accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
            if self.bad_accept:
                accept = "x" + accept[1:]
            self._sock.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                                f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
            if self.connect_error:
                self.push_text([{"T": "error", "code": self.connect_error, "msg": "x"}])
                return
            self.push_text([{"T": "success", "msg": "connected"}])
            while not self._stop:
                frame = self._read_frame()
                if frame is None:
                    return
                opcode, payload = frame
                if opcode == 0x9:
                    self.pings += 1
                    self.push_raw(server_frame(0xA, payload))
                elif opcode == 0x8:
                    return
                elif opcode == 0x1:
                    self._on_text(payload.decode("utf-8"))
        except OSError:
            return

    def _on_text(self, text: str) -> None:
        self.received.append(text)
        msg = json.loads(text)
        if msg.get("action") == "auth":
            if self.auth_error or (msg.get("key"), msg.get("secret")) != (self.key, self.secret):
                self.push_text([{"T": "error", "code": self.auth_error or 402, "msg": "auth failed"}])
            else:
                self.push_text([{"T": "success", "msg": "authenticated"}])
        elif msg.get("action") == "subscribe":
            self.push_text([{"T": "subscription", "trades": msg.get("trades", []),
                             "quotes": self.confirm(msg.get("quotes", [])), "bars": []}])

    def _read_frame(self):
        while True:
            if len(self._buf) >= 2:
                b1, b2 = self._buf[0], self._buf[1]
                n, off = b2 & 0x7F, 2
                if n == 126:
                    n, off = struct.unpack("!H", self._buf[2:4])[0], 4
                elif n == 127:
                    n, off = struct.unpack("!Q", self._buf[2:10])[0], 10
                if not b2 & 0x80:
                    raise OSError("client frame not masked")
                if len(self._buf) >= off + 4 + n:
                    mask = self._buf[off:off + 4]
                    data = self._buf[off + 4:off + 4 + n]
                    self._buf = self._buf[off + 4 + n:]
                    return b1 & 0x0F, bytes(c ^ mask[i % 4] for i, c in enumerate(data))
            chunk = self._sock.recv(65536)
            if not chunk:
                return None
            self._buf += chunk


def wait_until(predicate, timeout_s: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()
