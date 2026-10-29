"""Independent signed fixtures; no production/provider signing helper is imported."""

import hashlib
import hmac
import json
from uuid import uuid4

from ledgerops.db.payout_resolution import receive_observation
from ledgerops.provider_protocol import ProviderTrust
from tests.integration.payout_helpers import NOW, admit, fund_merchant, payout_input
from tests.provider.test_process import SECRET


def prepared_payout(db, golden, *, arguments=None, policy=None):
    fund_merchant(db, golden)
    arguments = arguments or payout_input(golden)
    assert admit(db, golden, arguments, policy=policy).http_status == 202
    return arguments[1]


def trust(command):
    return ProviderTrust(command.provider_namespace, {"test-v1": SECRET})


def envelope(payload, *, secret=SECRET, signature=None):
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    calculated = hmac.new(
        secret, b"ledgerops.provider-event:v1\ntest-v1\n" + raw.encode(), hashlib.sha256
    ).hexdigest()
    return json.dumps(
        {
            "key_id": "test-v1",
            "payload": raw,
            "signature": calculated if signature is None else signature,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def event(db, command, state="SUCCEEDED", *, event_id=None, sequence=1, changes=None):
    with db.connect("reader") as conn:
        key, raw = conn.execute(
            "SELECT provider_key,provider_request FROM ledger.payout "
            "WHERE workspace_id=%s AND operation_id=%s",
            (str(command.workspace_id), str(command.operation_id)),
        ).fetchone()
    payload = {
        "kind": "execution",
        "issuer": "ledgerops-provider-sim",
        "contract_version": 1,
        "namespace": str(command.provider_namespace),
        "provider_key": key,
        "event_id": str(event_id or uuid4()),
        "source_sequence": sequence,
        "state": state,
        "request_sha256": hashlib.sha256(raw).hexdigest(),
        "request": json.loads(raw),
        **(changes or {}),
    }
    return envelope(payload)


def lookup(db, command):
    with db.connect("reader") as conn:
        key = conn.execute(
            "SELECT provider_key FROM ledger.payout WHERE operation_id=%s",
            (str(command.operation_id),),
        ).fetchone()[0]
    return envelope(
        {
            "kind": "lookup",
            "issuer": "ledgerops-provider-sim",
            "contract_version": 1,
            "namespace": str(command.provider_namespace),
            "provider_key": key,
            "observation_id": str(uuid4()),
            "state": "NOT_FOUND",
            "visibility_watermark": 0,
        }
    )


def observe(db, command, raw, *, channel="CALLBACK", now=NOW):
    with db.connect("worker") as conn:
        return receive_observation(
            conn,
            command.workspace_id,
            command.operation_id,
            raw,
            trust=trust(command),
            channel=channel,
            clock=lambda: now,
        )


def financial_state(db):
    tables = {
        "account_balance": "workspace_id,account_id",
        "journal": "workspace_id,journal_id",
        "journal_entry": "workspace_id,journal_id,line_no",
        "reservation": "workspace_id,payout_id",
        "reservation_event": "workspace_id,payout_id,event_no",
        "operation": "workspace_id,operation_id",
        "request_result": "workspace_id,client_id,route_family,key",
    }
    with db.connect("reader") as conn, conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        return {
            table: conn.execute(f"SELECT * FROM ledger.{table} ORDER BY {order}").fetchall()
            for table, order in tables.items()
        }


def raw_table(db, table):
    with db.connect("reader") as conn:
        return conn.execute(f"SELECT * FROM ledger.{table}").fetchall()


def resolution_state(db):
    from tests.integration.payout_helpers import payout_state

    result = payout_state(db)
    tables = {
        "provider_observation": "observation_id",
        "observation_receipt": "receipt_id",
        "observation_quarantine": "quarantine_id",
        "payout_incident": "incident_id",
        "payout_resolution": "workspace_id,payout_id",
    }
    with db.connect("reader") as conn, conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        result.update(
            {
                table: conn.execute(f"SELECT * FROM ledger.{table} ORDER BY {order}").fetchall()
                for table, order in tables.items()
            }
        )
    return result
