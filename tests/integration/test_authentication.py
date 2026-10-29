"""Real credentials, current permission and observed PostgreSQL revocation ordering."""

import hashlib
from dataclasses import replace
from threading import Event
from time import monotonic
from uuid import uuid4

import pytest
from psycopg import errors

from ledgerops import authentication as auth
from ledgerops.db import admission
from ledgerops.domain import ActorId
from tests.integration.concurrency_helpers import finish_call, start_call, wait_for_blocker
from tests.integration.purchase_helpers import purchase_input, retained_rows
from tests.integration.test_purchase_retries import _install_terminal_fault

pytestmark = pytest.mark.integration
ADMIN = "95000000-0000-4000-8000-000000000001"


def provision(db, principal, *, scopes=("purchase:write", "accounts:read"), ttl=3600):
    with db.connect("auth_admin") as conn:
        auth.set_membership(
            conn,
            str(principal.workspace_id),
            str(principal.actor_id),
            scopes,
            admin_actor_id=ADMIN,
            reason="test initial grants",
        )
        auth.set_client(
            conn,
            str(principal.workspace_id),
            str(principal.client_id),
            str(principal.actor_id),
            admin_actor_id=ADMIN,
            reason="test initial client",
        )
        return auth.issue_credential(
            conn,
            str(principal.workspace_id),
            str(principal.client_id),
            scopes,
            admin_actor_id=ADMIN,
            reason="test credential",
            ttl_seconds=ttl,
        )


@pytest.fixture
def identity(seeded_db, golden):
    principal, command, key = purchase_input(golden)
    issued = provision(seeded_db, principal)
    with seeded_db.connect("service") as conn:
        credential = auth.authenticate(conn, issued.token)
    assert credential.principal == principal
    return issued, credential, command, key


def purchase(conn, credential, command, key, guard=None):
    return admission.submit_purchase(
        conn,
        credential.principal,
        command,
        key,
        authorization_guard=credential.authorization_guard if guard is None else guard,
    )


def revoke(db, credential):
    with db.connect("auth_admin") as conn:
        return auth.revoke_credential(
            conn,
            str(credential.principal.workspace_id),
            str(credential.credential_id),
            admin_actor_id=ADMIN,
            reason="test revoke",
        )


def test_server_derives_principal_and_only_digest_is_retained(seeded_db, identity):
    issued, credential, command, key = identity
    assert issued.token not in repr(issued) + repr(credential)
    with seeded_db.connect("owner") as conn:
        digest, client = conn.execute(
            "SELECT token_sha256,client_id FROM ledger.auth_credential"
        ).fetchone()
        assert digest == hashlib.sha256(issued.token.encode()).digest()
        assert str(client) == str(credential.principal.client_id)
        assert issued.token not in str(conn.execute("SELECT * FROM ledger.auth_audit").fetchall())
    with seeded_db.connect("service") as conn:
        assert purchase(conn, credential, command, key).http_status == 201
        assert purchase(conn, credential, command, key).replayed
    second = None
    with seeded_db.connect("auth_admin") as conn:
        second = auth.issue_credential(
            conn,
            str(credential.principal.workspace_id),
            str(credential.principal.client_id),
            ["purchase:write"],
            admin_actor_id=ADMIN,
            reason="rotate without changing transport client",
        )
    with seeded_db.connect("service") as conn:
        rotated = auth.authenticate(conn, second.token)
        assert rotated.principal == credential.principal
        assert purchase(conn, rotated, command, key).replayed


@pytest.mark.parametrize(
    "token", ["", "fake", "lo1_" + "a" * 42, "lo1_" + "a" * 44, "lo1_" + "a" * 43, True, None]
)
def test_invalid_or_unknown_credentials_are_401_without_mutation(seeded_db, token):
    before = retained_rows(seeded_db)
    with seeded_db.connect("service") as conn, pytest.raises(auth.AuthenticationRequired):
        auth.authenticate(conn, token)
    assert retained_rows(seeded_db) == before


