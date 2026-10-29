"""Database-derived opaque credentials and current transactional workspace grants.

Financial callers enter their existing workspace barrier before calling the guard.
All credential administration cooperates with the exclusive auth advisory barrier.
Raw schema-owner/administrator SQL is a separate trusted boundary, not an API.
"""

import hashlib
import re
import secrets
from contextlib import contextmanager
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import psycopg
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from ledgerops.db.admission import AdmissionError, Principal
from ledgerops.domain import ActorId, ClientId, WorkspaceId

SCOPES = frozenset(
    {
        "accounts:read",
        "operations:read",
        "payout:read",
        "purchase:write",
        "reversal:write",
        "payout:write",
        "evidence:read",
        "maintenance:freeze",
        "maintenance:preview",
        "maintenance:resume",
        "approval:propose",
        "approval:approve",
        "approval:execute",
    }
)
MAX_ACTIVE_CREDENTIALS = 20
_MAX_VERSION = 2**63 - 1


class AuthenticationRequired(AdmissionError):
    http_status = 401
    code = "INVALID_CREDENTIAL"

    def __init__(self):
        super().__init__("Valid current credentials required")


class PermissionDenied(AdmissionError):
    http_status = 403
    code = "FORBIDDEN"

    def __init__(self):
        super().__init__("Current permission required")


class AuthManagementError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _uuid(value):
    if type(value) is not str:
        raise ValueError("Canonical UUID required")
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise ValueError("Canonical UUID required") from exc
    if str(parsed) != value:
        raise ValueError("Canonical UUID required")
    return parsed


def _reason(value):
    if type(value) is not str or "\0" in value or not 1 <= len(value.encode()) <= 256:
        raise ValueError("Bounded management reason required")
    return value


def _scopes(value):
    if type(value) not in (list, tuple, set, frozenset) or len(value) > len(SCOPES):
        raise ValueError("Bounded explicit scopes required")
    if any(type(scope) is not str or scope not in SCOPES for scope in value):
        raise ValueError("Unknown permission scope")
    if len(set(value)) != len(value):
        raise ValueError("Duplicate permission scope")
    return sorted(value)


def _idle(conn):
    if not conn.autocommit or conn.info.transaction_status != TransactionStatus.IDLE:
        raise ValueError("Authentication requires an idle autocommit connection")


def _auth_key(workspace):
    return f"ledgerops:auth:{workspace}"


def _auth_lock(cur, workspace, *, shared):
    function = "pg_advisory_xact_lock_shared" if shared else "pg_advisory_xact_lock"
    cur.execute(f"SELECT {function}(hashtextextended(%s,0))", (_auth_key(workspace),))


def _workspace_lock(cur, workspace, *, shared):
    function = "pg_advisory_xact_lock_shared" if shared else "pg_advisory_xact_lock"
    cur.execute(f"SELECT {function}(hashtextextended(%s,0))", (f"ledgerops:workspace:{workspace}",))


def _timeouts(cur):
    cur.execute("SELECT set_config('statement_timeout','10s',true)")
    cur.execute("SELECT set_config('lock_timeout','5s',true)")
    cur.execute("SELECT set_config('transaction_timeout','30s',true)")


@contextmanager
def _admin_transaction(conn, workspace):
    _idle(conn)
    with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED READ WRITE")
        _timeouts(cur)
        _workspace_lock(cur, workspace, shared=True)
        _auth_lock(cur, workspace, shared=False)
        if (
            cur.execute("SELECT id FROM ledger.workspace WHERE id=%s", (workspace,)).fetchone()
            is None
        ):
            raise AuthManagementError("WORKSPACE_NOT_FOUND")
        yield cur


