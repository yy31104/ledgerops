"""Authenticated terminal evidence changes exactly one held payout on real PostgreSQL."""

import json
from uuid import uuid4

import pytest

from ledgerops.db.admission import get_payout_state
from ledgerops.db.payout_dispatch import claim_payout
from ledgerops.export import capture_snapshot
from ledgerops.verification import verify_snapshot
from tests.integration.payout_helpers import (
    NOW,
    admit,
    money,
    payout_input,
)
from tests.integration.payout_observation_helpers import (
    envelope,
    event,
    financial_state,
    lookup,
    observe,
    prepared_payout,
    raw_table,
)
from tests.integration.test_maintenance import ACTOR, _apply, _freeze, _preview

pytestmark = pytest.mark.integration


def check_final(db, golden, command, state):
    success = state == "SUCCEEDED"
    with db.connect("reader") as conn:
        assert conn.execute(
            "SELECT booked FROM ledger.account_balance ORDER BY account_id"
        ).fetchall() == ([(38,), (20,), (18,), (0,)] if success else [(100,), (20,), (80,), (0,)])
        assert conn.execute(
            "SELECT gross_minor,state,version FROM ledger.reservation WHERE payout_id=%s",
            (str(command.operation_id),),
        ).fetchone() == (62, "CONSUMED" if success else "RELEASED", 2)
        assert conn.execute(
            "SELECT state FROM ledger.reservation_event WHERE payout_id=%s ORDER BY event_no",
            (str(command.operation_id),),
        ).fetchall() == [("HELD",), ("CONSUMED" if success else "RELEASED",)]
        assert conn.execute(
            "SELECT state FROM ledger.dispatch_intent WHERE payout_id=%s",
            (str(command.operation_id),),
        ).fetchone() == ("DONE",)
        assert conn.execute(
            "SELECT knowledge FROM ledger.payout WHERE operation_id=%s",
            (str(command.operation_id),),
        ).fetchone() == (state,)
        journals = conn.execute(
            "SELECT journal_id,total_minor,line_count FROM ledger.journal WHERE operation_id=%s",
            (str(command.operation_id),),
        ).fetchall()
        if success:
            assert len(journals) == 1 and journals[0][1:] == (62, 2)
            assert conn.execute(
                "SELECT account_id::text,side,amount_minor FROM ledger.journal_entry "
                "WHERE journal_id=%s ORDER BY account_id",
                (journals[0][0],),
            ).fetchall() == [
                (golden["accounts"][0]["id"], "C", 62),
                (golden["purchase_request"]["merchant_account_id"], "D", 62),
            ]
        else:
            assert journals == []
    assert money(db, golden)[:3] == ((18, 0, 18) if success else (80, 0, 80))


@pytest.mark.parametrize("state", ["SUCCEEDED", "FAILED", "CLOSED_ABSENT"])
def test_authoritative_terminal_finishes_hold_and_keeps_admission_receipt_unchanged(
    seeded_db, golden, state
):
    command = prepared_payout(seeded_db, golden)
    admission = admit(seeded_db, golden)
    before = financial_state(seeded_db)
    result = observe(seeded_db, command, event(seeded_db, command, state))
    assert result.status == "APPLIED" and result.state == state
    check_final(seeded_db, golden, command, state)
    after = financial_state(seeded_db)
    assert (
        after["operation"] == before["operation"]
        and after["request_result"] == before["request_result"]
    )
    replay = admit(seeded_db, golden)
    assert (replay.http_status, replay.body, replay.replayed) == (202, admission.body, True)
    with seeded_db.connect("app") as conn:
        current = get_payout_state(conn, payout_input(golden)[0], command.operation_id)
    assert current["knowledge"] == state


@pytest.mark.parametrize("channel", ["CALLBACK", "PUT", "LOOKUP", "CLOSE", "HISTORY"])
def test_identical_event_across_receiving_channels_does_not_repeat_money(
    seeded_db, golden, channel
):
    command = prepared_payout(seeded_db, golden)
    raw = event(seeded_db, command)
    assert observe(seeded_db, command, raw).status == "APPLIED"
    before = financial_state(seeded_db)
    assert observe(seeded_db, command, raw, channel=channel).status == "REPLAYED"
    assert financial_state(seeded_db) == before
    check_final(seeded_db, golden, command, "SUCCEEDED")