@pytest.mark.parametrize(
    "fault", ["revoked", "disabled_member", "missing_scope", "disabled_client", "forged_actor"]
)
def test_transaction_rechecks_stale_preauth_before_claims(seeded_db, identity, fault):
    _, credential, command, key = identity
    workspace, actor, client = map(
        str,
        (
            credential.principal.workspace_id,
            credential.principal.actor_id,
            credential.principal.client_id,
        ),
    )
    expected = auth.AuthenticationRequired
    with seeded_db.connect("auth_admin") as conn:
        if fault == "revoked":
            auth.revoke_credential(
                conn, workspace, str(credential.credential_id), admin_actor_id=ADMIN, reason=fault
            )
        elif fault in ("disabled_member", "missing_scope"):
            auth.set_membership(
                conn,
                workspace,
                actor,
                [] if fault == "missing_scope" else ["accounts:read", "purchase:write"],
                enabled=fault != "disabled_member",
                expected_version=1,
                admin_actor_id=ADMIN,
                reason=fault,
            )
            if fault == "missing_scope":
                expected = auth.PermissionDenied
        elif fault == "disabled_client":
            auth.set_client(
                conn,
                workspace,
                client,
                actor,
                enabled=False,
                expected_version=1,
                admin_actor_id=ADMIN,
                reason=fault,
            )
        else:
            credential = replace(
                credential,
                principal=replace(credential.principal, actor_id=ActorId.parse(str(uuid4()))),
            )
    before = retained_rows(seeded_db)
    with seeded_db.connect("service") as conn, pytest.raises(expected):
        purchase(conn, credential, command, key)
    assert retained_rows(seeded_db) == before


def test_revoked_token_cannot_read_even_a_retained_success(seeded_db, identity):
    _, credential, command, key = identity
    with seeded_db.connect("service") as conn:
        assert purchase(conn, credential, command, key).http_status == 201
    revoke(seeded_db, credential)
    with seeded_db.connect("service") as conn, pytest.raises(auth.AuthenticationRequired):
        purchase(conn, credential, command, key)


def test_expiry_is_checked_using_actual_database_clock_at_decision(seeded_db, golden):
    principal, command, key = purchase_input(golden)
    issued = provision(seeded_db, principal, ttl=1)
    with seeded_db.connect("service") as conn:
        credential = auth.authenticate(conn, issued.token)
        deadline = monotonic() + 4
        while not conn.execute("SELECT clock_timestamp()>%s", (issued.expires_at,)).fetchone()[0]:
            assert monotonic() < deadline
            Event().wait(0.01)
        with pytest.raises(auth.AuthenticationRequired):
            purchase(conn, credential, command, key)
    assert len(retained_rows(seeded_db)["operation"]) == 1


def test_each_definitely_aborted_retry_reauthorizes(seeded_db, identity, golden, monkeypatch):
    _, credential, command, key = identity
    _install_terminal_fault(seeded_db, golden, failures=1, sqlstate="40001")
    calls = []
    original = admission.sleep

    def revoke_between_attempts(delay):
        revoke(seeded_db, credential)
        original(delay)

    def guard(cur, principal, scope):
        calls.append(scope)
        return credential.authorization_guard(cur, principal, scope)

    monkeypatch.setattr(admission, "sleep", revoke_between_attempts)
    with seeded_db.connect("service") as conn, pytest.raises(auth.AuthenticationRequired):
        purchase(conn, credential, command, key, guard)
    assert calls == ["purchase:write", "purchase:write"]
    assert len(retained_rows(seeded_db)["operation"]) == 1


def test_admitted_transaction_holds_revocation_until_commit(seeded_db, identity):
    _, credential, command, key = identity
    entered, release = Event(), Event()

    def guard(cur, principal, scope):
        credential.authorization_guard(cur, principal, scope)
        entered.set()
        assert release.wait(4)

    with (
        seeded_db.connect("service") as app,
        seeded_db.connect("auth_admin") as admin,
        seeded_db.connect("admin") as observer,
    ):
        bought = start_call(lambda: purchase(app, credential, command, key, guard))
        try:
            assert entered.wait(3)
            revoked = start_call(
                lambda: auth.revoke_credential(
                    admin,
                    str(credential.principal.workspace_id),
                    str(credential.credential_id),
                    admin_actor_id=ADMIN,
                    reason="concurrent revocation",
                )
            )
            wait_for_blocker(
                observer,
                admin.info.backend_pid,
                app.info.backend_pid,
                revoked,
                query_fragment="pg_advisory_xact_lock",
            )
        finally:
            release.set()
        kind, result = finish_call(bought)
        assert kind == "returned" and result.http_status == 201
        assert finish_call(revoked)[0] == "returned"
        with pytest.raises(auth.AuthenticationRequired):
            purchase(app, credential, command, key)