def _audit(
    cur,
    workspace,
    admin,
    reason,
    action,
    after,
    *,
    before=None,
    actor=None,
    client=None,
    credential=None,
):
    cur.execute(
        "INSERT INTO ledger.auth_audit "
        "(workspace_id,audit_id,action,admin_actor_id,subject_actor_id,client_id,credential_id,"
        "reason,before_state,after_state) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            workspace,
            uuid4(),
            action,
            admin,
            actor,
            client,
            credential,
            reason,
            None if before is None else Jsonb(before),
            Jsonb(after),
        ),
    )


def _workspace_state(cur, workspace, admin, reason):
    row = cur.execute(
        "SELECT * FROM ledger.auth_workspace_state WHERE workspace_id=%s", (workspace,)
    ).fetchone()
    if row is None:
        row = cur.execute(
            "INSERT INTO ledger.auth_workspace_state(workspace_id,generation) VALUES(%s,%s) "
            "RETURNING *",
            (workspace, uuid4()),
        ).fetchone()
        _audit(
            cur,
            workspace,
            admin,
            reason,
            "WORKSPACE_BOOTSTRAPPED",
            {"generation": str(row["generation"]), "version": row["version"]},
        )
    return row


def _version(row, expected):
    if type(expected) is not int or expected != row["version"]:
        raise AuthManagementError("STALE_AUTH_VERSION")
    if expected >= _MAX_VERSION:
        raise AuthManagementError("AUTH_VERSION_EXHAUSTED")


def set_membership(
    conn,
    workspace_id,
    actor_id,
    scopes,
    *,
    admin_actor_id,
    reason,
    enabled=True,
    expected_version=None,
):
    workspace, actor, admin = _uuid(workspace_id), _uuid(actor_id), _uuid(admin_actor_id)
    scopes, reason = _scopes(scopes), _reason(reason)
    if type(enabled) is not bool:
        raise ValueError("Boolean membership state required")
    with _admin_transaction(conn, workspace) as cur:
        _workspace_state(cur, workspace, admin, reason)
        previous = cur.execute(
            "SELECT * FROM ledger.auth_membership WHERE workspace_id=%s AND actor_id=%s",
            (workspace, actor),
        ).fetchone()
        before = (
            None
            if previous is None
            else {name: previous[name] for name in ("scopes", "enabled", "version")}
        )
        if previous is None:
            if expected_version is not None:
                raise AuthManagementError("MEMBERSHIP_NOT_FOUND")
            row = cur.execute(
                "INSERT INTO ledger.auth_membership(workspace_id,actor_id,scopes,enabled) "
                "VALUES(%s,%s,%s,%s) RETURNING *",
                (workspace, actor, scopes, enabled),
            ).fetchone()
            action = "MEMBERSHIP_CREATED"
        else:
            _version(previous, expected_version)
            row = cur.execute(
                "UPDATE ledger.auth_membership SET scopes=%s,enabled=%s,version=version+1 "
                "WHERE workspace_id=%s AND actor_id=%s RETURNING *",
                (scopes, enabled, workspace, actor),
            ).fetchone()
            action = "MEMBERSHIP_CHANGED"
        after = {name: row[name] for name in ("scopes", "enabled", "version")}
        _audit(cur, workspace, admin, reason, action, after, before=before, actor=actor)
    return after