@pytest.mark.parametrize("state", ["ACCEPTED", "NOT_FOUND"])
def test_nonterminal_observation_never_consumes_or_releases_funds(seeded_db, golden, state):
    command = prepared_payout(seeded_db, golden)
    before = financial_state(seeded_db)
    raw = lookup(seeded_db, command) if state == "NOT_FOUND" else event(seeded_db, command, state)
    assert observe(seeded_db, command, raw).status == "OBSERVED"
    assert financial_state(seeded_db) == before
    assert money(seeded_db, golden)[:3] == (80, 62, 18)


def test_old_nonterminal_cannot_downgrade_a_committed_success(seeded_db, golden):
    command = prepared_payout(seeded_db, golden)
    observe(seeded_db, command, event(seeded_db, command, sequence=10))
    before = financial_state(seeded_db)
    result = observe(seeded_db, command, event(seeded_db, command, "ACCEPTED", sequence=2))
    assert result.status in {"OBSERVED", "REPLAYED"}
    assert financial_state(seeded_db) == before
    check_final(seeded_db, golden, command, "SUCCEEDED")


@pytest.mark.parametrize(
    "first,second",
    [("SUCCEEDED", "FAILED"), ("FAILED", "SUCCEEDED"), ("CLOSED_ABSENT", "SUCCEEDED")],
)
def test_older_conflicting_terminal_is_retained_and_stops_automatic_work_without_reversing_money(
    seeded_db, golden, first, second
):
    command = prepared_payout(seeded_db, golden)
    assert (
        observe(seeded_db, command, event(seeded_db, command, first, sequence=10)).status
        == "APPLIED"
    )
    before = financial_state(seeded_db)
    result = observe(seeded_db, command, event(seeded_db, command, second, sequence=2))
    assert result.status == "INCIDENT"
    assert financial_state(seeded_db) == before
    check_final(seeded_db, golden, command, first)
    with seeded_db.connect("worker") as conn:
        assert (
            claim_payout(conn, command.workspace_id, command.operation_id, clock=lambda: NOW)
            is None
        )
    assert raw_table(seeded_db, "payout_incident")


def test_same_authenticated_event_id_with_changed_bytes_is_not_silently_deduplicated(
    seeded_db, golden
):
    command = prepared_payout(seeded_db, golden)
    identifier = str(uuid4())
    observe(
        seeded_db, command, event(seeded_db, command, "ACCEPTED", event_id=identifier, sequence=1)
    )
    before = financial_state(seeded_db)
    changed = event(seeded_db, command, "SUCCEEDED", event_id=identifier, sequence=2)
    assert observe(seeded_db, command, changed).status == "INCIDENT"
    assert financial_state(seeded_db) == before
    assert money(seeded_db, golden)[:3] == (80, 62, 18)


@pytest.mark.parametrize(
    "fault",
    [
        "signature",
        "provider_key",
        "namespace",
        "merchant",
        "beneficiary",
        "amount",
        "digest",
        "issuer",
    ],
)
def test_invalid_observation_cannot_change_money_or_disable_legitimate_payout(
    seeded_db, golden, fault
):
    command = prepared_payout(seeded_db, golden)
    raw = event(seeded_db, command)
    payload = json.loads(json.loads(raw)["payload"])
    if fault == "signature":
        corrupted = envelope(payload, signature="0" * 64)
    else:
        if fault in {"provider_key", "namespace", "issuer"}:
            payload[fault] = str(uuid4())
        elif fault == "digest":
            payload["request_sha256"] = "0" * 64
        else:
            request = dict(payload["request"])
            key = {
                "merchant": "merchant_id",
                "beneficiary": "beneficiary_id",
                "amount": "gross_minor",
            }[fault]
            request[key] = "63" if fault == "amount" else str(uuid4())
            payload["request"] = request
        corrupted = envelope(payload)
    before = financial_state(seeded_db)
    result = observe(seeded_db, command, corrupted)
    assert result.status == "QUARANTINED"
    assert financial_state(seeded_db) == before
    assert raw_table(seeded_db, "payout_incident") == []
    assert observe(seeded_db, command, raw).status == "APPLIED"
    check_final(seeded_db, golden, command, "SUCCEEDED")


def test_disabled_merchant_still_records_an_already_executed_external_success(seeded_db, golden):
    command = prepared_payout(seeded_db, golden)
    with seeded_db.connect("owner") as conn:
        conn.execute(
            "UPDATE ledger.account SET enabled=false WHERE id=%s",
            (str(command.merchant_account_id),),
        )
    assert observe(seeded_db, command, event(seeded_db, command)).status == "APPLIED"
    check_final(seeded_db, golden, command, "SUCCEEDED")


