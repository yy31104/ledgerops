"""Reproduce three payout incidents and record what an operator can see today.

Run from the repository root with the locked LedgerOps environment and Docker:

    uv run --locked python -m pytest -p tests.conftest -q assistant/scenarios/capture.py

Each scenario builds a fresh database with the repository's own disposable PostgreSQL
fixtures, drives the real payout code, then reads the result through the real HTTP
service with scoped tokens. The normalized responses are written to
``assistant/scenarios/snapshots``. Set ``LEDGEROPS_SCENARIOS_CHECK=1`` to compare a fresh
capture with the committed files instead of rewriting them.

The snapshots are the baseline for the operator assistant: they show the evidence an
operator works from without AI, and later they become recorded responses for the
assistant's read-only tools.
"""

import json
import os
import re
from pathlib import Path

import pytest

from ledgerops.db.admission import Principal
from ledgerops.db.payout_dispatch import dispatch_once
from ledgerops.domain import ActorId, ClientId, WorkspaceId
from ledgerops.export import export_workspace
from ledgerops.provider_client import ProviderHTTPClient
from ledgerops.reconciliation import StatementTrust, reconcile_bundle
from tests.integration.helpers import post_fixture, seed_accounts
from tests.integration.http_helpers import ProcessService
from tests.integration.payout_helpers import (
    BENEFICIARY,
    NAMESPACE,
    NOW,
    PAYOUT_ID,
    admit,
    fund_merchant,
    money,
    payout_input,
    payout_policy,
)
from tests.integration.payout_observation_helpers import trust
from tests.integration.purchase_helpers import fund_customer
from tests.integration.test_authentication import provision
from tests.integration.test_settlement_reconciliation import emit_statement
from tests.provider.test_process import SECRET, TOKEN, ProcessProvider

pytestmark = pytest.mark.integration

SNAPSHOTS = Path(__file__).resolve().parent / "snapshots"
SECOND_PAYOUT_ID = "40000000-0000-4000-8000-000000000004"
OPERATOR_ACTOR = "20000000-0000-4000-8000-000000000001"
OPERATOR_CLIENT = "60000000-0000-4000-8000-0000000000aa"
OPERATOR_SCOPES = ["evidence:read"]
MERCHANT_SCOPES = ["payout:read", "operations:read", "accounts:read"]

_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})?$")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_DIGEST = re.compile(r"^[0-9a-f]{40,64}$")
_FIXED_DAY = NOW.date().isoformat()
# Wall-clock values, plus next_due_at, which carries random retry jitter.
_VOLATILE_FIELDS = {"business_date", "oldest_unresolved_seconds", "next_due_at"}


@pytest.fixture
def seeded(db, golden):
    seed_accounts(db, golden)
    post_fixture(db, golden, "OPENING")
    return db


def _stable_uuids(golden):
    found = set()

    def walk(value):
        if isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, str) and _UUID.match(value):
            found.add(value)

    walk(golden)
    return found | {
        NAMESPACE,
        BENEFICIARY,
        PAYOUT_ID,
        SECOND_PAYOUT_ID,
        OPERATOR_ACTOR,
        OPERATOR_CLIENT,
        "60000000-0000-4000-8000-000000000003",
    }


class Normalizer:
    """Replace run-specific values with numbered placeholders, keeping equality visible."""

    def __init__(self, stable):
        self.stable = stable
        self.seen = {}

    def _placeholder(self, kind, value):
        key = (kind, value)
        if key not in self.seen:
            count = sum(1 for seen_kind, _ in self.seen if seen_kind == kind) + 1
            self.seen[key] = f"<{kind}:{count}>"
        return self.seen[key]

    def __call__(self, value, field=""):
        if isinstance(value, dict):
            return {name: self(item, name) for name, item in value.items()}
        if isinstance(value, list):
            return [self(item, field) for item in value]
        if not isinstance(value, str):
            return value
        if field in {"code_revision", "revision", "matcher_revision", "source_revision"}:
            return "<git-revision>"
        if field in _VOLATILE_FIELDS:
            return f"<{field}>"
        # Times derived from the fixed scenario clock (2026-10-29) are kept; database and
        # wall-clock times change on every run.
        if _TIMESTAMP.match(value) and not value.startswith(_FIXED_DAY):
            return self._placeholder("time", value)
        if _UUID.match(value) and value not in self.stable:
            return self._placeholder("uuid", value)
        if _DIGEST.match(value):
            return self._placeholder("digest", value)
        if value.startswith("lo1_"):
            return "<token>"
        return value


def _http(process, path, token):
    status, raw, _ = process.request("GET", path, token=token)
    try:
        body = json.loads(raw)
    except ValueError:
        body = raw.decode(errors="replace")
    return {"request": f"GET {path}", "status": status, "body": body}


