"""Strict WSGI boundary around trusted typed business APIs."""

import hashlib
import json
import re
import time
from datetime import datetime
from decimal import Decimal
from http import HTTPStatus
from importlib.resources import files
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import psycopg

from ledgerops.authentication import authenticate, authorized_snapshot
from ledgerops.db.admission import (
    AdmissionError,
    InvariantViolation,
    submit_payout,
    submit_purchase,
    submit_reversal,
)
from ledgerops.db.payout_admission import payout_state_rows
from ledgerops.domain import (
    OperationId,
    PayoutCommand,
    PurchaseCommand,
    RequestKey,
    ReversalCommand,
)
from ledgerops.http.assets import Asset, asset_for
from ledgerops.http.callbacks import CallbackUnavailable, authentic, receive
from ledgerops.http.callbacks import connect as callback_connection
from ledgerops.http.operators import connect as operator_connection
from ledgerops.http.operators import invoke as operator_request
from ledgerops.http.queries import metrics, operation_case, operation_receipt

MAX_BODY = 16384
MAX_RESPONSE = 1048576
EXPECTED_SCHEMA = tuple(
    (path.name, hashlib.sha256(path.read_bytes()).hexdigest())
    for path in sorted(files("ledgerops.db").joinpath("sql").iterdir(), key=lambda p: p.name)
    if path.name.endswith(".sql")
)