def test_repair_active_hold_then_settle_preserves_older_fold2_reserved_audit(
    seeded_db, golden, tmp_path
):
    command = prepared_payout(seeded_db, golden)
    with seeded_db.connect("owner") as conn:
        conn.execute(
            "UPDATE ledger.account_balance SET reserved=12 WHERE account_id=%s",
            (str(command.merchant_account_id),),
        )
    freeze = _freeze(seeded_db, golden)
    _preview(seeded_db, golden, freeze, tmp_path)
    _apply(seeded_db, golden, freeze)
    from ledgerops.db.maintenance import resume_workspace

    with seeded_db.connect("maintenance") as conn:
        resume_workspace(conn, str(command.workspace_id), freeze, ACTOR, "repaired hold")
    observe(seeded_db, command, event(seeded_db, command))
    check_final(seeded_db, golden, command, "SUCCEEDED")
    with seeded_db.connect("reader") as conn:
        assert conn.execute(
            "SELECT after_reserved FROM ledger.projection_repair_row WHERE account_id=%s",
            (str(command.merchant_account_id),),
        ).fetchone() == (62,)
        report = verify_snapshot(capture_snapshot(conn, str(command.workspace_id)))
    assert report.structure_ok and report.projection_ok and report.request_intent_ok
    assert report.healthy


def test_same_event_identity_and_sequence_are_independent_between_provider_namespaces(
    seeded_db, golden
):
    from ledgerops.domain import ProviderNamespaceId
    from tests.integration.payout_helpers import fund_merchant, payout_policy

    fund_merchant(seeded_db, golden)
    identifier = str(uuid4())
    commands = []
    for number, namespace in enumerate([str(uuid4()), str(uuid4())]):
        args = payout_input(
            golden,
            key=f"namespace-{number}",
            operation_id=str(uuid4()),
            provider_namespace=namespace,
            principal_minor="30",
        )
        policy = payout_policy(golden, provider_namespace=ProviderNamespaceId.parse(namespace))
        assert admit(seeded_db, golden, args, policy=policy).http_status == 202
        commands.append(args[1])
    for command in commands:
        assert (
            observe(
                seeded_db, command, event(seeded_db, command, event_id=identifier, sequence=1)
            ).status
            == "APPLIED"
        )
    assert raw_table(seeded_db, "payout_incident") == []
    assert money(seeded_db, golden)[:3] == (16, 0, 16)
    with seeded_db.connect("reader") as conn:
        assert conn.execute(
            "SELECT booked FROM ledger.account_balance WHERE account_id=%s",
            (golden["accounts"][0]["id"],),
        ).fetchone() == (36,)


@pytest.mark.parametrize("collision", ["event_id", "sequence"])
def test_authenticated_cross_payout_global_identity_collision_preserves_money_and_stops_second(
    seeded_db, golden, collision
):
    from tests.integration.payout_helpers import fund_merchant

    fund_merchant(seeded_db, golden)
    commands = []
    for number in range(2):
        args = payout_input(
            golden, key=f"collision-{number}", operation_id=str(uuid4()), principal_minor="30"
        )
        assert admit(seeded_db, golden, args).http_status == 202
        commands.append(args[1])
    identifier = str(uuid4())
    first = event(seeded_db, commands[0], event_id=identifier, sequence=1)
    assert observe(seeded_db, commands[0], first).status == "APPLIED"
    before = financial_state(seeded_db)
    second = event(
        seeded_db,
        commands[1],
        event_id=identifier if collision == "event_id" else str(uuid4()),
        sequence=2 if collision == "event_id" else 1,
    )
    assert observe(seeded_db, commands[1], second).status == "INCIDENT"
    assert financial_state(seeded_db) == before
    assert money(seeded_db, golden)[:3] == (48, 32, 16)
    with seeded_db.connect("worker") as conn:
        assert (
            claim_payout(
                conn, commands[1].workspace_id, commands[1].operation_id, clock=lambda: NOW
            )
            is None
        )