def _operator(db, golden):
    principal = Principal(
        WorkspaceId.parse(golden["workspace"]["id"]),
        ClientId.parse(OPERATOR_CLIENT),
        ActorId.parse(OPERATOR_ACTOR),
    )
    return provision(db, principal, scopes=OPERATOR_SCOPES).token


def _write(name, golden, snapshot):
    normalized = Normalizer(_stable_uuids(golden))(snapshot)
    text = json.dumps(normalized, indent=2, sort_keys=False) + "\n"
    target = SNAPSHOTS / f"{name}.json"
    if os.environ.get("LEDGEROPS_SCENARIOS_CHECK") == "1":
        assert target.exists(), f"missing committed snapshot {target.name}"
        assert target.read_text() == text, f"{target.name} no longer matches a fresh capture"
    else:
        SNAPSHOTS.mkdir(exist_ok=True)
        target.write_text(text)


def test_limit_exceeded(seeded, golden, tmp_path):
    """Two merchants each ask for 62 while clearing can cover 100 in total."""
    db = seeded
    fund_merchant(db, golden)
    second = golden["accounts"][3]
    fund_customer(db, golden, 80, customer_account_id=second["id"])
    with db.connect("owner") as conn:
        # Same setup as tests/integration/test_payout_clearing_commitments.py: the clearing
        # projection is set to 100 while the two merchants hold 160 between them, so only
        # one 62 hold fits. A healthy journal would not produce this projection by itself.
        conn.execute(
            "UPDATE ledger.account_balance SET debits=100,version=version+1 WHERE account_id=%s",
            (golden["accounts"][0]["id"],),
        )
    first = admit(db, golden)
    assert first.http_status == 202
    second_args = payout_input(
        golden,
        key="m2-payout-60",
        operation_id=SECOND_PAYOUT_ID,
        merchant_account_id=second["id"],
        actor_id=second["owner_id"],
    )
    policy = payout_policy(
        golden, beneficiary_owners={second_args[1].beneficiary_id: second_args[0].actor_id}
    )
    refused = admit(db, golden, second_args, policy=policy)
    assert refused.http_status == 422 and refused.body["error"]["code"] == "LIMIT_EXCEEDED"

    merchant = provision(db, second_args[0], scopes=MERCHANT_SCOPES).token
    operator = _operator(db, golden)
    process = ProcessService(db, tmp_path / "service")
    try:
        views = [
            _http(process, f"/v1/operations/{SECOND_PAYOUT_ID}", merchant),
            _http(process, f"/v1/payouts/{SECOND_PAYOUT_ID}", merchant),
            _http(process, "/v1/accounts", merchant),
            _http(process, f"/v1/investigation/operations/{SECOND_PAYOUT_ID}", operator),
            _http(process, f"/v1/investigation/operations/{PAYOUT_ID}", operator),
            _http(process, "/v1/metrics", operator),
        ]
    finally:
        process.close()
    _write(
        "limit_exceeded",
        golden,
        {
            "scenario": "limit_exceeded",
            "summary": (
                "Merchant M2 asks for a payout of 60 + fee 2. Merchant M1 already holds 62 "
                "against a clearing capacity of 100, so admission refuses M2 with a retained "
                "422 LIMIT_EXCEEDED and creates no hold."
            ),
            "setup": [
                "Opening: customer 100. Purchase 80 to M1. Opening 80 to M2.",
                "Clearing projection set to 100 (deliberately short; see capture.py).",
                "M1 payout 60 + 2 admitted: 202, hold 62.",
                "M2 payout 60 + 2 refused: 422 LIMIT_EXCEEDED, no hold.",
            ],
            "admission_receipts": {
                PAYOUT_ID: {"http_status": first.http_status, "body": first.body},
                SECOND_PAYOUT_ID: {"http_status": refused.http_status, "body": refused.body},
            },
            "operator_views": views,
        },
    )