def set_client(
    conn,
    workspace_id,
    client_id,
    actor_id,
    *,
    admin_actor_id,
    reason,
    enabled=True,
    expected_version=None,
):
    workspace, client, actor, admin = map(
        _uuid, (workspace_id, client_id, actor_id, admin_actor_id)
    )
    reason = _reason(reason)
    if type(enabled) is not bool:
        raise ValueError("Boolean client state required")
    with _admin_transaction(conn, workspace) as cur:
        previous = cur.execute(
            "SELECT * FROM ledger.auth_client WHERE workspace_id=%s AND client_id=%s",
            (workspace, client),
        ).fetchone()
        before = (
            None
            if previous is None
            else {
                "actor_id": str(previous["actor_id"]),
                "enabled": previous["enabled"],
                "version": previous["version"],
            }
        )
        if previous is None:
            if expected_version is not None:
                raise AuthManagementError("CLIENT_NOT_FOUND")
            row = cur.execute(
                "INSERT INTO ledger.auth_client(workspace_id,client_id,actor_id,enabled) "
                "VALUES(%s,%s,%s,%s) RETURNING *",
                (workspace, client, actor, enabled),
            ).fetchone()
            action = "CLIENT_CREATED"
        else:
            if previous["actor_id"] != actor:
                raise AuthManagementError("IMMUTABLE_CLIENT_ACTOR")
            _version(previous, expected_version)
            row = cur.execute(
                "UPDATE ledger.auth_client SET enabled=%s,version=version+1 WHERE "
                "workspace_id=%s AND client_id=%s RETURNING *",
                (enabled, workspace, client),
            ).fetchone()
            action = "CLIENT_CHANGED"
        after = {
            "actor_id": str(row["actor_id"]),
            "enabled": row["enabled"],
            "version": row["version"],
        }
        _audit(
            cur, workspace, admin, reason, action, after, before=before, actor=actor, client=client
        )
    return after


@dataclass(frozen=True, slots=True, repr=False)
class IssuedCredential:
    credential_id: UUID
    token: str = field(repr=False)
    expires_at: object
    generation: UUID


def issue_credential(
    conn, workspace_id, client_id, scopes, *, admin_actor_id, reason, ttl_seconds=3600
):
    workspace, client, admin = map(_uuid, (workspace_id, client_id, admin_actor_id))
    scopes, reason = _scopes(scopes), _reason(reason)
    if not scopes or type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 2_592_000:
        raise ValueError("Explicit credential scopes and TTL from1second to30days required")
    token = "lo1_" + secrets.token_urlsafe(32)
    digest, identifier = hashlib.sha256(token.encode()).digest(), uuid4()
    with _admin_transaction(conn, workspace) as cur:
        state = _workspace_state(cur, workspace, admin, reason)
        row = cur.execute(
            "SELECT c.actor_id,c.enabled,m.enabled AS member_enabled,m.scopes FROM "
            "ledger.auth_client c JOIN ledger.auth_membership m USING(workspace_id,actor_id) "
            "WHERE c.workspace_id=%s AND c.client_id=%s",
            (workspace, client),
        ).fetchone()
        if (
            row is None
            or not row["enabled"]
            or not row["member_enabled"]
            or not set(scopes) <= set(row["scopes"])
        ):
            raise AuthManagementError("CURRENT_CLIENT_GRANTS_REQUIRED")
        active = cur.execute(
            "SELECT count(*) AS count FROM ledger.auth_credential WHERE workspace_id=%s AND "
            "client_id=%s AND revoked_at IS NULL AND expires_at>clock_timestamp()",
            (workspace, client),
        ).fetchone()["count"]
        if active >= MAX_ACTIVE_CREDENTIALS:
            raise AuthManagementError("ACTIVE_CREDENTIAL_LIMIT")
        credential = cur.execute(
            "WITH stamp AS (SELECT clock_timestamp() AS now) INSERT INTO "
            "ledger.auth_credential(workspace_id,credential_id,client_id,generation,"
            "token_sha256,scopes,issued_at,expires_at) SELECT "
            "%s,%s,%s,%s,%s,%s,now,now+make_interval(secs=>%s) FROM stamp RETURNING *",
            (workspace, identifier, client, state["generation"], digest, scopes, ttl_seconds),
        ).fetchone()
        _audit(
            cur,
            workspace,
            admin,
            reason,
            "CREDENTIAL_ISSUED",
            {
                "scopes": scopes,
                "expires_at": credential["expires_at"].isoformat(),
                "generation": str(state["generation"]),
                "version": 1,
            },
            actor=row["actor_id"],
            client=client,
            credential=identifier,
        )
    return IssuedCredential(identifier, token, credential["expires_at"], state["generation"])


