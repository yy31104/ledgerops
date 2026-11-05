"""Narrow typed operator adapter; no caller-selected role, trust or SQL."""

import hashlib
import json

import psycopg

from ledgerops.authentication import authenticate
from ledgerops.db.admission import AdmissionError, InvariantViolation
from ledgerops.db.operator_actions import (
    ACTION_TYPE,
    action_state,
    approve_action,
    execute_action,
    propose_action,
)
from ledgerops.reconciliation.publication import ObjectStore


class OperatorUnavailable(AdmissionError):
    http_status = 503
    code = "OPERATOR_UNAVAILABLE"


def connect(config):
    conn = psycopg.connect(
        config.operator_dsn,
        autocommit=True,
        connect_timeout=3,
        application_name="ledgerops-operator",
        options=(
            "-c timezone=UTC -c statement_timeout=10000 -c lock_timeout=5000 "
            "-c transaction_timeout=30000 -c idle_in_transaction_session_timeout=15000"
        ),
    )
    try:
        row = conn.execute(
            "SELECT r.rolsuper,r.rolbypassrls,r.rolcreatedb,r.rolcreaterole,r.rolreplication,"
            "pg_has_role(current_user,'ledger_operator','USAGE'),"
            "pg_has_role(current_user,'ledger_auth_admin','MEMBER'),"
            "pg_has_role(current_user,'ledger_migrator','MEMBER'),"
            "pg_has_role(current_user,'ledger_app','MEMBER') "
            "FROM pg_roles r WHERE r.rolname=current_user"
        ).fetchone()
        if row is None or any(row[:5]) or not row[5] or any(row[6:]):
            raise OperatorUnavailable()
        conn.execute("SELECT action_id FROM ledger.operator_action LIMIT 0")
        return conn
    except BaseException:
        conn.close()
        raise


def present(value):
    """Keep hashed canonical JSON exact; the browser must not round its integers."""
    result = dict(value)
    proposal = result.pop("proposal", None)
    if proposal is not None:
        raw = json.dumps(proposal, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        if hashlib.sha256(raw).hexdigest() != result["proposal_sha256"]:
            raise InvariantViolation("proposal serialization differs from retained bytes")
        result["proposal_json"] = raw.decode()
        payout = proposal["target"]["payout"]
        result["review"] = {
            name: str(proposal[name])
            for name in (
                "action_type",
                "payout_id",
                "observation_id",
                "evidence_run_id",
                "reason",
                "created_at",
                "expires_at",
            )
        }
        result["review"].update(
            {
                name: str(payout[name])
                for name in (
                    "provider_key",
                    "merchant_account_id",
                    "principal_minor",
                    "fee_minor",
                    "gross_minor",
                    "currency",
                )
            }
        )
        result["review"]["maker_actor_id"] = proposal["maker"]["actor_id"]
        result["review"]["evidence_epoch"] = str(proposal["evidence"]["evidence_epoch"])
        result["review"]["incident_ids"] = proposal["incident_ids"]
    return result


def invoke(config, environ, path, method, payload, identity):
    header = environ.get("HTTP_AUTHORIZATION", "")
    if not header.startswith("Bearer ") or len(header) > 128:
        from ledgerops.authentication import AuthenticationRequired

        raise AuthenticationRequired()
    store = ObjectStore(config.operator_objects)
    conn = connect(config)
    try:
        credential = authenticate(conn, header[7:])
        common = {"store": store, "trust": config.operator_trust}
        if path == "/v1/operator/actions" and method == "POST":
            fields = {
                "action_id",
                "action_type",
                "payout_id",
                "observation_id",
                "evidence_run_id",
                "reason",
                "ttl_seconds",
            }
            if (
                type(payload) is not dict
                or payload.keys() != fields
                or payload["action_type"] != ACTION_TYPE
            ):
                raise ValueError("typed reprocessing action required")
            result = propose_action(
                conn,
                credential,
                **{
                    name: identity(payload[name])
                    for name in ("action_id", "payout_id", "observation_id", "evidence_run_id")
                },
                reason=payload["reason"],
                ttl_seconds=payload["ttl_seconds"],
                **common,
            )
            return result.http_status, present(result.body), "operator_propose"
        suffix = path.removeprefix("/v1/operator/actions/").split("/")
        if not 1 <= len(suffix) <= 2:
            raise ValueError("unknown operator route")
        action = identity(suffix[0])
        if len(suffix) == 1 and method == "GET":
            return 200, present(action_state(conn, credential, action_id=action)), "operator_read"
        if method != "POST" or len(suffix) != 2 or type(payload) is not dict:
            raise ValueError("unknown operator route")
        if suffix[1] == "approve" and payload.keys() == {"proposal_sha256"}:
            result = approve_action(
                conn,
                credential,
                action_id=action,
                proposal_sha256=payload["proposal_sha256"],
                **common,
            )
            return result.http_status, result.body, "operator_approve"
        if suffix[1] == "execute" and payload.keys() == {"proposal_sha256", "execution_id"}:
            result = execute_action(
                conn,
                credential,
                action_id=action,
                execution_id=identity(payload["execution_id"]),
                proposal_sha256=payload["proposal_sha256"],
                **common,
            )
            return result.http_status, result.body, "operator_execute"
        raise ValueError("unknown operator route or fields")

    finally:
        conn.close()
