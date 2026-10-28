"""Step actual PostgreSQL lock histories in the disposable teaching database.

Only the lab worker cursors extend the service's short timeouts, by issuing extra
recorded SQL. Source admission, lock order, transactions and migrations are intact.
Waiting is always established from pg_stat_activity/pg_blocking_pids, never elapsed
sleep. The five-minute lifetime is an abandoned-session failure guard.
"""

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, RLock, Thread, Timer
from time import monotonic
from uuid import uuid4

from lab.trace import _TracedCursor
from ledgerops.db.admission import AdmissionError, submit_purchase
from ledgerops.db.maintenance import freeze_workspace
from ledgerops.export import capture_snapshot, export_workspace, load_export
from ledgerops.verification import verify_snapshot
from tests.integration.purchase_helpers import purchase_input

WATCHDOG_SECONDS = 300
_OBSERVE_SECONDS = 3
_JOIN_SECONDS = 12
_ACTIVITY_SQL = """SELECT pid,state,wait_event_type,wait_event,query,pg_blocking_pids(pid)
                   FROM pg_stat_activity WHERE pid=ANY(%s) ORDER BY pid"""


class DemoError(ValueError):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


class _DemoCursor(_TracedCursor):
    """Extend only lab wait time, with visible extra commands, not substituted SQL."""

    def execute(self, query, params=None, **kwargs):
        result = super().execute(query, params, **kwargs)
        if isinstance(query, str) and query.strip() == (
            "SELECT set_config('statement_timeout', '10s', true)"
        ):
            self._extend_teaching_wait()
        return result

    def _extend_teaching_wait(self):
        # This separate source frame identifies actual lab-only extra commands;
        # trace attribution skips only the delegating execute wrapper above.
        # Leave headroom for the five-minute session watchdog to cancel FREEZE
        # before A rolls back. Equal deadlines could let A's SQL timeout release
        # its shared barrier and allow the waiting FREEZE to commit first.
        super().execute("SELECT set_config('lock_timeout', '10min', true)")
        super().execute("SELECT set_config('statement_timeout', '10min', true)")


@dataclass
class _Actor:
    name: str
    role: str
    conn: object
    trace: list = field(default_factory=list)
    done: Event = field(default_factory=Event)
    thread: Thread | None = None
    http_status: int | None = None
    result: dict | None = None
    error: dict | None = None

    @property
    def pid(self):
        return self._pid

    def __post_init__(self):
        self._pid = self.conn.info.backend_pid


