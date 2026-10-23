"""The control room HTTP server: standard library only, loopback only.

Safety rules (each has a test in tests/ui/test_ui_security.py):
  * binds 127.0.0.1 and refuses any other address;
  * every request must carry a Host header naming 127.0.0.1 or localhost with the server's own port (DNS rebinding);
  * a POST must also carry the per-start token in `X-UI-Token`, a matching Origin if one is sent, and a small JSON body;
  * routes are an explicit table; the only POST starts one job from a fixed list (ui/jobs.py);
  * nothing here can change the mode, a config file, an approval, a prompt, the kill switch or a secret, and nothing
    here submits an order or calls the model;
  * strict Content-Security-Policy, no external assets, no cookies, no CORS headers.
"""
import argparse
import json
import re
import secrets
import sys
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT / "src", ROOT / ".deps", ROOT):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from ui import data  # noqa: E402
from ui.jobs import CHECK_KEYS, LABELS, JobRunner  # noqa: E402

STATIC = Path(__file__).resolve().parent / "static"
STATIC_TYPES = {"index.html": "text/html; charset=utf-8", "app.js": "text/javascript; charset=utf-8",
                "style.css": "text/css; charset=utf-8", "favicon.svg": "image/svg+xml"}
LOOPBACK = {"127.0.0.1", "localhost"}
MAX_BODY = 2048
MAX_DRAIN = 65536
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


class ControlRoom(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, ws: data.Workspace, host: str = "127.0.0.1", port: int = 8765) -> None:
        if host not in LOOPBACK:
            raise ValueError("the control room only listens on 127.0.0.1")
        super().__init__(("127.0.0.1", port), Handler)
        self.ws = ws
        self.token = secrets.token_urlsafe(24)
        self.jobs = JobRunner(ws)

    @property
    def port(self) -> int:
        return self.server_address[1]


