"""Loopback-only HTTP adapter for the independent synthetic payment provider.

This is a local test double, not a production HTTP server.
This adapter exposes no LedgerOps business API. Fault controls exist only when
explicitly enabled, and use credentials distinct from the ordinary provider token.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from .protocol import ProviderError, sign_event

MAX_BODY_BYTES = 32 * 1024
MAX_HANDLERS = 16
REQUEST_TIMEOUT_SECONDS = 5
GATE_TIMEOUT_SECONDS = 60


class RequestError(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code
        super().__init__(code)


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON field")
        value[key] = item
    return value


def decode_body(raw):
    """Reject duplicate fields, non-UTF-8 input, and nonstandard JSON numbers."""

    def reject_constant(_):
        raise ValueError("nonstandard JSON number")

    try:
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=reject_constant
        )
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise RequestError(400, "INVALID_JSON") from exc
    if not isinstance(value, dict):
        raise RequestError(400, "OBJECT_REQUIRED")
    return value


def require_token(value):
    if not isinstance(value, str) or not 32 <= len(value) <= 512:
        raise ValueError("provider tokens must contain 32 to 512 printable ASCII characters")
    if not all(33 <= ord(c) <= 126 for c in value):
        raise ValueError("provider tokens must contain printable ASCII without whitespace")
    return value


class FaultControls:
    """Bounded test-only controls, never accepted as ordinary execution fields."""

    def __init__(self):
        self.lock = threading.Lock()
        self.gate = None
        self.release_event = threading.Event()
        self.config = {}

    def arm(self, data):
        if set(data) != {"provider_key", "point", "action"}:
            raise RequestError(400, "INVALID_CONTROL")
        if (
            not isinstance(data["provider_key"], str)
            or not 1 <= len(data["provider_key"]) <= 200
            or not isinstance(data["point"], str)
            or data["point"] not in {"before_commit", "after_commit"}
            or not isinstance(data["action"], str)
            or data["action"] not in {"pause", "exit"}
        ):
            raise RequestError(400, "INVALID_CONTROL")
        with self.lock:
            if self.gate is not None and not self.gate["released"]:
                raise RequestError(409, "GATE_ALREADY_ARMED")
            self.release_event = threading.Event()
            self.gate = {**data, "reached": False, "released": False}
        return self.state()

    def state(self):
        with self.lock:
            return {"gate": None if self.gate is None else dict(self.gate)}

    def release(self):
        with self.lock:
            if self.gate is not None:
                self.gate["released"] = True
            self.release_event.set()
        return self.state()

    def configure(self, data):
        if (
            not {"provider_key"} <= data.keys() <= {"provider_key", "lookup_lag", "put_outcome"}
            or not isinstance(data["provider_key"], str)
            or not 1 <= len(data["provider_key"]) <= 200
            or ("lookup_lag" in data and type(data["lookup_lag"]) is not bool)
            or not isinstance(data.get("put_outcome", "SUCCEEDED"), str)
            or data.get("put_outcome", "SUCCEEDED") not in {"SUCCEEDED", "FAILED", "ACCEPTED"}
        ):
            raise RequestError(400, "INVALID_CONTROL")
        with self.lock:
            key = data["provider_key"]
            if key not in self.config and len(self.config) >= 1000:
                raise RequestError(409, "CONTROL_LIMIT")
            entry = self.config.setdefault(key, {})
            entry.update({k: v for k, v in data.items() if k != "provider_key"})
            return dict(entry)

    def settings(self, key):
        with self.lock:
            return dict(self.config.get(key, {}))

    def hook(self, key):
        def reached(point):
            with self.lock:
                gate = self.gate
                if (
                    gate is None
                    or gate["reached"]
                    or gate["released"]
                    or gate["provider_key"] != key
                    or gate["point"] != point
                ):
                    return
                gate["reached"] = True
                action = gate["action"]
                release_event = self.release_event
            if action == "exit":
                os._exit(86)
            if not release_event.wait(GATE_TIMEOUT_SECONDS):
                with self.lock:
                    gate["released"] = True
                    release_event.set()
                raise TimeoutError("test gate timed out")

        return reached


class ProviderHTTPServer(ThreadingHTTPServer):
    daemon_threads = False
    block_on_close = True
    request_queue_size = MAX_HANDLERS

    def __init__(self, store, *, token, port=0, control_token=None):
        self.store = store
        self.token = require_token(token)
        self.control_token = None if control_token is None else require_token(control_token)
        if self.control_token is not None and hmac.compare_digest(self.token, self.control_token):
            raise ValueError("control credential must differ from provider credential")
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError("invalid port")
        self.controls = FaultControls() if control_token is not None else None
        self.slots = threading.BoundedSemaphore(MAX_HANDLERS)
        super().__init__(("127.0.0.1", port), ProviderHandler)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server_port}"

    def get_request(self):
        conn, addr = super().get_request()
        conn.settimeout(REQUEST_TIMEOUT_SECONDS)
        return conn, addr

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            try:
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\n"
                    b"Content-Length: 0\r\nConnection: close\r\n\r\n"
                )
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def server_close(self):
        if self.controls is not None:
            self.controls.release()
        super().server_close()

    def handle_error(self, request, client_address):
        # The HTTP handler returns bounded error codes. Never emit request headers,
        # request data, tokens, secrets, filesystem paths, or exception tracebacks.
        pass


class ProviderHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "LedgerOpsProvider/1"
    sys_version = ""

    def log_message(self, *args):
        pass

    def _one_header(self, name, required=False):
        values = self.headers.get_all(name, [])
        if len(values) > 1 or (required and not values):
            raise RequestError(400, "INVALID_HEADERS")
        return values[0] if values else None

    def _authorize(self, *, control):
        expected_host = f"127.0.0.1:{self.server.server_port}"
        if self._one_header("Host", required=True) != expected_host:
            raise RequestError(400, "INVALID_HOST")
        origin = self._one_header("Origin")
        if origin is not None and origin != self.server.url:
            raise RequestError(403, "INVALID_ORIGIN")
        token = self.server.control_token if control else self.server.token
        if token is None:
            raise RequestError(404, "NOT_FOUND")
        supplied = self._one_header("Authorization") or ""
        expected = f"Bearer {token}"
        # compare_digest rejects non-ASCII str; comparing UTF-8 bytes is total.
        if not hmac.compare_digest(supplied.encode("utf-8"), expected.encode("ascii")):
            raise RequestError(401, "UNAUTHENTICATED")

    def _body(self):
        if self._one_header("Transfer-Encoding") is not None:
            raise RequestError(400, "TRANSFER_ENCODING_UNSUPPORTED")
        length = self._one_header("Content-Length", required=True)
        if not re.fullmatch(r"0|[1-9][0-9]{0,8}", length):
            raise RequestError(400, "INVALID_CONTENT_LENGTH")
        size = int(length)
        if size > MAX_BODY_BYTES:
            raise RequestError(413, "BODY_TOO_LARGE")
        if self._one_header("Content-Type") != "application/json":
            raise RequestError(415, "JSON_REQUIRED")
        raw = self.rfile.read(size)
        if len(raw) != size:
            raise RequestError(400, "INCOMPLETE_BODY")
        return decode_body(raw)

    def _reply(self, status, data):
        raw = json.dumps(data, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(raw)
        self.close_connection = True

    def do_GET(self):
        self._dispatch()

    def do_PUT(self):
        self._dispatch()

    def do_POST(self):
        self._dispatch()

    def _dispatch(self):
        self.close_connection = True
        try:
            if len(self.path) > 2048:
                raise RequestError(414, "URI_TOO_LONG")
            parsed = urlsplit(self.path)
            if parsed.scheme or parsed.netloc or parsed.fragment or "%" in parsed.path:
                raise RequestError(400, "INVALID_TARGET")
            control = parsed.path.startswith("/__test/")
            self._authorize(control=control)
            if self._one_header("Transfer-Encoding") is not None:
                raise RequestError(400, "TRANSFER_ENCODING_UNSUPPORTED")
            if control:
                if parsed.query:
                    raise RequestError(400, "INVALID_QUERY")
                result = self._control(parsed.path)
            elif parsed.path == "/v1/history" and self.command == "GET":
                result = self._history(parsed.query)
            elif parsed.path.startswith("/v1/executions/") and not parsed.query:
                result = self._execution(parsed.path)
            else:
                raise RequestError(404, "NOT_FOUND")
            self._reply(200, result)
        except RequestError as exc:
            self._reply(exc.status, {"error": {"code": exc.code}})
        except ProviderError as exc:
            self._reply(exc.http_status, {"error": {"code": exc.code}})
        except TimeoutError:
            self._reply(503, {"error": {"code": "TIMEOUT_OUTCOME_UNKNOWN"}})
        except sqlite3.Error:
            self._reply(503, {"error": {"code": "STORE_UNAVAILABLE"}})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            self._reply(500, {"error": {"code": "INTERNAL_ERROR"}})

    def _execution(self, path):
        suffix = path.removeprefix("/v1/executions/")
        closing = suffix.endswith("/close-if-absent")
        key = suffix.removesuffix("/close-if-absent") if closing else suffix
        if not key or "/" in key:
            raise RequestError(404, "NOT_FOUND")
        controls = self.server.controls
        settings = controls.settings(key) if controls else {}
        hook = controls.hook(key) if controls else None
        store = self.server.store
        if self.command == "PUT" and not closing:
            return store.put(
                key, self._body(), outcome=settings.get("put_outcome", "SUCCEEDED"), hook=hook
            )
        if self.command == "GET" and not closing:
            return store.lookup(key, force_absent=settings.get("lookup_lag", False))
        if self.command == "POST" and closing:
            return store.close(key, self._body(), hook=hook)
        raise RequestError(405, "METHOD_NOT_ALLOWED")

    def _history(self, query):
        try:
            values = parse_qs(query, keep_blank_values=True, strict_parsing=True, max_num_fields=3)
        except ValueError as exc:
            raise RequestError(400, "INVALID_QUERY") from exc
        if not values.keys() <= {"after", "watermark", "limit"}:
            raise RequestError(400, "INVALID_QUERY")
        args = {}
        for name, value in values.items():
            if len(value) != 1 or not re.fullmatch(r"0|[1-9][0-9]{0,17}", value[0]):
                raise RequestError(400, "INVALID_QUERY")
            args[name] = int(value[0])
        return self.server.store.history(**args)

    def _control(self, path):
        controls = self.server.controls
        if self.command == "GET" and path == "/__test/state":
            return controls.state()
        if self.command != "POST":
            raise RequestError(405, "METHOD_NOT_ALLOWED")
        data = self._body()
        if path == "/__test/arm":
            return controls.arm(data)
        if path == "/__test/release" and not data:
            return controls.release()
        if path == "/__test/configure":
            return controls.configure(data)
        if path == "/__test/resolve" and set(data) == {"provider_key", "state"}:
            return self.server.store.resolve(data["provider_key"], data["state"])
        if path == "/__test/observation":
            return self._contradiction(data)
        raise RequestError(404, "NOT_FOUND")

    def _contradiction(self, data):
        if (
            set(data) != {"provider_key", "state"}
            or not isinstance(data["state"], str)
            or data["state"] not in {"SUCCEEDED", "FAILED", "CLOSED_ABSENT", "ACCEPTED"}
        ):
            raise RequestError(400, "INVALID_CONTROL")
        store = self.server.store
        result = store.lookup(data["provider_key"])
        payload = json.loads(result["payload"])
        if payload.get("kind") != "execution":
            raise RequestError(409, "EXECUTION_REQUIRED")
        payload.update(state=data["state"], event_id=str(uuid4()))
        # Deliberately violates the simulator's terminal immutability contract.
        # The real event/effect history is untouched; separate test credentials
        # are mandatory, and this helper is absent in the normal server mode.
        return sign_event(payload, store.key_id, store._secret)
