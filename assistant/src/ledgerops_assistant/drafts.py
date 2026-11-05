"""Durable, content-bound local drafts; never creates a LedgerOps proposal."""

import hashlib
import json
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from importlib.resources import files
from uuid import NAMESPACE_URL, UUID, uuid5

import psycopg
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


class WorkflowConflict(Exception):
    pass


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


@dataclass(frozen=True)
class DraftIntent:
    payout_id: str
    observation_id: str
    evidence_run_id: str
    reason: str
    ttl_seconds: int = 900

    @classmethod
    def parse(cls, data):
        expected = {"payout_id", "observation_id", "evidence_run_id", "reason", "ttl_seconds"}
        if set(data) - expected or not expected - {"ttl_seconds"} <= set(data):
            raise ValueError("invalid draft fields")
        values = {k: str(UUID(data[k])) for k in ("payout_id", "observation_id", "evidence_run_id")}
        reason, ttl = data["reason"], data.get("ttl_seconds", 900)
        if (
            not isinstance(reason, str)
            or not reason.strip()
            or "\0" in reason
            or len(reason.encode("utf-8")) > 256
        ):
            raise ValueError("reason must be nonblank, contain no NUL, and fit in 256 UTF-8 bytes")
        if type(ttl) is not int or not 1 <= ttl <= 3600:
            raise ValueError("draft TTL must be 1 to 3600 seconds")
        return cls(**values, reason=reason, ttl_seconds=ttl)


def setup(dsn):
    with psycopg.connect(dsn) as conn:
        conn.execute(files("ledgerops_assistant").joinpath("sql/002_drafts.sql").read_text())
    with PostgresSaver.from_conn_string(dsn) as saver:
        saver.setup()


class DraftStore:
    def __init__(self, dsn):
        self.dsn = psycopg.conninfo.make_conninfo(
            dsn, connect_timeout=3, options="-c statement_timeout=5000 -c lock_timeout=3000"
        )

    def register(self, thread_key, request):
        hashed = digest(request)
        with psycopg.connect(self.dsn) as conn:
            conn.execute(
                "INSERT INTO assistant_workflow.runs(thread_key,request_sha256) "
                "VALUES (%s,%s) ON CONFLICT DO NOTHING",
                (thread_key, hashed),
            )
            found = conn.execute(
                "SELECT request_sha256 FROM assistant_workflow.runs WHERE thread_key=%s",
                (thread_key,),
            ).fetchone()
            if found[0] != hashed:
                raise WorkflowConflict("thread already binds different request content")

    def get_or_create(self, owner, request_id, intent, evidence):
        inputs = digest({"intent": asdict(intent), "evidence": evidence})
        action_id = str(uuid5(NAMESPACE_URL, f"ledgerops-assistant:{owner}:{request_id}:{inputs}"))
        payload = {
            **asdict(intent),
            "action_id": action_id,
            "action_type": "REPROCESS_TERMINAL_LOCAL_BLOCKER",
        }
        hashed = digest(payload)
        with psycopg.connect(self.dsn, row_factory=dict_row) as conn:
            conn.execute(
                "INSERT INTO assistant_workflow.drafts "
                "(owner_key,request_id,inputs_sha256,draft_sha256,action_id,payload,expires_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,now()+make_interval(secs=>%s)) "
                "ON CONFLICT DO NOTHING",
                (owner, request_id, inputs, hashed, action_id, Jsonb(payload), intent.ttl_seconds),
            )
            row = conn.execute(
                "SELECT * FROM assistant_workflow.drafts WHERE owner_key=%s AND request_id=%s",
                (owner, request_id),
            ).fetchone()
            if row is None or row["inputs_sha256"] != inputs:
                raise WorkflowConflict(
                    "request identity already binds different intent or evidence"
                )
        return {
            "action_id": str(row["action_id"]),
            "payload": row["payload"],
            "draft_sha256": row["draft_sha256"],
            "expires_at": row["expires_at"].isoformat(),
        }

    def expired(self, owner, request_id):
        with psycopg.connect(self.dsn) as conn:
            row = conn.execute(
                "SELECT expires_at<=now() FROM assistant_workflow.drafts "
                "WHERE owner_key=%s AND request_id=%s",
                (owner, request_id),
            ).fetchone()
        return row is None or row[0]

    @contextmanager
    def lock(self, thread_key):
        # A session advisory lock serializes invocations across processes, without a
        # transaction spanning network/model work. This is not a financial DB lock.
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            acquired = conn.execute(
                "SELECT pg_try_advisory_lock(hashtextextended(%s,0))", (thread_key,)
            ).fetchone()[0]
            if not acquired:
                raise WorkflowConflict("workflow is already running")
            try:
                yield
            finally:
                conn.execute("SELECT pg_advisory_unlock(hashtextextended(%s,0))", (thread_key,))