class ConcurrentDemo:
    def __init__(
        self, db, golden, artifact_root, code_revision, dirty=False, *, kind="purchase_race"
    ):
        if kind not in {"purchase_race", "freeze_drain"}:
            raise DemoError("UNKNOWN_SCENARIO", "没有这个并发场景。")
        self.db, self.golden, self.kind = db, deepcopy(golden), kind
        self.artifact_root, self.code_revision, self.dirty = (
            Path(artifact_root),
            code_revision,
            dirty,
        )
        self.workspace_id = golden["workspace"]["id"]
        self.stage, self.complete, self.closed = 0, False, False
        self.error, self.final = None, None
        self.actors, self.history = [], []
        self.observation = None
        self._observer, self._timer, self._blocker = None, None, None
        self.observer_pid = None
        self._lock = RLock()

    def _actor(self, name, role, *, pausable=False):
        conn = self.db.connect(role)
        if pausable:
            conn.cursor_factory = _DemoCursor
        actor = _Actor(name, role, conn)
        self.actors.append(actor)
        return actor

    def _spawn(self, name, role, call):
        actor = self._actor(name, role, pausable=True)

        def run():
            try:
                with self.db.record() as trace:
                    actor.trace = trace
                    result = call(actor.conn)
                if hasattr(result, "http_status"):
                    actor.http_status, actor.result = result.http_status, result.body
                else:
                    actor.result = result
            except AdmissionError as exc:
                actor.http_status = exc.http_status
                actor.error = {"code": exc.code, "sqlstate": None}
            except Exception as exc:
                actor.error = {
                    "code": type(exc).__name__,
                    "sqlstate": getattr(exc, "sqlstate", None),
                }
            finally:
                actor.done.set()

        actor.thread = Thread(target=run, name="ledgerops-lab-" + name, daemon=True)
        actor.thread.start()
        return actor

    def _purchase(self, name):
        arguments = purchase_input(
            self.golden,
            operation_id=str(uuid4()),
            key="concurrent-" + uuid4().hex,
            amount_minor="80",
        )
        return self._spawn(name, "app", lambda conn: submit_purchase(conn, *arguments))

    def _observe(self):
        if self._observer is None or self._observer.closed:
            return self.observation
        rows = self._observer.execute(_ACTIVITY_SQL, ([a.pid for a in self.actors],)).fetchall()
        result = {
            "observed_at": datetime.now(UTC).isoformat(),
            "rows": [
                {
                    "pid": row[0],
                    "state": row[1],
                    "wait_event_type": row[2],
                    "wait_event": row[3],
                    "query": row[4],
                    "blocking_pids": row[5],
                }
                for row in rows
            ],
            "edges": [
                {"waiter_pid": row[0], "blocker_pid": blocker} for row in rows for blocker in row[5]
            ],
        }
        self.observation = result
        return result

    def _waiting(self, actor, blockers, sql_fragment):
        deadline = monotonic() + _OBSERVE_SECONDS
        while monotonic() < deadline:
            observation = self._observe()
            row = next((row for row in observation["rows"] if row["pid"] == actor.pid), None)
            if (
                not actor.done.is_set()
                and row
                and row["wait_event_type"] == "Lock"
                and set(blockers).intersection(row["blocking_pids"])
                and sql_fragment in row["query"]
            ):
                self.history.append(deepcopy(observation))
                return
            if actor.done.is_set():
                raise DemoError("EXPECTED_WAIT_MISSING", "调用提前结束，没有观测到要求的锁等待。")
            actor.done.wait(0.01)  # Poll the observed condition; never infer order from time.
        raise DemoError("WAIT_NOT_OBSERVED", "未在保护期限内观测到要求的数据库锁依赖。")

    def _initial(self):
        with self.db.connect("reader") as conn:
            snapshot = capture_snapshot(conn, self.workspace_id)
        customer = self.golden["purchase_request"]["customer_account_id"]
        balance = next(row for row in snapshot["balances"] if row["account_id"] == customer)
        if not (
            snapshot["workspace"]["mode"] == "NORMAL"
            and verify_snapshot(snapshot).healthy
            and balance["booked"] == "100"
            and all(row["reserved"] == "0" for row in snapshot["balances"])
        ):
            raise DemoError(
                "FRESH_WORKSPACE_REQUIRED", "并发场景需要健康、客户余额100的初始工作区。"
            )
        self._observer = self.db.connect("admin")
        self.observer_pid = self._observer.info.backend_pid
        self._blocker = self._actor("blocker", "app")
        with self.db.record() as trace:
            self._blocker.trace = trace
            self._blocker.conn.execute("BEGIN")
            self._blocker.conn.execute(
                "SELECT account_id FROM ledger.account_balance "
                "WHERE workspace_id=%s AND account_id=%s FOR UPDATE",
                (self.workspace_id, customer),
            ).fetchone()
        self._timer = Timer(WATCHDOG_SECONDS, self._expire)
        self._timer.daemon = True
        self._timer.start()
        first = self._purchase("purchase_a")
        self._waiting(first, [self._blocker.pid], "FOR UPDATE OF b")

    def _second(self):
        first = self.actors[1]
        if self.kind == "purchase_race":
            second = self._purchase("purchase_b")
            blockers, fragment = [self._blocker.pid, first.pid], "FOR UPDATE OF b"
        else:
            actor = self.golden["actors"][0]["id"]
            freeze_id = str(uuid4())
            second = self._spawn(
                "freeze",
                "maintenance",
                lambda conn: freeze_workspace(
                    conn, self.workspace_id, freeze_id, actor, "local concurrency learning"
                ),
            )
            blockers, fragment = [first.pid], "pg_advisory_xact_lock("
        self._waiting(second, blockers, fragment)

    def _join(self, actor):
        actor.thread.join(_JOIN_SECONDS)
        if actor.thread.is_alive():
            raise DemoError("WORKER_NOT_FINISHED", "数据库调用没有在保护期限内结束。")
        if actor.error:
            raise DemoError("WORKER_FAILED", "并发调用失败，请查看实际SQL和SQLSTATE。")

    def _release(self):
        with self.db.record() as trace:
            self._blocker.conn.rollback()
        for index, entry in enumerate(trace, start=len(self._blocker.trace) + 1):
            entry["index"] = index
        self._blocker.trace.extend(trace)
        self._blocker.done.set()
        for actor in self.actors[1:]:
            self._join(actor)
        if self.kind == "purchase_race":
            if sorted(a.http_status for a in self.actors[1:]) != [201, 422]:
                raise DemoError("UNEXPECTED_RESULTS", "没有得到一笔成功、一笔余额不足的实际结果。")
        elif self.actors[1].http_status != 201 or not self.actors[2].result:
            raise DemoError("UNEXPECTED_RESULTS", "没有得到购买成功与冻结完成的实际结果。")
        self.history.append(deepcopy(self._observe()))
        self._verify_final()
        for actor in self.actors:
            actor.conn.close()

    def _probe(self):
        probe = self._purchase("after_freeze")
        probe.thread.join(_JOIN_SECONDS)
        if probe.thread.is_alive():
            raise DemoError("WORKER_NOT_FINISHED", "冻结后的购买未及时结束。")
        if probe.http_status != 503 or probe.error != {
            "code": "RETRY_SAME_IDENTITY",
            "sqlstate": None,
        }:
            raise DemoError("UNEXPECTED_RESULTS", "冻结后的新购买没有得到预期503。")
        self._verify_final()
        probe.conn.close()

    def _verify_final(self):
        with self.db.connect("reader") as conn:
            path = export_workspace(
                conn,
                self.workspace_id,
                self.artifact_root / "concurrency",
                code_revision=self.code_revision,
                dirty=self.dirty,
            )
        snapshot = load_export(path)
        report = verify_snapshot(snapshot).as_dict()
        if not report["healthy"]:
            raise DemoError("FINAL_VERIFICATION_FAILED", "并发结束后的独立核对没有通过。")
        self.final = {"snapshot": snapshot, "report": report, "export_id": path.name}

    def advance(self):
        with self._lock:
            if self.closed or self.complete or self.error:
                raise DemoError("SCENARIO_FINISHED", "场景已结束；重新开始可再次观察。")
            try:
                if self.stage == 0:
                    self._initial()
                elif self.stage == 1:
                    self._second()
                elif self.stage == 2:
                    self._release()
                else:
                    self._probe()
                self.stage += 1
                self.complete = self.stage == (3 if self.kind == "purchase_race" else 4)
                if self.complete:
                    self.close()
            except Exception as exc:
                self.error = {
                    "code": getattr(exc, "code", type(exc).__name__),
                    "message": "并发场景失败，正在取消未完成的调用。",
                }
                self.close()
                self.error["message"] = "并发场景失败；所有在途调用已取消并清理。"
                raise
            return self.state()

    def state(self):
        with self._lock:
            observation = self._observe() if not self.closed else self.observation
            waiting = {edge["waiter_pid"] for edge in (observation or {}).get("edges", [])}
            return deepcopy(
                {
                    "kind": self.kind,
                    "stage": self.stage,
                    "complete": self.complete,
                    "closed": self.closed,
                    "error": self.error,
                    "watchdog_seconds": WATCHDOG_SECONDS,
                    "observer_pid": self.observer_pid,
                    "workers": [
                        {
                            "name": a.name,
                            "pid": a.pid,
                            "role": a.role,
                            "status": "finished"
                            if a.done.is_set()
                            else "holding"
                            if a.thread is None
                            else "waiting"
                            if a.pid in waiting
                            else "running",
                            "connection_closed": bool(a.conn.closed),
                            "http_status": a.http_status,
                            "result": a.result,
                            "error": a.error,
                            "trace": a.trace,
                        }
                        for a in self.actors
                    ],
                    "observation": observation,
                    "history": self.history,
                    "final": self.final,
                }
            )

    def _expire(self):
        with self._lock:
            if not self.complete and not self.closed:
                self.error = {
                    "code": "SCENARIO_EXPIRED",
                    "message": "等待超过五分钟，正在取消场景。",
                }
                self.close()
                self.error["message"] = "等待超过五分钟，场景已取消并清理。"

    def close(self):
        """Cancel blocked workers before releasing the lock that could let them write."""
        with self._lock:
            if self.closed:
                return
            if self._timer is not None:
                self._timer.cancel()
            workers = [a for a in self.actors if a.thread is not None]
            # Stop and join dependants before their blockers. In freeze_drain,
            # cancelling A first would release its shared barrier and could let
            # FREEZE commit before FREEZE's cancellation reaches the server.
            for actor in reversed(workers):
                if not actor.done.is_set() and not actor.conn.closed:
                    actor.conn.cancel()
                if actor.thread.is_alive():
                    actor.thread.join(_JOIN_SECONDS)
                if actor.thread.is_alive():
                    actor.conn.close()
                    actor.thread.join(_JOIN_SECONDS)
                if actor.thread.is_alive():
                    raise DemoError("CLEANUP_INCOMPLETE", "数据库调用未结束，不能确认清理完成。")
            # Cancellation/rollback is complete before this row lock is released.
            if self._blocker is not None and not self._blocker.conn.closed:
                with self.db.record() as trace:
                    self._blocker.conn.rollback()
                for index, entry in enumerate(trace, start=len(self._blocker.trace) + 1):
                    entry["index"] = index
                self._blocker.trace.extend(trace)
                self._blocker.done.set()
            if self._observer is not None:
                self._observe()
                self._observer.close()
            for actor in self.actors:
                actor.conn.close()
            self.closed = True
