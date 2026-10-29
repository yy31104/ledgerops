"""Authenticate the exact callback payload before database lookup, then reuse resolution."""

import hashlib
import hmac
import re

import psycopg

from ledgerops.db.payout_resolution import receive_observation
from ledgerops.worker import ConfigurationError as WorkerConfigurationError
from ledgerops.worker import _validate_database


class CallbackUnavailable(RuntimeError):
    pass


def authentic(envelope, trust):
    if type(envelope) is not dict or envelope.keys() != {"key_id", "payload", "signature"}:
        return False
    key_id, payload, signature = (envelope[name] for name in ("key_id", "payload", "signature"))
    if (
        type(key_id) is not str
        or type(payload) is not str
        or type(signature) is not str
        or re.fullmatch("[0-9a-f]{64}", signature) is None
    ):
        return False
    secret = trust.keys.get(key_id)
    if secret is None:
        return False
    try:
        message = (
            b"ledgerops.provider-event:v1\n" + key_id.encode() + b"\n" + payload.encode("utf-8")
        )
    except UnicodeError:
        return False
    return hmac.compare_digest(hmac.new(secret, message, hashlib.sha256).hexdigest(), signature)


def connect(config):
    conn = psycopg.connect(
        config.callback_dsn,
        autocommit=True,
        connect_timeout=3,
        application_name="ledgerops-callback",
        options=(
            "-c timezone=UTC -c statement_timeout=10000 -c lock_timeout=5000 "
            "-c transaction_timeout=30000 -c idle_in_transaction_session_timeout=15000"
        ),
    )
    try:
        try:
            _validate_database(conn)
        except WorkerConfigurationError as exc:
            raise CallbackUnavailable("CALLBACK_DATABASE_IDENTITY_INVALID") from exc
        if conn.execute("SELECT pg_has_role(current_user,'ledger_auth_admin','MEMBER')").fetchone()[
            0
        ]:
            raise CallbackUnavailable("CALLBACK_DATABASE_IDENTITY_INVALID")
        return conn
    except BaseException:
        conn.close()
        raise


def receive(config, workspace, payout_id, raw):
    with connect(config) as conn:
        return receive_observation(
            conn, workspace, payout_id, raw, trust=config.callback_trust, channel="CALLBACK"
        )
