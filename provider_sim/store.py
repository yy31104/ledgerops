"""Durable independent SQLite authority for the synthetic provider protocol."""

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from .protocol import (
    Conflict,
    NotFound,
    StoreBusy,
    ValidationError,
    canonical_json,
    canonical_uuid,
    sign_event,
    validate_key,
    validate_request,
    validate_signer,
)

_STATES = {"ACCEPTED", "SUCCEEDED", "FAILED", "CLOSED_ABSENT"}
_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS metadata "
    "(id INTEGER PRIMARY KEY CHECK(id=1), config TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS executions (
       provider_key TEXT PRIMARY KEY, request_bytes BLOB NOT NULL,
       request_sha256 TEXT NOT NULL, state TEXT NOT NULL
       CHECK(state IN ('ACCEPTED','SUCCEEDED','FAILED','CLOSED_ABSENT')),
       current_event INTEGER REFERENCES events(sequence) DEFERRABLE INITIALLY DEFERRED)""",
    """CREATE TABLE IF NOT EXISTS events (
       sequence INTEGER PRIMARY KEY, event_id TEXT UNIQUE NOT NULL,
       provider_key TEXT NOT NULL REFERENCES executions(provider_key),
       state TEXT NOT NULL, envelope TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS effects (
       provider_key TEXT PRIMARY KEY REFERENCES executions(provider_key),
       principal_minor TEXT NOT NULL, fee_minor TEXT NOT NULL, gross_minor TEXT NOT NULL)""",
    """CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
       BEGIN SELECT RAISE(ABORT, 'immutable provider event'); END""",
    """CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
       BEGIN SELECT RAISE(ABORT, 'immutable provider event'); END""",
    """CREATE TRIGGER IF NOT EXISTS effects_no_update BEFORE UPDATE ON effects
       BEGIN SELECT RAISE(ABORT, 'immutable provider effect'); END""",
    """CREATE TRIGGER IF NOT EXISTS effects_no_delete BEFORE DELETE ON effects
       BEGIN SELECT RAISE(ABORT, 'immutable provider effect'); END""",
    """CREATE TRIGGER IF NOT EXISTS executions_no_delete BEFORE DELETE ON executions
       BEGIN SELECT RAISE(ABORT, 'retained provider identity'); END""",
    """CREATE TRIGGER IF NOT EXISTS executions_transition BEFORE UPDATE ON executions
       WHEN NEW.provider_key != OLD.provider_key OR NEW.request_bytes != OLD.request_bytes
         OR NEW.request_sha256 != OLD.request_sha256
         OR (OLD.current_event IS NOT NULL AND
             (OLD.state != 'ACCEPTED' OR NEW.state NOT IN ('SUCCEEDED','FAILED')))
       BEGIN SELECT RAISE(ABORT, 'invalid provider transition'); END""",
)


class ProviderStore:
    """One durable namespace, bounded connections and serialized identity decisions.

    Caller owns the namespace directory and must retain it across recovery. This
    class neither resets history nor claims to withstand file loss or tampering.
    """

    def __init__(self, path, namespace, key_id, secret, busy_timeout_ms=2000):
        self.path = Path(path).absolute()
        if str(path) == ":memory:" or self.path.is_symlink():
            raise ValidationError("Provider requires a durable regular file")
        if self.path.exists() and not self.path.is_file():
            raise ValidationError("Provider requires a regular file")
        self.namespace = canonical_uuid(namespace)
        validate_signer(key_id, secret)
        self.key_id, self._secret = key_id, secret
        if type(busy_timeout_ms) is not int or not 1 <= busy_timeout_ms <= 60_000:
            raise ValidationError("Invalid SQLite busy timeout")
        self.busy_timeout_ms = busy_timeout_ms
        self.path.parent.mkdir(parents=True, exist_ok=True)
        config = canonical_json(
            {
                "schema_version": 1,
                "namespace": namespace,
                "key_id": key_id,
                "signer_digest": hashlib.sha256(secret).hexdigest(),
            }
        )
        with self._connection() as conn:
            mode = conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
            if mode != "delete":
                raise ValidationError("SQLite DELETE journal mode unavailable")
            with self._write(conn):
                for statement in _SCHEMA:
                    conn.execute(statement)
                row = conn.execute("SELECT config FROM metadata WHERE id=1").fetchone()
                if row is None:
                    conn.execute("INSERT INTO metadata(id,config) VALUES(1,?)", (config,))
                elif row[0] != config:
                    raise Conflict("Database namespace, schema or signing identity changed")

    @contextmanager
    def _connection(self):
        conn = sqlite3.connect(self.path, timeout=self.busy_timeout_ms / 1000, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("PRAGMA foreign_keys=ON")
            if (
                conn.execute("PRAGMA journal_mode").fetchone()[0] != "delete"
                or conn.execute("PRAGMA synchronous").fetchone()[0] != 2
                or conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1
            ):
                raise ValidationError("Unexpected SQLite persistence settings")
            yield conn
        except sqlite3.OperationalError as exc:
            if getattr(exc, "sqlite_errorcode", 0) & 0xFF in (
                sqlite3.SQLITE_BUSY,
                sqlite3.SQLITE_LOCKED,
            ):
                raise StoreBusy() from exc
            raise
        finally:
            conn.close()

    @contextmanager
    def _write(self, conn, hook=None):
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield
            if hook:
                hook("before_commit")
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        if hook:
            hook("after_commit")

    def _row(self, conn, key):
        return conn.execute("SELECT * FROM executions WHERE provider_key=?", (key,)).fetchone()

    def _event(self, conn, row):
        event = conn.execute(
            "SELECT envelope FROM events WHERE sequence=?", (row["current_event"],)
        ).fetchone()
        if event is None:
            raise RuntimeError("Provider history is incomplete")
        return json.loads(event[0])

    def _append(self, conn, key, raw, state):
        sequence = conn.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM events").fetchone()[0]
        event_id = str(uuid4())
        payload = {
            "kind": "execution",
            "issuer": "ledgerops-provider-sim",
            "contract_version": 1,
            "namespace": self.namespace,
            "provider_key": key,
            "event_id": event_id,
            "source_sequence": sequence,
            "state": state,
            "request_sha256": hashlib.sha256(raw).hexdigest(),
            "request": json.loads(raw),
        }
        envelope = sign_event(payload, self.key_id, self._secret)
        conn.execute(
            "INSERT INTO events(sequence,event_id,provider_key,state,envelope) VALUES(?,?,?,?,?)",
            (sequence, event_id, key, state, canonical_json(envelope)),
        )
        conn.execute(
            "UPDATE executions SET state=?,current_event=? WHERE provider_key=?",
            (state, sequence, key),
        )
        if state == "SUCCEEDED":
            request = payload["request"]
            conn.execute(
                "INSERT INTO effects VALUES(?,?,?,?)",
                (key, request["principal_minor"], request["fee_minor"], request["gross_minor"]),
            )
        return envelope

    def _create(self, conn, key, raw, state):
        conn.execute(
            "INSERT INTO executions(provider_key,request_bytes,request_sha256,state) "
            "VALUES(?,?,?,?)",
            (key, raw, hashlib.sha256(raw).hexdigest(), state),
        )
        return self._append(conn, key, raw, state)

    def put(self, key, request, *, outcome="SUCCEEDED", hook=None):
        raw = validate_request(key, request, self.namespace)
        if type(outcome) is not str or outcome not in _STATES - {"CLOSED_ABSENT"}:
            raise ValidationError("Invalid execution outcome")
        with self._connection() as conn, self._write(conn, hook):
            row = self._row(conn, key)
            if row is None:
                result = self._create(conn, key, raw, outcome)
            else:
                if row["request_bytes"] != raw:
                    raise Conflict()
                result = self._event(conn, row)
        return result

    def close(self, key, request, *, hook=None):
        raw = validate_request(key, request, self.namespace)
        with self._connection() as conn, self._write(conn, hook):
            row = self._row(conn, key)
            if row is None:
                result = self._create(conn, key, raw, "CLOSED_ABSENT")
            else:
                if row["request_bytes"] != raw:
                    raise Conflict()
                result = self._event(conn, row)
        return result

    def resolve(self, key, state, *, hook=None):
        validate_key(key, self.namespace)
        if state not in ("SUCCEEDED", "FAILED"):
            raise ValidationError("Only accepted requests can reach success or failure")
        with self._connection() as conn, self._write(conn, hook):
            row = self._row(conn, key)
            if row is None:
                raise NotFound()
            if row["state"] == state:
                result = self._event(conn, row)
            elif row["state"] == "ACCEPTED":
                result = self._append(conn, key, row["request_bytes"], state)
            else:
                raise Conflict("Terminal provider result cannot change")
        return result

    def lookup(self, key, *, force_absent=False):
        validate_key(key, self.namespace)
        if type(force_absent) is not bool:
            raise ValidationError("Invalid lookup fault")
        with self._connection() as conn:
            conn.execute("BEGIN")
            row = self._row(conn, key)
            if row is not None and not force_absent:
                return self._event(conn, row)
            watermark = conn.execute("SELECT COALESCE(MAX(sequence),0) FROM events").fetchone()[0]
        return sign_event(
            {
                "kind": "lookup",
                "issuer": "ledgerops-provider-sim",
                "contract_version": 1,
                "namespace": self.namespace,
                "provider_key": key,
                "observation_id": str(uuid4()),
                "state": "NOT_FOUND",
                "visibility_watermark": 0 if force_absent else watermark,
            },
            self.key_id,
            self._secret,
        )

    def history(self, after=0, watermark=None, limit=100):
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 1000:
            raise ValidationError("Invalid history pagination")
        with self._connection() as conn:
            conn.execute("BEGIN")
            maximum = conn.execute("SELECT COALESCE(MAX(sequence),0) FROM events").fetchone()[0]
            if watermark is None:
                watermark = maximum
            if type(watermark) is not int or not 0 <= after <= watermark <= maximum:
                raise ValidationError("Invalid history watermark")
            rows = conn.execute(
                "SELECT sequence,envelope FROM events WHERE sequence>? AND sequence<=? "
                "ORDER BY sequence LIMIT ?",
                (after, watermark, limit),
            ).fetchall()
        next_after = rows[-1]["sequence"] if rows else after
        events = [json.loads(row["envelope"]) for row in rows]
        metadata = {
            "namespace": self.namespace,
            "watermark": watermark,
            "after": after,
            "next_after": next_after,
            "complete": next_after == watermark,
            "event_payload_hashes": [
                hashlib.sha256(event["payload"].encode()).hexdigest() for event in events
            ],
        }
        return {
            "namespace": self.namespace,
            "watermark": watermark,
            "next_after": next_after,
            "complete": next_after == watermark,
            "events": events,
            "proof": sign_event({"kind": "history_page", **metadata}, self.key_id, self._secret),
        }

    def diagnostics(self):
        with self._connection() as conn:
            conn.execute("BEGIN")
            return {
                "namespace": self.namespace,
                "sqlite_version": sqlite3.sqlite_version,
                "journal_mode": conn.execute("PRAGMA journal_mode").fetchone()[0],
                "synchronous": conn.execute("PRAGMA synchronous").fetchone()[0],
                "foreign_keys": conn.execute("PRAGMA foreign_keys").fetchone()[0],
                "busy_timeout_ms": conn.execute("PRAGMA busy_timeout").fetchone()[0],
                "executions": conn.execute("SELECT COUNT(*) FROM executions").fetchone()[0],
                "events": conn.execute("SELECT COUNT(*) FROM events").fetchone()[0],
                "effects": conn.execute("SELECT COUNT(*) FROM effects").fetchone()[0],
                "integrity_check": conn.execute("PRAGMA quick_check").fetchone()[0],
            }