def revoke_credential(conn, workspace_id, credential_id, *, admin_actor_id, reason):
    workspace, credential, admin = map(_uuid, (workspace_id, credential_id, admin_actor_id))
    reason = _reason(reason)
    with _admin_transaction(conn, workspace) as cur:
        row = cur.execute(
            "SELECT * FROM ledger.auth_credential WHERE workspace_id=%s AND credential_id=%s",
            (workspace, credential),
        ).fetchone()
        if row is None:
            raise AuthManagementError("CREDENTIAL_NOT_FOUND")
        if row["revoked_at"] is not None:
            return {"credential_id": str(credential), "revoked": True, "version": row["version"]}
        _version(row, row["version"])
        updated = cur.execute(
            "UPDATE ledger.auth_credential SET revoked_at=clock_timestamp(),version=version+1 "
            "WHERE workspace_id=%s AND credential_id=%s RETURNING version",
            (workspace, credential),
        ).fetchone()
        after = {"credential_id": str(credential), "revoked": True, "version": updated["version"]}
        _audit(
            cur,
            workspace,
            admin,
            reason,
            "CREDENTIAL_REVOKED",
            after,
            before={"revoked": False, "version": row["version"]},
            client=row["client_id"],
            credential=credential,
        )
    return after


_LOOKUP = """SELECT c.workspace_id,c.client_id,c.credential_id,c.generation,
 c.expires_at,c.revoked_at,
 c.scopes AS credential_scopes,c.version AS credential_version,
 l.actor_id,l.enabled AS client_enabled,l.version AS client_version,
 m.enabled AS member_enabled,m.scopes AS membership_scopes,m.version AS membership_version,
 s.generation AS current_generation,clock_timestamp() AS decided_at
 FROM ledger.auth_credential c JOIN ledger.auth_client l USING(workspace_id,client_id)
 JOIN ledger.auth_membership m ON m.workspace_id=l.workspace_id AND m.actor_id=l.actor_id
 JOIN ledger.auth_workspace_state s ON s.workspace_id=c.workspace_id WHERE c.token_sha256=%s"""


def _current(cur, digest, required_scope=None):
    row = cur.execute(_LOOKUP, (digest,)).fetchone()
    if (
        row is None
        or not row["client_enabled"]
        or not row["member_enabled"]
        or row["revoked_at"] is not None
        or row["expires_at"] <= row["decided_at"]
        or row["generation"] != row["current_generation"]
    ):
        raise AuthenticationRequired()
    if required_scope is not None and (
        required_scope not in row["credential_scopes"]
        or required_scope not in row["membership_scopes"]
    ):
        raise PermissionDenied()
    return row


def _principal(row):
    return Principal(
        WorkspaceId(row["workspace_id"]), ClientId(row["client_id"]), ActorId(row["actor_id"])
    )


@dataclass(frozen=True, slots=True, repr=False)
class AuthenticatedCredential:
    principal: Principal
    credential_id: UUID
    generation: UUID
    _digest: bytes = field(repr=False)

    def authorization_guard(self, cur, principal, required_scope):
        """Caller already holds shared workspace barrier; scope is fixed by the core route."""
        if required_scope not in SCOPES:
            raise ValueError("Unknown endpoint scope")
        if type(principal) is not Principal or principal != self.principal:
            raise AuthenticationRequired()
        cur.execute("SELECT set_config('transaction_timeout','30s',true)")
        _auth_lock(cur, principal.workspace_id.value, shared=True)
        return self._check(cur, required_scope)

    def _check(self, cur, required_scope):
        row = _current(cur, self._digest, required_scope)
        if (
            _principal(row) != self.principal
            or row["credential_id"] != self.credential_id
            or row["generation"] != self.generation
        ):
            raise AuthenticationRequired()
        return {
            "generation": str(row["generation"]),
            "membership_version": row["membership_version"],
            "client_version": row["client_version"],
            "credential_version": row["credential_version"],
            "credential_id": str(row["credential_id"]),
            "decided_at": row["decided_at"],
        }

    def identity_view(self, cur):
        """Metadata only, inside a current-auth snapshot; never a permission substitute."""
        self._check(cur, None)
        row = _current(cur, self._digest)
        return {
            "workspace_id": str(self.principal.workspace_id),
            "actor_id": str(self.principal.actor_id),
            "client_id": str(self.principal.client_id),
            "scopes": sorted(set(row["credential_scopes"]) & set(row["membership_scopes"])),
            "expires_at": row["expires_at"].isoformat(),
        }