def test_reader_waits_for_revocation_then_cannot_open_stale_snapshot(
    seeded_db, identity, monkeypatch
):
    _, credential, _, _ = identity
    entered, release = Event(), Event()
    original = auth._audit

    def pause(cur, *args, **kwargs):
        original(cur, *args, **kwargs)
        if args[3] == "CREDENTIAL_REVOKED":
            entered.set()
            assert release.wait(4)

    monkeypatch.setattr(auth, "_audit", pause)
    with seeded_db.connect("auth_admin") as admin, seeded_db.connect("admin") as observer:
        revoked = start_call(
            lambda: auth.revoke_credential(
                admin,
                str(credential.principal.workspace_id),
                str(credential.credential_id),
                admin_actor_id=ADMIN,
                reason="revoke before read",
            )
        )
        assert entered.wait(3)
        conn = seeded_db.connect("service")
        pid = conn.info.backend_pid

        def read():
            with auth.authorized_snapshot(conn, credential, "accounts:read") as cur:
                return cur.execute("SELECT 1").fetchone()

        reader = start_call(read)
        try:
            wait_for_blocker(
                observer,
                pid,
                admin.info.backend_pid,
                reader,
                query_fragment="pg_advisory_lock_shared",
            )
        finally:
            release.set()
        assert finish_call(revoked)[0] == "returned"
        kind, error = finish_call(reader)
        assert kind == "raised" and isinstance(error, auth.AuthenticationRequired)
        assert conn.closed


def test_authorized_read_exception_releases_session_lock_and_connection(seeded_db, identity):
    _, credential, _, _ = identity
    conn = seeded_db.connect("service")
    with pytest.raises(RuntimeError, match="read failed"):
        with auth.authorized_snapshot(conn, credential, "accounts:read") as cur:
            assert (
                cur.execute("SHOW transaction_read_only").fetchone()["transaction_read_only"]
                == "on"
            )
            assert (
                cur.execute("SHOW transaction_timeout").fetchone()["transaction_timeout"] == "30s"
            )
            raise RuntimeError("read failed")
    assert conn.closed
    assert revoke(seeded_db, credential)["revoked"]


@pytest.mark.parametrize("role", ["app", "worker", "reader", "service"])
def test_financial_runtime_cannot_manage_credentials(seeded_db, role):
    with seeded_db.connect(role) as conn, pytest.raises(errors.InsufficientPrivilege):
        conn.execute("DELETE FROM ledger.auth_credential")


def test_identity_and_audit_are_immutable_under_actual_admin_credentials(seeded_db, identity):
    issued, credential, _, _ = identity
    with seeded_db.connect("auth_admin") as conn:
        with pytest.raises(errors.InsufficientPrivilege):
            conn.execute(
                "UPDATE ledger.auth_credential SET token_sha256=%s WHERE credential_id=%s",
                (bytes(32), issued.credential_id),
            )
        with pytest.raises(errors.InsufficientPrivilege):
            conn.execute("UPDATE ledger.auth_audit SET reason='tamper'")
        with pytest.raises(errors.InsufficientPrivilege):
            conn.execute("UPDATE ledger.account_balance SET credits=0")
        with pytest.raises(auth.AuthManagementError, match="IMMUTABLE_CLIENT_ACTOR"):
            auth.set_client(
                conn,
                str(credential.principal.workspace_id),
                str(credential.principal.client_id),
                str(uuid4()),
                admin_actor_id=ADMIN,
                reason="cannot reassign client",
                expected_version=1,
            )
    with seeded_db.connect("owner") as conn, pytest.raises(errors.CheckViolation):
        conn.execute(
            "UPDATE ledger.auth_credential SET token_sha256=%s,version=version+1 "
            "WHERE credential_id=%s",
            (bytes(32), issued.credential_id),
        )