def test_quarantine_storage_is_bounded_and_cannot_starve_a_valid_terminal(seeded_db, golden):
    command = prepared_payout(seeded_db, golden)
    before = financial_state(seeded_db)
    for number in range(103):
        invalid = json.dumps({"untrusted": number}).encode()
        assert observe(seeded_db, command, invalid).status == "QUARANTINED"
    assert financial_state(seeded_db) == before
    assert 0 < len(raw_table(seeded_db, "observation_quarantine")) <= 100
    assert raw_table(seeded_db, "payout_incident") == []
    assert observe(seeded_db, command, event(seeded_db, command)).status == "APPLIED"
    check_final(seeded_db, golden, command, "SUCCEEDED")


def test_outer_json_formatting_does_not_change_signed_event_identity(seeded_db, golden):
    command = prepared_payout(seeded_db, golden)
    raw = event(seeded_db, command)
    assert observe(seeded_db, command, raw).status == "APPLIED"
    before = financial_state(seeded_db)
    formatted = json.dumps(dict(reversed(list(json.loads(raw).items()))), indent=2).encode()
    assert formatted != raw
    assert observe(seeded_db, command, formatted).status == "REPLAYED"
    assert financial_state(seeded_db) == before
    assert raw_table(seeded_db, "payout_incident") == []


def test_changed_signed_payload_text_is_an_identity_conflict_even_when_parsed_data_matches(
    seeded_db, golden
):
    import hashlib
    import hmac

    from tests.provider.test_process import SECRET

    command = prepared_payout(seeded_db, golden)
    raw = event(seeded_db, command, "ACCEPTED")
    assert observe(seeded_db, command, raw).status == "OBSERVED"
    item = json.loads(raw)
    item["payload"] = json.dumps(json.loads(item["payload"]), indent=2)
    item["signature"] = hmac.new(
        SECRET, b"ledgerops.provider-event:v1\ntest-v1\n" + item["payload"].encode(), hashlib.sha256
    ).hexdigest()
    before = financial_state(seeded_db)
    assert observe(seeded_db, command, json.dumps(item).encode()).status == "INCIDENT"
    assert financial_state(seeded_db) == before


def test_cross_workspace_identity_collision_preserves_frozen_workspace_and_reports_related_incident(
    seeded_db, golden
):
    from ledgerops.db.maintenance import freeze_workspace
    from tests.integration.helpers import post_fixture, seed_accounts

    command_a = prepared_payout(seeded_db, golden)
    event_id = str(uuid4())
    assert (
        observe(seeded_db, command_a, event(seeded_db, command_a, event_id=event_id)).status
        == "APPLIED"
    )
    workspace_a = str(command_a.workspace_id)
    workspace_b = str(uuid4())
    second = json.loads(json.dumps(golden).replace(workspace_a, workspace_b))
    seed_accounts(seeded_db, second)
    post_fixture(seeded_db, second, "OPENING")
    command_b = prepared_payout(seeded_db, second)
    with seeded_db.connect("maintenance") as conn:
        freeze_workspace(
            conn, workspace_a, str(uuid4()), ACTOR, "frozen during another workspace event"
        )
    tables = [
        "payout",
        "reservation",
        "reservation_event",
        "dispatch_intent",
        "account_balance",
        "journal",
        "journal_entry",
        "provider_observation",
        "payout_incident",
        "payout_resolution",
    ]

    def capture_a():
        with seeded_db.connect("reader") as conn:
            return {
                table: conn.execute(
                    f"SELECT * FROM ledger.{table} WHERE workspace_id=%s", (workspace_a,)
                ).fetchall()
                for table in tables
            }

    before = capture_a()
    incoming = event(seeded_db, command_b, event_id=event_id, sequence=2)
    assert observe(seeded_db, command_b, incoming).status == "INCIDENT"
    assert capture_a() == before
    with seeded_db.connect("reader") as conn:
        assert conn.execute(
            "SELECT mode FROM ledger.workspace WHERE id=%s", (workspace_a,)
        ).fetchone() == ("FROZEN",)
        related = conn.execute(
            "SELECT workspace_id,payout_id,code FROM ledger.related_payout_incident "
            "WHERE workspace_id=%s",
            (workspace_a,),
        ).fetchall()
        assert len(related) == 1
        assert tuple(str(value) for value in related[0][:2]) == (
            workspace_a,
            str(command_a.operation_id),
        )
        assert related[0][2] == "EVENT_ID_REUSED"
    assert money(seeded_db, golden)[:3] == (18, 0, 18)
    assert money(seeded_db, second)[:3] == (80, 62, 18)
