"""Record what admission transactions read and wrote, from the client side.

The recorder is installed per connection through psycopg's `cursor_factory`, so
`src/ledgerops` is unchanged. It recognises the two balance statements that purchase
and reversal admission share (`_lock_balances` and `_post_transfer`):

- the locked balance read: the row the caller actually fetched gives ["r", account, version];
- the balance delta: a follow-up read of the same row inside the same transaction
  (which holds the row lock after UPDATE) gives the installed ["w", account, version].

Each admission attempt starts at its `SET TRANSACTION ISOLATION LEVEL` statement, so
internal retries become separate attempts. Outcomes come from PostgreSQL's commit log
(`pg_xact_status`) after the test, not from the caller: an attempt is "ok" only if it
committed. That also resolves attempts whose client saw only a connection error.

Test-only mutation: `strip_lock=True` removes `FOR UPDATE OF b` from the balance read,
recreating admission without its row lock. `after_read` lets a test pause both
transactions after their reads to force the race deterministically.
"""

import threading
import time

import psycopg

READ_MARK = "JOIN ledger.account_balance b"
LOCK_CLAUSE = " FOR UPDATE OF b"
WRITE_MARK = "SET debits=debits+%s, credits=credits+%s, version=version+1"
BEGIN_MARK = "SET TRANSACTION ISOLATION LEVEL"


class Recorder:
    def __init__(self, *, strip_lock=False, after_read=None):
        self.strip_lock = strip_lock
        self.after_read = after_read
        self.expect_anomalies = False
        self._lock = threading.Lock()
        self._attempts = []
        self._open = {}
        recorder = self

        class RecordingCursor(psycopg.Cursor):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self._pending_read = None

            def execute(self, query, params=None, **kwargs):
                text = query if isinstance(query, str) else ""
                self._pending_read = None
                if text.startswith(BEGIN_MARK):
                    recorder._begin(self.connection)
                if READ_MARK in text and LOCK_CLAUSE in text:
                    if recorder.strip_lock:
                        query = text.replace(LOCK_CLAUSE, "")
                    self._pending_read = str(params[1])
                result = super().execute(query, params, **kwargs)
                if WRITE_MARK in text:
                    row = (
                        psycopg.Cursor(self.connection)
                        .execute(
                            "SELECT version FROM ledger.account_balance "
                            "WHERE workspace_id=%s AND account_id=%s",
                            (params[2], params[3]),
                        )
                        .fetchone()
                    )
                    recorder._op(self.connection, ["w", str(params[3]), int(row[0])])
                return result

            def fetchone(self):
                row = super().fetchone()
                account, self._pending_read = self._pending_read, None
                if account is not None and row is not None:
                    version = row["version"] if isinstance(row, dict) else row[-1]
                    attempt = recorder._op(self.connection, ["r", account, int(version)])
                    if recorder.after_read is not None:
                        recorder.after_read(attempt)
                return row

        self.cursor_class = RecordingCursor

    def _begin(self, conn):
        with self._lock:
            attempt = {
                "process": conn.info.backend_pid,
                "ops": [],
                "xid": None,
                "invoke_ns": time.monotonic_ns(),
            }
            self._open[id(conn)] = attempt
            self._attempts.append(attempt)

    def _op(self, conn, op):
        with self._lock:
            attempt = self._open.get(id(conn))
        if attempt is None:
            return None  # a balance statement outside admission (fixture code)
        if attempt["xid"] is None:
            attempt["xid"] = (
                psycopg.Cursor(conn)
                .execute("SELECT pg_current_xact_id_if_assigned()::text")
                .fetchone()[0]
            )
        attempt["ops"].append(op)
        attempt["complete_ns"] = time.monotonic_ns()
        return attempt["ops"]

    def history(self, admin_conn):
        """Completed attempts with balance operations, statuses from the commit log."""
        result = []
        with self._lock:
            attempts = [a for a in self._attempts if a["ops"]]
        for attempt in attempts:
            status = "info"
            if attempt["xid"] is not None:
                state = admin_conn.execute(
                    "SELECT pg_xact_status(%s::xid8)", (attempt["xid"],)
                ).fetchone()[0]
                status = {"committed": "ok", "aborted": "fail"}.get(state, "info")
            result.append(
                {
                    "index": len(result),
                    "process": attempt["process"],
                    "xid": attempt["xid"],
                    "status": status,
                    "ops": attempt["ops"],
                    "invoke_ns": attempt["invoke_ns"],
                    "complete_ns": attempt.get("complete_ns"),
                }
            )
        return result


class RecordingDatabase:
    """Delegates to the per-test database; every new connection records."""

    def __init__(self, database, recorder):
        self._database = database
        self._recorder = recorder

    def __getattr__(self, name):
        return getattr(self._database, name)

    def connect(self, role="app"):
        conn = self._database.connect(role=role)
        conn.cursor_factory = self._recorder.cursor_class
        return conn