class Handler(BaseHTTPRequestHandler):
    server: ControlRoom
    server_version = "ControlRoom"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------ plumbing
    def log_message(self, fmt: str, *args: Any) -> None:       # quiet: never echo query strings or headers
        pass

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("X-Frame-Options", "DENY")
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: Any, status: int = 200) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=True, default=str).encode("utf-8"), "application/json")

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message}, status)

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").lower()
        return host in {f"127.0.0.1:{self.server.port}", f"localhost:{self.server.port}"}

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        return origin is None or origin in {f"http://127.0.0.1:{self.server.port}", f"http://localhost:{self.server.port}"}

    # ------------------------------------------------------------ routing
    def do_GET(self) -> None:
        if not self._host_ok():
            return self._error(HTTPStatus.FORBIDDEN, "host not allowed")
        url = urlsplit(self.path)
        path, query = unquote(url.path), {k: v[0] for k, v in parse_qs(url.query).items()}
        if path == "/" or path == "/index.html":
            return self._static("index.html")
        if path.startswith("/static/"):
            return self._static(path[len("/static/"):])
        if not path.startswith("/api/"):
            return self._error(HTTPStatus.NOT_FOUND, "not found")
        try:
            result = self._api(path[len("/api/"):].strip("/").split("/"), query)
        except data.NotFound:
            return self._error(HTTPStatus.NOT_FOUND, "not found")
        except (ValueError, KeyError):
            return self._error(HTTPStatus.BAD_REQUEST, "bad request")
        except Exception as exc:                                  # never leak a stack trace or a path
            return self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"internal error ({type(exc).__name__})")
        if result is None:
            return self._error(HTTPStatus.NOT_FOUND, "not found")
        self._json(result)

    def do_POST(self) -> None:
        self.close_connection = True              # never reuse the connection after a POST
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = -1
        raw = self.rfile.read(min(length, MAX_DRAIN)) if length > 0 else b""   # read it first: an unread body resets the socket
        if not self._host_ok() or not self._origin_ok():
            return self._error(HTTPStatus.FORBIDDEN, "host or origin not allowed")
        if self.headers.get("X-UI-Token") != self.server.token:
            return self._error(HTTPStatus.FORBIDDEN, "missing or wrong token")
        if urlsplit(self.path).path != "/api/jobs":
            return self._error(HTTPStatus.NOT_FOUND, "not found")
        if (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/json":
            return self._error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "json only")
        if length < 0:
            return self._error(HTTPStatus.BAD_REQUEST, "bad length")
        if not 0 < length <= MAX_BODY:
            return self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "body too large")
        try:
            body = json.loads(raw.decode("utf-8"))
            key, dataset = str(body["key"]), body.get("dataset")
            if dataset is not None and not isinstance(dataset, str):
                raise ValueError("dataset")
            job = self.server.jobs.start(key, dataset)
        except RuntimeError:
            return self._error(HTTPStatus.CONFLICT, "another job is running")
        except (ValueError, KeyError, TypeError, UnicodeDecodeError):
            return self._error(HTTPStatus.BAD_REQUEST, "bad request")
        self._json(job.view(), HTTPStatus.ACCEPTED)

    def _static(self, name: str) -> None:
        if name not in STATIC_TYPES or not (STATIC / name).is_file():
            return self._error(HTTPStatus.NOT_FOUND, "not found")
        self._send(200, (STATIC / name).read_bytes(), STATIC_TYPES[name])

    # ------------------------------------------------------------ API table (GET only)
    def _api(self, parts: List[str], q: Dict[str, str]) -> Any:
        ws, s = self.server.ws, self.server
        head, rest = parts[0], parts[1:]
        if head == "session" and not rest:
            return {"token": s.token, "port": s.port, "mode_note": "read-only control room",
                    "jobs": [{"key": k, "label": LABELS[k]} for k in LABELS]}
        if head == "overview" and not rest:
            return data.overview(ws)
        if head == "approvals" and not rest:
            return data.approvals(ws)
        if head == "policy" and not rest:
            return data.policy(ws)
        if head == "docs" and not rest:
            return data.docs_index(ws)
        if head == "doc" and not rest:
            return data.doc(ws, q.get("path", ""))
        if head == "runs":
            if not rest:
                return data.runs(ws)
            if len(rest) == 1:
                return data.run_detail(ws, rest[0])
            if rest[1] == "packets" and len(rest) == 2:
                return data.packets(ws, rest[0], q.get("outcome", ""), int(q.get("limit", "500")), int(q.get("offset", "0")))
            if rest[1] == "packets" and len(rest) == 3:
                return data.packet(ws, rest[0], int(rest[2]))
        if head == "datasets" and not rest:
            return data.datasets(ws)
        if head == "backtests":
            if not rest:
                return data.backtests(ws)
            if len(rest) == 2:
                return data.backtest(ws, rest[0], rest[1])
        if head == "model" and not rest:
            return data.model_info(ws)
        if head == "evals":
            if not rest:
                return data.eval_reports(ws)
            if rest == ["report"]:
                return data.eval_report(ws, q.get("id", ""))
        if head == "scenarios":
            if not rest:
                return data.scenarios(ws, q.get("split", ""), q.get("kind", ""))
            if len(rest) == 2:
                return data.scenario(ws, rest[0], rest[1])
        if head == "checks" and not rest:
            cache = data.read_json(ws.runs / "ui" / "last_checks.json") or {}
            return [{"key": k, "label": LABELS[k], "last": cache.get(k)} for k in CHECK_KEYS]
        if head == "jobs":
            if not rest:
                return s.jobs.recent()
            job = s.jobs.get(rest[0])
            return job.view() if job else None
        return None


def make_server(root: Path = ROOT, runs_dir: Optional[Path] = None, host: str = "127.0.0.1", port: int = 8765) -> ControlRoom:
    return ControlRoom(data.Workspace(root, runs_dir), host, port)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Local control room for the trading agent (read-mostly, 127.0.0.1 only).")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--root", default=str(ROOT), help="project folder to show (default: this one)")
    ap.add_argument("--runs-dir", default=None, help="folder with live-loop run directories (default: <root>/runs)")
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args(argv)
    try:
        server = make_server(Path(a.root), Path(a.runs_dir) if a.runs_dir else None, port=a.port)
    except OSError as exc:
        print(f"cannot listen on 127.0.0.1:{a.port}: {exc}. Is the control room already running? Try --port {a.port + 1}")
        return 1
    url = f"http://127.0.0.1:{server.port}/"
    print(f"Control room: {url}   (Ctrl+C to stop; reads {server.ws.root})")
    if not a.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("stopped")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
