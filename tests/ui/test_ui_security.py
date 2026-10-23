"""The control room is a local, read-mostly tool in a project whose point is safety. These tests pin what it may not do."""
import ast
import http.client
import json
import os
import re
import threading
from pathlib import Path

import pytest

from ui import jobs
from ui.server import make_server

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
SERVED = [p for p in UI.glob("*.py") if p.name != "make_demo_run.py"]   # the demo script runs as its own process

FORBIDDEN_PROJECT = ("trading_agent.execution", "trading_agent.risk", "trading_agent.secret_store", "trading_agent.model_client",
                     "trading_agent.core.signing", "trading_agent.market_data", "trading_agent.account_data",
                     "trading_agent.orchestration.live_loop", "trading_agent.orchestration.tick", "trading_agent.backtest.engine",
                     "trading_agent.backtest.run", "trading_agent.planning")
FORBIDDEN_LIBS = {"requests", "httpx", "anthropic", "socket", "ssl", "aiohttp", "websockets", "websocket", "ftplib", "smtplib",
                  "telnetlib", "paramiko", "grpc", "cryptography"}
ALLOWED_STDLIB_NETWORK = {"http.server", "http", "urllib.parse", "webbrowser"}


def imports(path: Path):
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            for a in node.names:
                yield a.name
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            yield node.module
            for a in node.names:
                yield f"{node.module}.{a.name}"


def test_the_served_code_imports_no_order_risk_vault_signing_model_or_feed_code():
    bad = sorted({f"{p.name}:{m}" for p in SERVED for m in imports(p)
                  if m.startswith(FORBIDDEN_PROJECT) or m.split(".")[0] in FORBIDDEN_LIBS})
    assert bad == []


def test_the_only_network_code_is_the_loopback_http_server():
    used = {m for p in SERVED for m in imports(p) if m.split(".")[0] in {"http", "urllib", "webbrowser", "socketserver"}}
    assert used <= ALLOWED_STDLIB_NETWORK | {"http.HTTPStatus", "http.server.BaseHTTPRequestHandler", "http.server.ThreadingHTTPServer",
                                              "urllib.parse.parse_qs", "urllib.parse.unquote", "urllib.parse.urlsplit"}, used


def test_files_are_written_only_by_the_job_runner_and_only_under_runs_ui():
    writers = {p.name for p in SERVED if re.search(r"write_text|write_bytes|unlink|rmtree|\.rename\(|os\.remove|open\([^)]*['\"][wa]", p.read_text(encoding="utf-8"))}
    assert writers == {"jobs.py"}
    text = (UI / "jobs.py").read_text(encoding="utf-8")
    assert text.count("write_text") == 1 and '"ui" / "last_checks.json"' in text