class HTTPError(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code
        super().__init__(code)


def unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def json_value(value):
    if isinstance(value, (UUID, Decimal)):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError("Unsupported response type")


def identity(value):
    try:
        parsed = UUID(value)
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPError(400, "INVALID_IDENTITY") from exc
    if str(parsed) != value:
        raise HTTPError(400, "INVALID_IDENTITY")
    return parsed


def body(environ):
    if environ.get("HTTP_TRANSFER_ENCODING"):
        raise HTTPError(400, "TRANSFER_ENCODING_UNSUPPORTED")
    length = environ.get("CONTENT_LENGTH", "")
    if not re.fullmatch("[0-9]{1,6}", length):
        raise HTTPError(411, "CONTENT_LENGTH_REQUIRED")
    length = int(length)
    if not 1 <= length <= MAX_BODY:
        raise HTTPError(413, "REQUEST_BODY_LIMIT")
    if environ.get("CONTENT_TYPE", "").lower().split(";")[0].strip() != "application/json":
        raise HTTPError(415, "JSON_CONTENT_TYPE_REQUIRED")
    raw = environ["wsgi.input"].read(length)
    if len(raw) != length:
        raise HTTPError(400, "INCOMPLETE_BODY")
    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=unique,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise HTTPError(400, "INVALID_JSON") from exc


class Application:
    def __init__(self, config):
        self.config = config
        self.requests = 0
        self.failures = 0

    def connect(self):
        conn = psycopg.connect(
            self.config.dsn,
            autocommit=True,
            connect_timeout=3,
            application_name="ledgerops-api",
            options=(
                "-c timezone=UTC -c statement_timeout=10000 -c lock_timeout=5000 "
                "-c transaction_timeout=30000 -c idle_in_transaction_session_timeout=15000"
            ),
        )
        try:
            row = conn.execute(
                "SELECT r.rolsuper,r.rolbypassrls,r.rolcreatedb,r.rolcreaterole,r.rolreplication,"
                "pg_has_role(current_user,'ledger_app','USAGE'),"
                "pg_has_role(current_user,'ledger_auth_reader','USAGE'),"
                "pg_has_role(current_user,'ledger_reader','USAGE'),"
                "pg_has_role(current_user,'ledger_auth_admin','MEMBER'),"
                "pg_has_role(current_user,'ledger_migrator','MEMBER'),"
                "pg_has_role(current_user,'ledger_worker','MEMBER') "
                "FROM pg_roles r WHERE r.rolname=current_user"
            ).fetchone()
            if row is None or any(row[:5]) or not all(row[5:8]) or any(row[8:]):
                raise HTTPError(503, "SERVICE_DATABASE_IDENTITY_INVALID")
            conn.execute("SELECT generation FROM ledger.auth_workspace_state LIMIT 0")
            actual = conn.execute(
                "SELECT version,sha256 FROM ledger.schema_migration ORDER BY version"
            ).fetchall()
            if tuple(actual) != EXPECTED_SCHEMA:
                raise HTTPError(503, "SERVICE_SCHEMA_MISMATCH")
            return conn
        except BaseException:
            conn.close()
            raise

    def boundary(self, environ):
        host = environ.get("HTTP_HOST", "")
        try:
            if not host or len(host) > 256 or not host.isascii() or any(c.isspace() for c in host):
                raise ValueError
            parsed = urlsplit("//" + host)
            if parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
                raise ValueError
            if parsed.hostname not in self.config.hosts:
                raise ValueError
            if parsed.port is not None and not 1 <= parsed.port <= 65535:
                raise ValueError
        except ValueError as exc:
            raise HTTPError(400, "INVALID_HOST") from exc
        origin = environ.get("HTTP_ORIGIN")
        if origin and origin != environ.get("wsgi.url_scheme", "http") + "://" + host:
            raise HTTPError(403, "CROSS_ORIGIN_DENIED")
        if len(environ.get("PATH_INFO", "")) > 1024 or len(environ.get("QUERY_STRING", "")) > 1024:
            raise HTTPError(414, "REQUEST_TARGET_LIMIT")
        if (
            self.config.production
            and environ.get("PATH_INFO", "") not in {"/health/live", "/health/ready"}
            and environ.get("wsgi.url_scheme") != "https"
        ):
            raise HTTPError(400, "HTTPS_REQUIRED")

    def __call__(self, environ, start_response):
        request_id = str(uuid4())
        started = time.monotonic()
        route = "unknown"
        self.requests += 1
        try:
            self.boundary(environ)
            status, result, route = self.dispatch(environ)
        except HTTPError as exc:
            status, result = exc.status, {"error": {"code": exc.code}}
        except AdmissionError as exc:
            status, result = (
                exc.http_status,
                {"error": {"code": exc.code, "retryable": exc.retryable}},
            )
        except (psycopg.OperationalError, psycopg.InterfaceError):
            status, result = (
                503,
                {"error": {"code": "RETRY_WITH_SAME_BUSINESS_IDENTITY", "retryable": True}},
            )
        except CallbackUnavailable:
            status, result = 503, {"error": {"code": "CALLBACK_UNAVAILABLE"}}
        except (ValueError, TypeError, RecursionError):
            status, result = 400, {"error": {"code": "INVALID_REQUEST"}}
        except (psycopg.Error, InvariantViolation):
            status, result = 500, {"error": {"code": "INTERNAL_INVARIANT_FAILURE"}}
        except Exception:
            status, result = 500, {"error": {"code": "INTERNAL_SERVICE_FAILURE"}}
        if status >= 500:
            self.failures += 1
        static = isinstance(result, Asset)
        raw = (
            result.raw
            if static
            else json.dumps(
                result, default=json_value, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode()
        )
        content_type = result.content_type if static else "application/json; charset=utf-8"
        csp = (
            "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
            "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
        )
        if len(raw) > MAX_RESPONSE:
            status = 413
            raw = b'{"error":{"code":"RESPONSE_SIZE_LIMIT"}}'
        headers = [
            ("Content-Type", content_type),
            ("Content-Length", str(len(raw))),
            ("Cache-Control", "no-store"),
            ("X-Content-Type-Options", "nosniff"),
            ("Referrer-Policy", "no-referrer"),
            ("X-Request-ID", request_id),
            ("Content-Security-Policy", csp),
        ]
        print(
            json.dumps(
                {
                    "event": "http_request",
                    "request_id": request_id,
                    "route": route,
                    "status": status,
                    "elapsed_ms": round((time.monotonic() - started) * 1000),
                    **{
                        name: str(result[name])
                        for name in ("operation_id", "action_id", "execution_id", "observation_id")
                        if not static
                        and name in result
                        and re.fullmatch(
                            "[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", str(result[name])
                        )
                    },
                },
                separators=(",", ":"),
            ),
            flush=True,
        )
        start_response(f"{status} {HTTPStatus(status).phrase}", headers)
        return [raw]

    def dispatch(self, environ):
        method, path = environ.get("REQUEST_METHOD"), environ.get("PATH_INFO", "")
        if method == "GET":
            asset = asset_for(path)
            if asset is not None:
                return 200, asset, "console_asset"
        if path == "/health/live" and method == "GET":
            return 200, {"live": True}, "live"
        if path == "/health/ready" and method == "GET":
            try:
                with self.connect() as conn:
                    conn.execute("SELECT operation_id,knowledge FROM ledger.payout LIMIT 0")
                if self.config.callback_trust is not None:
                    with callback_connection(self.config):
                        pass
                if self.config.operator_trust is not None:
                    with operator_connection(self.config):
                        pass
            except (psycopg.Error, HTTPError, CallbackUnavailable, AdmissionError):
                return 503, {"ready": False}, "ready"
            ready = not self.config.restore_mode and self.config.writes_enabled
            return (200 if ready else 503), {"ready": ready}, "ready"
        if path == "/v1/operator/actions" or path.startswith("/v1/operator/actions/"):
            if self.config.operator_trust is None:
                raise HTTPError(404, "NOT_FOUND")
            if environ.get("QUERY_STRING"):
                raise HTTPError(400, "QUERY_UNSUPPORTED")
            if method not in {"GET", "POST"}:
                raise HTTPError(405, "METHOD_NOT_ALLOWED")
            if method == "POST" and (self.config.restore_mode or not self.config.writes_enabled):
                raise HTTPError(503, "PHYSICAL_RESTORE_OR_WRITES_DISABLED")
            return operator_request(
                self.config,
                environ,
                path,
                method,
                body(environ) if method == "POST" else None,
                identity,
            )
        if method == "POST" and path.startswith("/v1/provider/callbacks/"):
            parts = path.removeprefix("/v1/provider/callbacks/").split("/")
            if self.config.callback_trust is None or len(parts) != 2:
                raise HTTPError(404, "NOT_FOUND")
            if self.config.restore_mode or not self.config.writes_enabled:
                raise HTTPError(503, "PHYSICAL_RESTORE_OR_WRITES_DISABLED")
            if environ.get("QUERY_STRING"):
                raise HTTPError(400, "MUTATION_QUERY_UNSUPPORTED")
            workspace, payout = map(identity, parts)
            envelope = body(environ)
            if not authentic(envelope, self.config.callback_trust):
                raise HTTPError(401, "INVALID_PROVIDER_AUTHENTICATION")
            raw = json.dumps(envelope, separators=(",", ":"), ensure_ascii=True).encode()
            result = receive(self.config, workspace, payout, raw)
            return (
                202,
                {
                    "status": result.status,
                    "state": result.state,
                    "observation_id": result.observation_id,
                    "code": result.code,
                },
                "callback",
            )
        route = {
            "/v1/purchases": ("purchase", PurchaseCommand, submit_purchase),
            "/v1/reversals": ("reversal", ReversalCommand, submit_reversal),
            "/v1/payouts": ("payout", PayoutCommand, submit_payout),
        }.get(path)
        if method == "POST" and route:
            if self.config.restore_mode or not self.config.writes_enabled:
                raise HTTPError(503, "PHYSICAL_RESTORE_OR_WRITES_DISABLED")
            if environ.get("QUERY_STRING"):
                raise HTTPError(400, "MUTATION_QUERY_UNSUPPORTED")
            payload = body(environ)
            key = environ.get("HTTP_IDEMPOTENCY_KEY", "")
            if re.fullmatch(r"[A-Za-z0-9._~-]{1,128}", key) is None:
                raise HTTPError(400, "IDEMPOTENCY_KEY_REQUIRED_OR_INVALID")
            with self.connect() as conn:
                credential = self.credential(conn, environ)
                name, command_type, submit = route
                command = command_type.from_payload(credential.principal.workspace_id, payload)
                request = RequestKey(
                    credential.principal.workspace_id,
                    credential.principal.client_id,
                    key,
                    f"{name}:v1",
                )
                kwargs = {"authorization_guard": credential.authorization_guard}
                if name == "payout":
                    policy = self.config.payout_policies.get(str(credential.principal.workspace_id))
                    if policy is None:
                        raise HTTPError(503, "PAYOUT_POLICY_NOT_CONFIGURED")
                    kwargs["policy"] = policy
                result = submit(conn, credential.principal, command, request, **kwargs)
                return result.http_status, result.body, name
        if method == "GET" and path in ("/v1/me", "/v1/accounts"):
            conn = self.connect()
            try:
                credential = self.credential(conn, environ)
                with authorized_snapshot(
                    conn, credential, None if path == "/v1/me" else "accounts:read"
                ) as cur:
                    if path == "/v1/me":
                        metadata = credential.identity_view(cur)
                        metadata["operator_controls_available"] = (
                            self.config.operator_trust is not None
                        )
                        return 200, metadata, "identity"
                    query = parse_qs(environ.get("QUERY_STRING", ""), strict_parsing=True)
                    if query.keys() - {"after"} or any(len(v) != 1 for v in query.values()):
                        raise HTTPError(400, "INVALID_PAGINATION")
                    after = identity(query["after"][0]) if "after" in query else UUID(int=0)
                    rows = cur.execute(
                        "SELECT a.id,a.class,a.currency,b.booked,b.reserved, "
                        "b.booked-b.reserved AS available,b.version FROM ledger.account a "
                        "JOIN ledger.account_balance b ON b.workspace_id=a.workspace_id "
                        "AND b.account_id=a.id "
                        "WHERE a.workspace_id=%s AND a.owner_id=%s AND a.id>%s "
                        "ORDER BY a.id LIMIT 101",
                        (
                            credential.principal.workspace_id.value,
                            credential.principal.actor_id.value,
                            after,
                        ),
                    ).fetchall()
                    for row in rows:
                        for field in ("booked", "reserved", "available", "version"):
                            row[field] = str(row[field])
                    return (
                        200,
                        {
                            "accounts": rows[:100],
                            "next_after": str(rows[99]["id"]) if len(rows) > 100 else None,
                        },
                        "accounts",
                    )
            finally:
                conn.close()
        if method == "GET" and (
            path.startswith("/v1/payouts/")
            or path.startswith("/v1/operations/")
            or path.startswith("/v1/investigation/operations/")
            or path == "/v1/metrics"
        ):
            if environ.get("QUERY_STRING"):
                raise HTTPError(400, "QUERY_UNSUPPORTED")
            conn = self.connect()
            try:
                credential = self.credential(conn, environ)
                scope = (
                    "payout:read"
                    if path.startswith("/v1/payouts/")
                    else "operations:read"
                    if path.startswith("/v1/operations/")
                    else "evidence:read"
                )
                with authorized_snapshot(conn, credential, scope) as cur:
                    workspace = credential.principal.workspace_id.value
                    if path == "/v1/metrics":
                        return 200, metrics(cur, workspace), "metrics"
                    identifier = identity(path.rsplit("/", 1)[1])
                    if scope == "payout:read":
                        state = payout_state_rows(
                            cur, credential.principal, OperationId(identifier)
                        )
                        state["source_sequence"] = str(state["source_sequence"])
                        state["attempts"] = str(state["attempts"])
                        for observation in state["recent_observations"]:
                            observation["source_sequence"] = str(observation["source_sequence"])
                        return 200, state, "payout_status"
                    if scope == "operations:read":
                        receipt = operation_receipt(
                            cur, workspace, credential.principal.client_id.value, identifier
                        )
                        if receipt is None:
                            raise HTTPError(404, "NOT_FOUND")
                        return 200, receipt, "operation_receipt"
                    case = operation_case(cur, workspace, identifier)
                    if case is None:
                        raise HTTPError(404, "NOT_FOUND")
                    return 200, case, "investigation"
            finally:
                conn.close()
        raise HTTPError(404, "NOT_FOUND")

    @staticmethod
    def credential(conn, environ):
        header = environ.get("HTTP_AUTHORIZATION", "")
        if not header.startswith("Bearer ") or len(header) > 128:
            raise HTTPError(401, "INVALID_CREDENTIAL")
        return authenticate(conn, header[7:])