@pytest.mark.parametrize("route", ["payout", "reversal"])
def test_merchant_routes_use_fixed_scope_and_recheck_it_on_replay(seeded_db, golden, route):
    from tests.integration.payout_helpers import fund_merchant, payout_input, payout_policy
    from tests.integration.reversal_helpers import reversal_input

    fund_merchant(seeded_db, golden)
    principal, command, key = payout_input(golden) if route == "payout" else reversal_input(golden)
    issued = provision(seeded_db, principal, scopes=[f"{route}:write"])
    with seeded_db.connect("service") as conn:
        credential = auth.authenticate(conn, issued.token)

        def submit():
            if route == "payout":
                return admission.submit_payout(
                    conn,
                    principal,
                    command,
                    key,
                    policy=payout_policy(golden),
                    authorization_guard=credential.authorization_guard,
                )
            return admission.submit_reversal(
                conn, principal, command, key, authorization_guard=credential.authorization_guard
            )

        assert submit().http_status == (202 if route == "payout" else 201)
        assert submit().replayed
        revoke(seeded_db, credential)
        with pytest.raises(auth.AuthenticationRequired):
            submit()


def test_restore_generation_revokes_old_access_once_and_preserves_fresh_bootstrap(
    seeded_db, identity
):
    issued, credential, _, _ = identity
    args = {
        "restore_id": str(uuid4()),
        "backup_id": str(uuid4()),
        "admin_actor_id": ADMIN,
        "reason": "test restored access invalidation",
    }
    with seeded_db.connect("owner") as conn:
        result = auth.invalidate_restored_access(conn, **args)
        assert result["workspaces"][0]["revoked_count"] == 1
        assert conn.execute("SELECT mode FROM ledger.workspace").fetchone() == ("RESTORE",)
    with seeded_db.connect("service") as conn, pytest.raises(auth.AuthenticationRequired):
        auth.authenticate(conn, issued.token)
    # Use the original immutable actor/client, issue a new token in the new generation.
    with seeded_db.connect("auth_admin") as conn:
        fresh = auth.issue_credential(
            conn,
            str(credential.principal.workspace_id),
            str(credential.principal.client_id),
            ["purchase:write"],
            admin_actor_id=ADMIN,
            reason="fresh bootstrap",
        )
    with seeded_db.connect("owner") as conn:
        assert auth.invalidate_restored_access(conn, **args) == result
    with seeded_db.connect("service") as conn:
        assert auth.authenticate(conn, fresh.token).generation == fresh.generation
        with pytest.raises(auth.AuthenticationRequired):
            auth.authenticate(conn, issued.token)
    with (
        seeded_db.connect("owner") as conn,
        pytest.raises(auth.AuthManagementError, match="RESTORE_IDENTITY_CONFLICT"),
    ):
        auth.invalidate_restored_access(conn, **{**args, "reason": "changed intent"})


def test_read_holds_revocation_until_its_snapshot_finishes(seeded_db, identity):
    _, credential, _, _ = identity
    with seeded_db.connect("auth_admin") as admin, seeded_db.connect("admin") as observer:
        conn = seeded_db.connect("service")
        with auth.authorized_snapshot(conn, credential, "accounts:read") as cur:
            revoked = start_call(
                lambda: auth.revoke_credential(
                    admin,
                    str(credential.principal.workspace_id),
                    str(credential.credential_id),
                    admin_actor_id=ADMIN,
                    reason="wait for active read",
                )
            )
            wait_for_blocker(
                observer,
                admin.info.backend_pid,
                conn.info.backend_pid,
                revoked,
                query_fragment="pg_advisory_xact_lock",
            )
            assert len(cur.execute("SELECT id FROM ledger.account").fetchall()) == 4
        assert conn.closed
        assert finish_call(revoked)[0] == "returned"


def test_restore_access_invalidation_preserves_open_freeze_audit(seeded_db, identity, golden):
    from ledgerops.export import capture_snapshot
    from ledgerops.verification import verify_snapshot
    from tests.integration.test_maintenance import _freeze

    frozen = _freeze(seeded_db, golden)
    with seeded_db.connect("owner") as conn:
        before = conn.execute("SELECT * FROM ledger.maintenance_freeze").fetchall()
        result = auth.invalidate_restored_access(
            conn,
            restore_id=str(uuid4()),
            backup_id=str(uuid4()),
            admin_actor_id=ADMIN,
            reason="restore frozen database",
        )
        assert result["workspaces"][0]["previous_mode"] == "FROZEN"
        assert result["workspaces"][0]["previous_freeze_id"] == str(frozen)
        assert conn.execute("SELECT * FROM ledger.maintenance_freeze").fetchall() == before
    with seeded_db.connect("reader") as conn:
        report = verify_snapshot(capture_snapshot(conn, golden["workspace"]["id"]))
        assert report.structure_ok and report.projection_ok
