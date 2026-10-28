"""Observe client-issued SQL in the disposable lab, without changing core code.

Cursor commands and psycopg's internal transaction commands share one ordered
record. This is not a server/trigger trace or a lock-wait monitor. The protected
``_exec_command`` hook is deliberately local to this pinned psycopg lab adapter;
real-server tests guard it against dependency changes.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from inspect import currentframe
from pathlib import Path
from time import perf_counter
from uuid import UUID, uuid4

import psycopg
from psycopg import sql

_ROOT = Path(__file__).resolve().parents[1]
_RECORD = ContextVar("ledgerops_lab_sql_record", default=None)


def _json_value(value):
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return {"hex": raw.hex()}
    if isinstance(value, dict):
        return {str(k): _json_value(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(v) for v in value]
    if hasattr(value, "obj"):  # psycopg Json/Jsonb adapts this object.
        return _json_value(value.obj)
    return str(value)


def _source():
    frame = currentframe()
    source, attempt = None, None
    try:
        while frame is not None:
            path = Path(frame.f_code.co_filename).resolve()
            if path.is_relative_to(_ROOT):
                relative = path.relative_to(_ROOT).as_posix()
                if relative != "lab/trace.py" and source is None:
                    source = {
                        "path": relative,
                        "function": frame.f_code.co_name,
                        "line": frame.f_lineno,
                    }
                if (
                    relative == "src/ledgerops/db/admission.py"
                    and frame.f_code.co_name == "_submit_with_retry"
                ):
                    attempt = frame.f_locals.get("attempt")
            frame = frame.f_back
    finally:
        del frame  # Do not retain live financial frames via a reference cycle.
    return source, attempt


def _start(conn, command, params=None):
    record = _RECORD.get()
    if record is None:
        return None
    if isinstance(command, sql.Composable):
        statement = command.as_string(conn)
    elif isinstance(command, bytes):
        statement = command.decode(conn.info.encoding)
    else:
        statement = str(command)
    source, attempt = _source()
    entry = {
        "index": len(record) + 1,
        "sql": statement.strip(),
        "params": _json_value(params),
        "duration_ms": None,
        "status": "running",
        "sqlstate": None,
        "connection": conn.lab_connection_id,
        "role": conn.lab_role,
        "code": source,
        "attempt": attempt,
    }
    record.append(entry)
    return entry, perf_counter()


def _finish(observation, error=None):
    if observation is None:
        return
    entry, started = observation
    entry["duration_ms"] = round((perf_counter() - started) * 1000, 3)
    entry["status"] = "error" if error is not None else "ok"
    entry["sqlstate"] = getattr(error, "sqlstate", None)


class _TracedCursor(psycopg.Cursor):
    def execute(self, query, params=None, **kwargs):
        observation = _start(self.connection, query, params)
        try:
            result = super().execute(query, params, **kwargs)
        except BaseException as exc:
            _finish(observation, exc)
            raise
        _finish(observation)
        return result


class _TracedConnection(psycopg.Connection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.lab_connection_id = uuid4().hex[:12]
        self.lab_role = "unassigned"

    def _exec_command(self, command, *args, **kwargs):
        observation = _start(self, command)
        try:
            result = yield from super()._exec_command(command, *args, **kwargs)
        except BaseException as exc:
            _finish(observation, exc)
            raise
        _finish(observation)
        return result


class TracedDatabase:
    """Same role-scoped connection interface as the existing disposable fixture."""

    def __init__(self, database):
        self._database = database

    def connect(self, role="app"):
        if role not in {"app", "reader", "maintenance", "owner", "admin"}:
            raise ValueError("unknown test connection role")
        database = self._database
        role_name = {
            "app": "ledger_app",
            "reader": "ledger_reader",
            "maintenance": "ledger_maintenance",
            "owner": "ledger_migrator",
            "admin": "postgres",
        }[role]
        password = {
            "app": database.app_password,
            "reader": database.reader_password,
            "maintenance": database.maintenance_password,
        }.get(role, database.admin_password)
        conn = _TracedConnection.connect(
            host="127.0.0.1",
            port=database.port,
            dbname=database.name,
            user=role_name if role in {"app", "reader", "maintenance"} else "postgres",
            password=password,
            autocommit=True,
            connect_timeout=2,
            options="-c timezone=UTC -c statement_timeout=10000 -c lock_timeout=5000",
            cursor_factory=_TracedCursor,
        )
        conn.lab_role = role_name
        if role == "owner":
            try:
                conn.execute("SET ROLE ledger_migrator")
            except BaseException:
                conn.close()
                raise
        return conn

    @contextmanager
    def record(self):
        record = []
        token = _RECORD.set(record)
        try:
            yield record
        finally:
            _RECORD.reset(token)

    @contextmanager
    def suspend(self):
        token = _RECORD.set(None)
        try:
            yield
        finally:
            _RECORD.reset(token)
