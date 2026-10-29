"""Payout fixtures and raw observations, with literal financial expectations."""

from datetime import UTC, datetime
from uuid import uuid4

from ledgerops.db.admission import Principal, submit_payout
from ledgerops.domain import (
    ActorId,
    BeneficiaryId,
    ClientId,
    PayoutCommand,
    ProviderNamespaceId,
    RequestKey,
    WorkspaceId,
)
from ledgerops.domain.payouts import TrustedPayoutPolicy
from tests.integration.purchase_helpers import invoke, purchase_input

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
NAMESPACE = "00000000-0000-4000-8000-000000000100"
BENEFICIARY = "00000000-0000-4000-8000-000000000103"
PAYOUT_ID = "40000000-0000-4000-8000-000000000003"


def payout_input(golden, *, key="payout-60", actor_id=None, **changes):
    workspace = WorkspaceId.parse(golden["workspace"]["id"])
    principal = Principal(
        workspace,
        ClientId.parse("60000000-0000-4000-8000-000000000003"),
        ActorId.parse(actor_id or golden["purchase_request"]["merchant_id"]),
    )
    command = PayoutCommand.from_payload(
        workspace,
        {
            "operation_id": PAYOUT_ID,
            "merchant_account_id": golden["purchase_request"]["merchant_account_id"],
            "beneficiary_id": BENEFICIARY,
            "provider_namespace": NAMESPACE,
            "principal_minor": "60",
            "fee_minor": "2",
            "currency": "PLN",
            "contract_version": 1,
            **changes,
        },
    )
    return principal, command, RequestKey(workspace, principal.client_id, key, "payout:v1")


def payout_policy(golden, **changes):
    return TrustedPayoutPolicy(
        **{
            "workspace_id": WorkspaceId.parse(golden["workspace"]["id"]),
            "provider_namespace": ProviderNamespaceId.parse(NAMESPACE),
            "fixed_fee_minor": 2,
            "dispatch_ttl_seconds": 60,
            "beneficiary_owners": {
                BeneficiaryId.parse(BENEFICIARY): ActorId.parse(
                    golden["purchase_request"]["merchant_id"]
                )
            },
            **changes,
        }
    )


def fund_merchant(db, golden):
    assert invoke(db, *purchase_input(golden)).http_status == 201


def admit(db, golden, arguments=None, *, policy=None, now=NOW):
    with db.connect("app") as conn:
        return submit_payout(
            conn,
            *(arguments or payout_input(golden)),
            policy=payout_policy(golden) if policy is None else policy,
            clock=lambda: now,
        )


def money(db, golden):
    with db.connect("reader") as conn:
        return conn.execute(
            "SELECT booked,reserved,booked-reserved,version FROM ledger.account_balance "
            "WHERE workspace_id=%s AND account_id=%s",
            (golden["workspace"]["id"], golden["purchase_request"]["merchant_account_id"]),
        ).fetchone()


def payout_state(db):
    orders = {
        "workspace": "id",
        "operation": "workspace_id,operation_id",
        "request_result": "workspace_id,client_id,route_family,key",
        "journal": "workspace_id,journal_id",
        "journal_entry": "workspace_id,journal_id,line_no",
        "account_balance": "workspace_id,account_id",
        "payout": "workspace_id,operation_id",
        "reservation": "workspace_id,payout_id",
        "reservation_event": "workspace_id,payout_id,event_no",
        "dispatch_intent": "workspace_id,payout_id",
        "transport_attempt": "workspace_id,attempt_id",
    }
    with db.connect("reader") as conn, conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        return {
            table: conn.execute(f"SELECT * FROM ledger.{table} ORDER BY {order}").fetchall()
            for table, order in orders.items()
        }


def fresh_input(golden, **changes):
    return payout_input(golden, key=str(uuid4()), operation_id=str(uuid4()), **changes)