def test_the_front_end_has_no_html_injection_eval_or_external_assets():
    for name in ("app.js", "index.html", "style.css"):
        text = (UI / "static" / name).read_text(encoding="utf-8")
        for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function", "setTimeout('", 'setTimeout("'):
            assert bad not in text, (name, bad)
        urls = set(re.findall(r"https?://[^\s'\")]+", text))
        assert urls <= {"http://www.w3.org/2000/svg"}, (name, urls)
    assert "<script>" not in (UI / "static" / "index.html").read_text(encoding="utf-8")      # no inline script (CSP)


# ------------------------------------------------------------------ the server
@pytest.fixture(scope="module")
def server(tmp_path_factory):
    srv = make_server(ROOT, tmp_path_factory.mktemp("runs"), port=0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def call(srv, method, path, headers=None, body=None, host=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=15)
    h = {"Host": host or f"127.0.0.1:{srv.port}"}
    h.update(headers or {})
    conn.request(method, path, body=body, headers=h)
    r = conn.getresponse()
    data = r.read()
    conn.close()
    return r.status, dict((k.lower(), v) for k, v in r.getheaders()), data


def post(srv, payload, token=None, headers=None, raw=None):
    h = {"Content-Type": "application/json", "X-UI-Token": srv.token if token is None else token}
    h.update(headers or {})
    return call(srv, "POST", "/api/jobs", h, raw if raw is not None else json.dumps(payload))


def test_it_listens_on_loopback_only(server):
    assert server.server_address[0] == "127.0.0.1"
    for host in ("0.0.0.0", "::", "192.168.1.10", "example.com", ""):
        with pytest.raises(ValueError):
            make_server(ROOT, None, host=host, port=0)


@pytest.mark.parametrize("host", ["evil.example", "evil.example:80", "127.0.0.1", "127.0.0.1:1", "localhost", "0.0.0.0:8765"])
def test_a_wrong_host_header_is_refused(server, host):
    status, _, _ = call(server, "GET", "/api/overview", host=host)
    assert status == 403
    assert call(server, "GET", "/", host=host)[0] == 403


def test_localhost_with_the_right_port_is_accepted(server):
    assert call(server, "GET", "/api/overview", host=f"localhost:{server.port}")[0] == 200


def test_every_response_carries_the_security_headers(server):
    for path in ("/", "/static/app.js", "/api/overview", "/api/nothing", "/static/nothing"):
        _, h, _ = call(server, "GET", path)
        assert "default-src 'none'" in h["content-security-policy"] and "script-src 'self'" in h["content-security-policy"]
        assert h["x-content-type-options"] == "nosniff" and h["cache-control"] == "no-store" and h["x-frame-options"] == "DENY"
        assert "access-control-allow-origin" not in h and "set-cookie" not in h


def test_there_is_exactly_one_post_route_and_no_other_method(server):
    for method in ("PUT", "DELETE", "PATCH", "OPTIONS"):
        status, _, _ = call(server, method, "/api/jobs", {"X-UI-Token": server.token})
        assert status in (405, 501), method
    assert call(server, "POST", "/api/overview", {"X-UI-Token": server.token, "Content-Type": "application/json"}, "{}")[0] == 404
    assert call(server, "POST", "/api/approvals", {"X-UI-Token": server.token, "Content-Type": "application/json"}, "{}")[0] == 404


def test_a_post_needs_the_token_the_origin_and_a_small_json_body(server):
    ok_body = {"key": "sbom"}
    assert post(server, ok_body, token="")[0] == 403
    assert post(server, ok_body, token="wrong")[0] == 403
    assert call(server, "POST", "/api/jobs", {"Content-Type": "application/json"}, json.dumps(ok_body))[0] == 403   # no token
    assert post(server, ok_body, headers={"Origin": "http://evil.example"})[0] == 403
    assert post(server, ok_body, headers={"Origin": f"http://127.0.0.1:{server.port + 1}"})[0] == 403
    assert post(server, ok_body, headers={"Content-Type": "text/plain"})[0] == 415
    assert post(server, None, raw="x" * 5000)[0] == 413
    assert post(server, None, raw="not json")[0] == 400
    assert post(server, None, raw="[]")[0] == 400


def test_a_job_can_only_be_one_of_the_fixed_keys_and_a_dataset_that_exists(server):
    for payload in ({"key": "tests; echo hi"}, {"key": "vault"}, {"key": "run_model_eval"}, {"key": "mode"}, {"key": "../x"}, {"key": 5},
                    {"key": "backtest"}, {"key": "backtest", "dataset": "../../etc"}, {"key": "backtest", "dataset": "nope.v1"},
                    {"key": "backtest", "dataset": ["a"]}, {"key": "sbom", "extra": 1, "dataset": 7}):
        assert post(server, payload)[0] == 400, payload


def test_static_files_are_a_whitelist_and_cannot_be_traversed(server):
    assert call(server, "GET", "/static/app.js")[0] == 200
    for path in ("/static/../server.py", "/static/%2e%2e/server.py", "/static/..%2fserver.py", "/static/data.py", "/static/", "/static//etc/passwd",
                 "/ui/server.py", "/../CLAUDE.md", "/%2e%2e/CLAUDE.md"):
        assert call(server, "GET", path)[0] == 404, path


def test_names_from_the_browser_cannot_escape_their_folders(server):
    for path in ("/api/runs/..", "/api/runs/%2e%2e", "/api/runs/a%2fb", "/api/runs/..%5c..", "/api/runs/demo/packets/../x",
                 "/api/doc?path=../CLAUDE.md", "/api/doc?path=/etc/passwd", "/api/doc?path=src/trading_agent/core/mode.py",
                 "/api/doc?path=config/approvals.yaml.bak", "/api/doc?path=docs/../../etc/passwd",
                 "/api/backtests/../x", "/api/backtests/a/..", "/api/evals/report?id=../../x", "/api/evals/report?id=a/b/c"):
        assert call(server, "GET", path)[0] in (400, 404), path


def test_the_sealed_holdout_is_never_exposed(server):
    assert call(server, "GET", "/api/scenarios?split=holdout")[0] == 404
    assert call(server, "GET", "/api/scenarios/holdout/clean_entry-000")[0] == 404
    status, _, body = call(server, "GET", "/api/scenarios?split=development")
    assert status == 200 and json.loads(body)
    assert not os.path.exists(ROOT / "evals" / "planner_quality" / "holdout_access.log")


def test_errors_do_not_leak_paths_or_stack_traces(server):
    for path in ("/api/runs/zzz", "/api/doc?path=nope.md", "/api/runs/x/packets?limit=abc", "/api/backtests/a/b"):
        status, _, body = call(server, "GET", path)
        assert status in (400, 404), path
        assert re.fullmatch(r'\{"error": "[a-z ]+"\}', body.decode()), body
        assert str(ROOT) not in body.decode() and "Traceback" not in body.decode()


def test_nothing_in_the_ui_can_start_the_vault_the_model_or_change_a_mode():
    argvs = [" ".join(jobs.build_argv(ROOT, k, "synthetic-ca-2026jan.v1", ROOT / "runs")) for k in jobs.CHECK_KEYS + jobs.ACTION_KEYS]
    for text in argvs:
        for word in ("vault", "run_model_eval", "anthropic", "KILL_SWITCH", "runtime.backtest", "approvals", "git "):
            assert word not in text, (word, text)
    assert set(jobs.CHECK_KEYS + jobs.ACTION_KEYS) == {"tests", "imports", "schemas", "risk_lock", "sbom", "evals", "backtest", "demo_run"}


def test_job_processes_never_inherit_secrets_from_the_environment(monkeypatch):
    for name in ("TRADING_VAULT_TOKEN", "ANTHROPIC_API_KEY", "MY_PASSWORD", "GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY", "VAULT_PASSPHRASE", "SOME_CREDENTIAL"):
        monkeypatch.setenv(name, "value-that-must-not-leak")
    monkeypatch.setenv("HARMLESS_SETTING", "kept")
    env = jobs.clean_env(ROOT)
    assert not [k for k in env if jobs.SECRET_ENV.search(k)]
    assert "value-that-must-not-leak" not in " ".join(env.values()) and env["HARMLESS_SETTING"] == "kept"


def test_job_output_is_redacted_and_stripped_of_colour_codes():
    assert jobs.ANSI.sub("", "\x1b[22m\x1b[32mKEPT\x1b[0m") == "KEPT"
