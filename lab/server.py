"""Loopback-only teaching adapter. Never packaged in the ledgerops wheel."""

import ast
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from lab.session import LabError

ROOT = Path(__file__).resolve().parents[1]
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/lessons.json": ("lessons.json", "application/json; charset=utf-8"),
}
SOURCES = {
    "purchase": ("src/ledgerops/db/admission.py", "_submit_once"),
    "transfer": ("src/ledgerops/db/admission.py", "_post_transfer"),
    "balance_lock": ("src/ledgerops/db/admission.py", "_lock_balances"),
    "replay": ("src/ledgerops/db/admission.py", "_replay_by_request_key"),
    "reversal": ("src/ledgerops/db/admission.py", "_post_new_reversal"),
    "freeze": ("src/ledgerops/db/maintenance.py", "freeze_workspace"),
    "repair": ("src/ledgerops/db/maintenance.py", "apply_rebuild"),
    "resume": ("src/ledgerops/db/maintenance.py", "resume_workspace"),
    "verify": ("src/ledgerops/verification.py", "verify_snapshot"),
    "export": ("src/ledgerops/export.py", "capture_snapshot"),
    "schema": ("src/ledgerops/db/sql/001_foundation.sql", None),
}
ACTIONS = {
    "next",
    "opening",
    "purchase",
    "replay_purchase",
    "conflict_purchase",
    "reversal",
    "replay_reversal",
    "duplicate_reversal",
    "drift",
    "verify",
    "freeze",
    "preview",
    "rebuild",
    "resume",
    "reset",
    "shutdown",
}


def source_excerpt(key):
    """Fixed mapping, actual saved source, no arbitrary file access."""
    if key not in SOURCES:
        raise LabError("UNKNOWN_SOURCE", "没有这个代码片段。", 404)
    path, function = SOURCES[key]
    text = (ROOT / path).read_text()
    first, last = 1, len(text.splitlines())
    if function:
        node = next(
            n for n in ast.parse(text).body if isinstance(n, ast.FunctionDef) and n.name == function
        )
        first, last = node.lineno, node.end_lineno
    return {
        "title": function or "数据库约束与权限（完整迁移）",
        "path": path,
        "start_line": first,
        "text": "\n".join(text.splitlines()[first - 1 : last]),
    }


class LabServer(ThreadingHTTPServer):
    # server_close joins active actions before the pytest fixtures drop the DB.
    daemon_threads = False

    def __init__(self, session):
        self.session = session
        self.token = secrets.token_urlsafe(32)
        super().__init__(("127.0.0.1", 0), Handler)
        self.origin = f"http://127.0.0.1:{self.server_port}"

    def state(self):
        return {**self.session.state(), "token": self.token}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # No request bodies, credentials or trace parameters in the console log.
        pass

    def _send(self, status, value, content_type="application/json; charset=utf-8"):
        data = json.dumps(value, ensure_ascii=False).encode() if isinstance(value, dict) else value
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self'; "
            "script-src 'self'; connect-src 'self'; frame-ancestors 'none'",
        )
        self.end_headers()
        self.wfile.write(data)

    def _local(self):
        if self.headers.get("Host") != self.server.origin.removeprefix("http://"):
            raise LabError("LOCAL_ONLY", "只接受本机学习页面的访问。", 403)
        origin = self.headers.get("Origin")
        if origin is not None and origin != self.server.origin:
            raise LabError("LOCAL_ONLY", "不接受其他页面发来的请求。", 403)

    def _failure(self, exc):
        payload = {"error": {"code": exc.code, "message": exc.message}}
        if exc.http_status != 403:
            payload["state"] = self.server.state()
        self._send(exc.http_status, payload)

    def do_GET(self):
        try:
            self._local()
            path = urlsplit(self.path)
            if path.path == "/api/state":
                self._send(200, self.server.state())
            elif path.path == "/api/source":
                key = parse_qs(path.query).get("key", [""])[0]
                self._send(200, source_excerpt(key))
            elif path.path in ASSETS:
                name, content_type = ASSETS[path.path]
                self._send(200, (ROOT / "lab" / name).read_bytes(), content_type)
            elif path.path == "/favicon.ico":
                self._send(204, b"", "image/x-icon")
            else:
                raise LabError("NOT_FOUND", "没有这个页面。", 404)
        except LabError as exc:
            self._failure(exc)
        except Exception as exc:
            self._unexpected(exc)

    def do_POST(self):
        try:
            self._local()
            if self.path != "/api/action":
                raise LabError("NOT_FOUND", "没有这个操作入口。", 404)
            if not secrets.compare_digest(self.headers.get("X-Ledger-Lab", ""), self.server.token):
                raise LabError("LOCAL_ONLY", "请从本机学习页面发起操作。", 403)
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4096 or self.headers.get("Content-Type") != "application/json":
                raise LabError("INVALID_INPUT", "需要不超过4KB的JSON请求。")
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict) or body.keys() - {"action", "amount_minor", "merchant"}:
                raise LabError("INVALID_INPUT", "请求字段不符合学习工具约定。")
            action = body.get("action")
            if not isinstance(action, str) or action not in ACTIONS:
                raise LabError("UNKNOWN_ACTION", "没有这个学习操作。")
            if action == "shutdown":
                self._send(200, {**self.server.state(), "closing": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                self._send(
                    200, {**self.server.session.act(action, body), "token": self.server.token}
                )
        except LabError as exc:
            self._failure(exc)
        except (ValueError, TypeError):
            self._failure(LabError("INVALID_INPUT", "请求不是有效的JSON或金额。"))
        except Exception as exc:
            self._unexpected(exc)

    def _unexpected(self, exc):
        # Keep database diagnostics/DSNs out of the browser while preserving
        # an unmistakable failure. Financial failures never become fake success.
        print(f"Lab request failed: {type(exc).__name__}", flush=True)
        self._send(500, {"error": {"code": "INTERNAL_ERROR", "message": "执行失败，请查看终端。"}})


def serve(session):
    """Keep the fixture-owned database alive until explicit shutdown or Ctrl-C."""
    import signal

    with LabServer(session) as server:
        previous = signal.signal(
            signal.SIGINT,
            lambda *_: threading.Thread(target=server.shutdown, daemon=True).start(),
        )
        print(f"LEDGEROPS_LAB_URL={server.origin}/", flush=True)
        print("打开上面的地址开始学习；结束并清理按钮或 Ctrl-C 会清理临时数据库。", flush=True)
        try:
            server.serve_forever(poll_interval=0.1)
        finally:
            signal.signal(signal.SIGINT, previous)