def authenticate(conn, token):
    """Locate immutable identity only. Current permission is rechecked in the actual operation."""
    _idle(conn)
    if type(token) is not str or not re.fullmatch(r"lo1_[A-Za-z0-9_-]{43}", token):
        raise AuthenticationRequired()
    digest = hashlib.sha256(token.encode()).digest()
    with conn.cursor(row_factory=dict_row) as cur:
        row = _current(cur, digest)
    return AuthenticatedCredential(_principal(row), row["credential_id"], row["generation"], digest)


@contextmanager
def authorized_snapshot(conn, credential, required_scope):
    """Own and close a dedicated connection; acquire auth lock BEFORE the RR snapshot.

    Session locks have explicit finally cleanup. READ ONLY requires no UPDATE grant
    on credential tables. Expiry is evaluated at the authoritative DB decision time.
    None is reserved for current public identity metadata; financial routes pass
    their fixed scope and authorization_guard rejects a missing scope.
    """
    _idle(conn)
    if type(credential) is not AuthenticatedCredential or (
        required_scope is not None and required_scope not in SCOPES
    ):
        raise ValueError("Authenticated credential and fixed read scope required")
    key = _auth_key(credential.principal.workspace_id.value)
    locked = False
    try:
        conn.execute("SELECT set_config('statement_timeout','10s',false)")
        conn.execute("SELECT set_config('lock_timeout','5s',false)")
        conn.execute("SELECT set_config('idle_in_transaction_session_timeout','15s',false)")
        conn.execute("SELECT pg_advisory_lock_shared(hashtextextended(%s,0))", (key,))
        locked = True
        with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            cur.execute("SELECT set_config('transaction_timeout','30s',true)")
            credential._check(cur, required_scope)
            yield cur
    finally:
        try:
            if (
                locked
                and not conn.closed
                and conn.info.transaction_status == TransactionStatus.IDLE
            ):
                conn.execute("SELECT pg_advisory_unlock_shared(hashtextextended(%s,0))", (key,))
        except psycopg.Error:
            pass  # Closing this owned connection is the final lock-release boundary.
        finally:
            conn.close()