def test_dispatch_timeout(seeded, golden, tmp_path):
    """The worker sends the payout and the provider call times out."""
    db = seeded
    fund_merchant(db, golden)
    principal, command, _ = payout_input(golden)
    admitted = admit(db, golden)
    assert admitted.http_status == 202

    class TimingOutProvider:
        def put(self, key, raw):
            raise TimeoutError("synthetic provider timeout")

        def lookup(self, key):
            raise TimeoutError("synthetic provider timeout")

        def close(self, key, raw):
            raise TimeoutError("synthetic provider timeout")

    with db.connect("worker") as conn:
        result = dispatch_once(
            conn,
            command.workspace_id,
            command.operation_id,
            TimingOutProvider(),
            trust=trust(command),
            clock=lambda: NOW,
        )
    assert result.outcome == "TIMEOUT"
    assert tuple(money(db, golden)[:3]) == (80, 62, 18)

    merchant = provision(db, principal, scopes=MERCHANT_SCOPES).token
    operator = _operator(db, golden)
    process = ProcessService(db, tmp_path / "service")
    try:
        views = [
            _http(process, f"/v1/payouts/{PAYOUT_ID}", merchant),
            _http(process, "/v1/accounts", merchant),
            _http(process, f"/v1/investigation/operations/{PAYOUT_ID}", operator),
            _http(process, "/v1/metrics", operator),
        ]
    finally:
        process.close()
    _write(
        "dispatch_timeout",
        golden,
        {
            "scenario": "dispatch_timeout",
            "summary": (
                "Merchant M1's payout of 60 + 2 is admitted and claimed by the worker. The "
                "worker records MAY_HAVE_SENT, sends PUT, and the call times out. Nobody knows "
                "whether the provider paid, so the 62 stays held."
            ),
            "setup": [
                "Opening: customer 100. Purchase 80 to M1.",
                "M1 payout 60 + 2 admitted: 202, hold 62.",
                "Worker claim commits MAY_HAVE_SENT, then PUT raises a timeout.",
            ],
            "worker_result": {"outcome": result.outcome},
            "merchant_balance": {
                name: str(value)
                for name, value in zip(
                    ("booked", "reserved", "available"), money(db, golden)[:3], strict=True
                )
            },
            "operator_views": views,
        },
    )


def test_reconciliation_missing_internal(seeded, golden, tmp_path):
    """The provider paid, the ledger never saw the result, and reconciliation notices."""
    db = seeded
    fund_merchant(db, golden)
    principal, command, _ = payout_input(golden)
    assert admit(db, golden).http_status == 202
    provider = ProcessProvider(tmp_path / "provider", namespace=str(command.provider_namespace))
    provider.directory.mkdir()
    provider.start()
    try:
        real = ProviderHTTPClient(provider.ready["url"], TOKEN)

        class LostResponseProvider:
            """The PUT reaches the provider and pays; the reply never reaches the worker."""

            def put(self, key, raw):
                assert real.put(key, raw).http_status == 200
                raise TimeoutError("synthetic lost response")

            def lookup(self, key):
                raise TimeoutError("synthetic lost response")

            def close(self, key, raw):
                raise TimeoutError("synthetic lost response")

        with db.connect("worker") as conn:
            result = dispatch_once(
                conn,
                command.workspace_id,
                command.operation_id,
                LostResponseProvider(),
                trust=trust(command),
                clock=lambda: NOW,
            )
        assert result.outcome == "TIMEOUT"
        assert tuple(money(db, golden)[:3]) == (80, 62, 18)
        statement, _, _ = emit_statement(provider, command, tmp_path)
    finally:
        provider.stop()
    with db.connect("reader") as conn:
        export = export_workspace(
            conn,
            str(command.workspace_id),
            tmp_path / "exports",
            code_revision="0" * 40,
            dirty=True,
        )
    report = reconcile_bundle(
        export,
        statement,
        trust=StatementTrust(str(command.provider_namespace), {"test-v1": SECRET}),
        code_revision="0" * 40,
        dirty=True,
    )
    categories = {issue["category"] for issue in report["issues"]}
    assert not report["reconciled"] and "MISSING_INTERNAL" in categories

    merchant = provision(db, principal, scopes=MERCHANT_SCOPES).token
    operator = _operator(db, golden)
    process = ProcessService(db, tmp_path / "service")
    try:
        views = [
            _http(process, f"/v1/payouts/{PAYOUT_ID}", merchant),
            _http(process, f"/v1/investigation/operations/{PAYOUT_ID}", operator),
            _http(process, "/v1/metrics", operator),
        ]
    finally:
        process.close()
    _write(
        "reconciliation_missing_internal",
        golden,
        {
            "scenario": "reconciliation_missing_internal",
            "summary": (
                "Same timeout as dispatch_timeout, except the PUT did reach the provider and it "
                "paid 62. The reply was lost, so the ledger still holds 62 with an unknown "
                "outcome. The provider's signed settlement statement includes the payment, and "
                "the offline reconciliation report flags MISSING_INTERNAL."
            ),
            "setup": [
                "Opening: customer 100. Purchase 80 to M1.",
                "M1 payout 60 + 2 admitted: 202, hold 62.",
                "Worker claim commits MAY_HAVE_SENT; PUT reaches the provider and pays 62.",
                "The provider's reply is lost: the worker records TIMEOUT, hold stays 62.",
                "Provider issues a signed settlement statement; ledger export is matched offline.",
            ],
            "worker_result": {"outcome": result.outcome},
            "reconciliation_report": report,
            "operator_views": views,
        },
    )