def invalidate_restored_access(conn, *, restore_id, backup_id, admin_actor_id, reason):
    """Atomically close restored workspaces and invalidate old access/approval generations.

    Run before allowing runtime connections. Restore administrator needs both workspace
    maintenance and auth-admin grants. Replaying this restore ID never revokes fresh
    credentials issued after the first successful invalidation.
    """
    restore, backup, admin = map(_uuid, (restore_id, backup_id, admin_actor_id))
    reason = _reason(reason)
    _idle(conn)
    with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED READ WRITE")
        _timeouts(cur)
        cur.execute("SELECT pg_advisory_xact_lock(hashtextextended('ledgerops:restore-access',0))")
        workspaces = [
            row["id"]
            for row in cur.execute(
                "SELECT id FROM ledger.workspace ORDER BY id LIMIT 10001"
            ).fetchall()
        ]
        if len(workspaces) > 10000:
            raise AuthManagementError("RESTORE_WORKSPACE_LIMIT")
        previous = cur.execute(
            "SELECT * FROM ledger.auth_restore_run WHERE restore_id=%s", (restore,)
        ).fetchone()
        if previous is not None:
            if (
                previous["backup_id"] != backup
                or previous["admin_actor_id"] != admin
                or previous["reason"] != reason
                or previous["workspace_ids"] != workspaces
            ):
                raise AuthManagementError("RESTORE_IDENTITY_CONFLICT")
            records = cur.execute(
                "SELECT * FROM ledger.auth_restore_workspace WHERE restore_id=%s ORDER BY "
                "workspace_id",
                (restore,),
            ).fetchall()
            for row in records:
                current = cur.execute(
                    "SELECT generation FROM ledger.auth_workspace_state WHERE workspace_id=%s",
                    (row["workspace_id"],),
                ).fetchone()
                if current is None or current["generation"] != row["generation"]:
                    raise AuthManagementError("RESTORE_GENERATION_SUPERSEDED")
            return _restore_result(restore, backup, records)
        for workspace in workspaces:
            _workspace_lock(cur, workspace, shared=False)
        for workspace in workspaces:
            _auth_lock(cur, workspace, shared=False)
        cur.execute(
            "INSERT INTO "
            "ledger.auth_restore_run(restore_id,backup_id,workspace_ids,admin_actor_id,reason) "
            "VALUES(%s,%s,%s,%s,%s)",
            (restore, backup, workspaces, admin, reason),
        )
        records = []
        for workspace in workspaces:
            state = _workspace_state(cur, workspace, admin, reason)
            _version(state, state["version"])
            previous_workspace = cur.execute(
                "SELECT mode,current_freeze_id FROM ledger.workspace WHERE id=%s", (workspace,)
            ).fetchone()
            cur.execute("UPDATE ledger.workspace SET mode='RESTORE' WHERE id=%s", (workspace,))
            revoked = cur.execute(
                "UPDATE ledger.auth_credential SET "
                "revoked_at=clock_timestamp(),version=version+1 WHERE workspace_id=%s AND "
                "revoked_at IS NULL RETURNING credential_id,client_id,version",
                (workspace,),
            ).fetchall()
            if len(revoked) > 10000:
                raise AuthManagementError("RESTORE_CREDENTIAL_LIMIT")
            for credential in revoked:
                _audit(
                    cur,
                    workspace,
                    admin,
                    reason,
                    "CREDENTIAL_REVOKED",
                    {"revoked": True, "version": credential["version"], "restore_id": str(restore)},
                    before={"revoked": False, "version": credential["version"] - 1},
                    client=credential["client_id"],
                    credential=credential["credential_id"],
                )
            generation = uuid4()
            cur.execute(
                "UPDATE ledger.auth_workspace_state SET generation=%s,version=version+1 WHERE "
                "workspace_id=%s",
                (generation, workspace),
            )
            record = cur.execute(
                "INSERT INTO "
                "ledger.auth_restore_workspace(restore_id,workspace_id,previous_generation,"
                "generation,previous_mode,previous_freeze_id,previous_version,version,"
                "revoked_count) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                (
                    restore,
                    workspace,
                    state["generation"],
                    generation,
                    previous_workspace["mode"],
                    previous_workspace["current_freeze_id"],
                    state["version"],
                    state["version"] + 1,
                    len(revoked),
                ),
            ).fetchone()
            records.append(record)
    return _restore_result(restore, backup, records)


def _restore_result(restore, backup, records):
    return {
        "restore_id": str(restore),
        "backup_id": str(backup),
        "workspaces": [
            {
                "workspace_id": str(row["workspace_id"]),
                "generation": str(row["generation"]),
                "version": row["version"],
                "revoked_count": row["revoked_count"],
                "previous_mode": row["previous_mode"],
                "previous_freeze_id": (
                    None if row["previous_freeze_id"] is None else str(row["previous_freeze_id"])
                ),
            }
            for row in records
        ],
    }
