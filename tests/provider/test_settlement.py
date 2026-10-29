"""Real SQLite settlement evidence; expected amounts, dates and HMAC are literal."""

import hashlib
import hmac
import json
import os
import selectors
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Barrier, Event
from uuid import UUID
from zoneinfo import ZoneInfoNotFoundError

import pytest

from provider_sim.protocol import Conflict, NotFound, ValidationError
from provider_sim.settlement import SettlementStore, day_bounds
from provider_sim.store import ProviderStore

NAMESPACE = str(UUID(int=100))
WORKSPACE = str(UUID(int=101))
MERCHANT = str(UUID(int=102))
SECRET = bytes(range(32))
KEY_ID = "settlement-test-v1"
BATCH = str(UUID(int=200))
ROW = str(UUID(int=300))


def body(number=1, *, merchant=MERCHANT, workspace=WORKSPACE, fee="2"):
    return {
        "workspace_id": workspace,
        "operation_id": str(UUID(int=number)),
        "merchant_id": merchant,
        "beneficiary_id": str(UUID(int=103)),
        "principal_minor": "60",
        "fee_minor": fee,
        "gross_minor": str(60 + int(fee)),
        "currency": "PLN",
        "contract_version": 1,
        "dispatch_deadline": "2099-01-01T00:00:00Z",
    }


def key(number=1, workspace=WORKSPACE):
    return f"v1.{NAMESPACE}.{workspace}.{UUID(int=number)}"


def row(*numbers, row_id=ROW, workspace=WORKSPACE):
    return {
        "row_id": row_id,
        "provider_keys": [key(number, workspace) for number in numbers],
    }


@pytest.fixture
def store(tmp_path):
    return ProviderStore(tmp_path / "provider.sqlite3", NAMESPACE, KEY_ID, SECRET)


@pytest.fixture
def settlement(store):
    return SettlementStore(store)


def decode(envelope):
    assert envelope.keys() == {"key_id", "payload", "signature"}
    assert envelope["key_id"] == KEY_ID
    raw = envelope["payload"].encode()
    signed = b"ledgerops.provider-statement:v1\nsettlement-test-v1\n" + raw
    assert hmac.compare_digest(
        hmac.new(SECRET, signed, hashlib.sha256).hexdigest(), envelope["signature"]
    )
    # This signature must not be usable as a financial execution observation.
    assert (
        envelope["signature"]
        != hmac.new(
            SECRET,
            b"ledgerops.provider-event:v1\nsettlement-test-v1\n" + raw,
            hashlib.sha256,
        ).hexdigest()
    )
    result = json.loads(raw)
    assert json.dumps(result, sort_keys=True, separators=(",", ":")) == envelope["payload"]
    return result


def test_split_rows_exact_groups_and_quoted_fee_from_provider_source(store, settlement):
    for number, fee in ((1, "2"), (2, "2"), (3, "3")):
        store.put(key(number), body(number, fee=fee))
    receipt = settlement.assign_batch(
        BATCH,
        "2026-10-29T09:00:00Z",
        [row(2, 1), row(3, row_id=str(UUID(int=301)))],
    )
    result = decode(settlement.statement(WORKSPACE, "2026-10-29", 1))
    assert result["scope"] == "COMPLETE_WORKSPACE_EFFECTS_AT_WATERMARK"
    assert result["time_basis"] == "SETTLEMENT_EFFECTIVE_TIME"
    assert (result["effect_watermark"], result["assignment_watermark"]) == (3, 3)
    assert result["start_inclusive"] == "2026-10-28T23:00:00Z"
    assert result["end_exclusive"] == "2026-10-29T23:00:00Z"
    assert result["rows"] == [
        {
            "row_id": ROW,
            "group": {
                "namespace": NAMESPACE,
                "batch_id": BATCH,
                "currency": "PLN",
                "merchant_id": MERCHANT,
                "fee_rule_version": "quoted_fee_v1",
            },
            "provider_keys": [key(1), key(2)],
            "principal_minor": "120",
            "fee_minor": "4",
            "gross_minor": "124",
        },
        {
            "row_id": str(UUID(int=301)),
            "group": {
                "namespace": NAMESPACE,
                "batch_id": BATCH,
                "currency": "PLN",
                "merchant_id": MERCHANT,
                "fee_rule_version": "quoted_fee_v1",
            },
            "provider_keys": [key(3)],
            "principal_minor": "60",
            "fee_minor": "3",
            "gross_minor": "63",
        },
    ]
    assert len(result["effects"]) == 3
    assert result["effects"][0]["request"] == body(1)
    assert result["effects"][0]["assignment"]["effective_at"] == "2026-10-29T09:00:00Z"
    assert result["effects"][0]["assignment"]["recorded_at"] == receipt["recorded_at"]


def test_old_statement_immutable_and_late_assignment_advances_only_assignment_watermark(
    store, settlement
):
    event = store.put(key(), body())
    with sqlite3.connect(store.path) as conn:
        old_event_bytes = conn.execute("SELECT envelope FROM events").fetchone()[0]
    first = settlement.statement(WORKSPACE, "2026-10-29", 1)
    unassigned = decode(first)
    assert unassigned["rows"] == []
    assert unassigned["effects"][0]["assignment"] is None
    assert (unassigned["effect_watermark"], unassigned["assignment_watermark"]) == (
        1,
        0,
    )
    settlement.assign_batch(BATCH, "2026-10-29T09:00:00Z", [row(1)])
    second = decode(settlement.statement(WORKSPACE, "2026-10-29", 2))
    assert (second["effect_watermark"], second["assignment_watermark"]) == (1, 1)
    assert len(second["rows"]) == 1
    assert settlement.statement(WORKSPACE, "2026-10-29", 1) == first
    reopened = SettlementStore(ProviderStore(store.path, NAMESPACE, KEY_ID, SECRET))
    assert reopened.statement(WORKSPACE, "2026-10-29", 1) == first
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT envelope FROM events").fetchone()[0] == old_event_bytes
        assert (
            json.loads(conn.execute("SELECT config FROM metadata").fetchone()[0])["schema_version"]
            == 1
        )
    assert store.lookup(key()) == event


def test_outside_period_effect_remains_in_complete_inventory_and_workspace_isolated(
    store, settlement
):
    other_workspace = str(UUID(int=110))
    store.put(key(), body())
    store.put(key(2), body(2))
    store.put(key(3, other_workspace), body(3, workspace=other_workspace))
    settlement.assign_batch(BATCH, "2026-10-30T09:00:00Z", [row(1)])
    result = decode(settlement.statement(WORKSPACE, "2026-10-29", 1))
    assert result["effect_watermark"] == 3
    assert result["rows"] == []
    assert [effect["provider_key"] for effect in result["effects"]] == [key(1), key(2)]
    assert result["effects"][0]["assignment"]["effective_at"] == "2026-10-30T09:00:00Z"
    assert result["effects"][1]["assignment"] is None


def test_equivalent_batch_order_replays_and_changed_metadata_conflicts(store, settlement):
    for number in (1, 2, 3):
        store.put(key(number), body(number))
    original = [row(2, 1), row(3, row_id=str(UUID(int=301)))]
    first = settlement.assign_batch(BATCH, "2026-10-29T09:00:00Z", original)
    assert (
        settlement.assign_batch(
            BATCH,
            "2026-10-29T09:00:00Z",
            list(
                reversed(
                    [
                        row(1, 2),
                        row(3, row_id=str(UUID(int=301))),
                    ]
                )
            ),
        )
        == first
    )
    with pytest.raises(Conflict):
        settlement.assign_batch(BATCH, "2026-10-30T09:00:00Z", original)
    with pytest.raises(Conflict):
        settlement.assign_batch(BATCH, "2026-10-29T09:00:00Z", [row(1, 2, 3)])
    with pytest.raises(Conflict):
        settlement.assign_batch(
            str(UUID(int=201)),
            "2026-10-29T09:00:00Z",
            [row(1, row_id=str(UUID(int=302)))],
        )


@pytest.mark.parametrize("outcome", ["ACCEPTED", "FAILED", "CLOSED_ABSENT", "MISSING"])
def test_no_effect_cannot_be_settled(store, settlement, outcome):
    if outcome == "CLOSED_ABSENT":
        store.close(key(), body())
    elif outcome != "MISSING":
        store.put(key(), body(), outcome=outcome)
    with pytest.raises(NotFound):
        settlement.assign_batch(BATCH, "2026-10-29T09:00:00Z", [row(1)])
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM settlement_batch").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM settlement_assignment").fetchone()[0] == 0


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [row(1, 1)],
        [row(1), row(1, row_id=str(UUID(int=301)))],
        [row(1), row(2)],
        [{"row_id": ROW, "provider_keys": []}],
        [{**row(1), "principal_minor": "1"}],
        [{**row(1), "merchant_id": MERCHANT}],
        [{"row_id": "bad", "provider_keys": [key()]}],
        [{"row_id": ROW, "provider_keys": ["bad"]}],
    ],
)
def test_invalid_members_or_caller_amounts_rejected(settlement, rows):
    with pytest.raises(ValidationError):
        settlement.assign_batch(BATCH, "2026-10-29T09:00:00Z", rows)


@pytest.mark.parametrize(
    "change", [{"merchant": str(UUID(int=120))}, {"workspace": str(UUID(int=121))}]
)
def test_row_must_stay_in_one_group(store, settlement, change):
    store.put(key(), body())
    second = body(2, **change)
    store.put(key(2, second["workspace_id"]), second)
    rows = [{"row_id": ROW, "provider_keys": [key(), key(2, second["workspace_id"])]}]
    with pytest.raises(ValidationError, match="one workspace/merchant/currency"):
        settlement.assign_batch(BATCH, "2026-10-29T09:00:00Z", rows)


def test_batch_cannot_split_same_merchant_uuid_across_workspace_scopes(store, settlement):
    other_workspace = str(UUID(int=121))
    store.put(key(), body())
    store.put(key(2, other_workspace), body(2, workspace=other_workspace))
    with pytest.raises(ValidationError, match="batch must contain one workspace"):
        settlement.assign_batch(
            BATCH,
            "2026-10-29T09:00:00Z",
            [row(1), row(2, row_id=str(UUID(int=301)), workspace=other_workspace)],
        )
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM settlement_batch").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM settlement_assignment").fetchone()[0] == 0


@pytest.mark.parametrize(
    "instant", ["2026-10-29", "2026-10-29T09:00:00+00:00", "2026-02-30T09:00:00Z", None]
)
def test_invalid_settlement_instant_rejected(settlement, instant):
    with pytest.raises(ValidationError):
        settlement.assign_batch(BATCH, instant, [row(1)])


def test_unknown_fee_rule_rejected(settlement):
    with pytest.raises(ValidationError):
        settlement.assign_batch(
            BATCH, "2026-10-29T09:00:00Z", [row(1)], fee_rule_version="fixed_two"
        )


@pytest.mark.parametrize(
    "day,start,end,hours",
    [
        ("2026-03-29", "2026-03-28T23:00:00Z", "2026-03-29T22:00:00Z", 23),
        ("2026-10-25", "2026-10-24T22:00:00Z", "2026-10-25T23:00:00Z", 25),
        ("2026-10-29", "2026-10-28T23:00:00Z", "2026-10-29T23:00:00Z", 24),
    ],
)
def test_business_calendar_uses_actual_dst_bounds(day, start, end, hours):
    assert day_bounds(day) == (start, end)
    assert (
        datetime.fromisoformat(end) - datetime.fromisoformat(start)
    ).total_seconds() == hours * 3600


@pytest.mark.parametrize("day", ["2026-2-01", "2026-02-30", "9999-12-31", None])
def test_invalid_business_date_rejected(settlement, day):
    with pytest.raises(ValidationError):
        settlement.statement(WORKSPACE, day, 1)


@pytest.mark.parametrize("version", [0, -1, True, "1", 2**31])
def test_invalid_statement_version_rejected(settlement, version):
    with pytest.raises(ValidationError):
        settlement.statement(WORKSPACE, "2026-10-29", version)


def test_other_timezone_not_silently_used(settlement):
    with pytest.raises(ValidationError):
        settlement.statement(WORKSPACE, "2026-10-29", 1, timezone_name="UTC")


def test_missing_timezone_database_fails_explicitly(monkeypatch):
    def missing(_):
        raise ZoneInfoNotFoundError("timezone data unavailable")

    monkeypatch.setattr("provider_sim.settlement.ZoneInfo", missing)
    with pytest.raises(ValidationError, match="Unsupported business date"):
        day_bounds("2026-10-29")


def test_period_start_inclusive_end_exclusive(store, settlement):
    for number in (1, 2, 3):
        store.put(key(number), body(number))
    for number, instant in (
        (1, "2026-10-28T22:59:59Z"),
        (2, "2026-10-28T23:00:00Z"),
        (3, "2026-10-29T23:00:00Z"),
    ):
        settlement.assign_batch(
            str(UUID(int=200 + number)),
            instant,
            [row(number, row_id=str(UUID(int=300 + number)))],
        )
    result = decode(settlement.statement(WORKSPACE, "2026-10-29", 1))
    assert [member for item in result["rows"] for member in item["provider_keys"]] == [key(2)]
    assert len(result["effects"]) == 3


def test_concurrent_batch_race_allocates_each_effect_once(store, settlement):
    store.put(key(), body())
    barrier = Barrier(2)

    def assign(number):
        barrier.wait(timeout=5)
        try:
            return settlement.assign_batch(
                str(UUID(int=200 + number)),
                "2026-10-29T09:00:00Z",
                [row(1, row_id=str(UUID(int=300 + number)))],
            )
        except Conflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(assign, (1, 2)))
    assert results.count("conflict") == 1
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM settlement_assignment").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM settlement_batch").fetchone()[0] == 1


def test_concurrent_same_version_returns_exact_same_persisted_bytes(store, settlement):
    store.put(key(), body())
    barrier = Barrier(4)

    def build(_):
        barrier.wait(timeout=5)
        return settlement.statement(WORKSPACE, "2026-10-29", 1)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(build, range(4)))
    assert all(result == results[0] for result in results)
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM settlement_statement").fetchone()[0] == 1


def test_versions_must_start_at_one_and_increase_without_gaps(settlement):
    with pytest.raises(Conflict, match="next consecutive"):
        settlement.statement(WORKSPACE, "2026-10-29", 2)
    first = settlement.statement(WORKSPACE, "2026-10-29", 1)
    with pytest.raises(Conflict, match="next consecutive"):
        settlement.statement(WORKSPACE, "2026-10-29", 3)
    settlement.statement(WORKSPACE, "2026-10-29", 2)
    assert settlement.statement(WORKSPACE, "2026-10-29", 1) == first


def test_next_version_waits_for_first_version_transaction(settlement):
    first_ready, second_started, release = Event(), Event(), Event()

    def hook(phase):
        if phase == "before_commit":
            first_ready.set()
            assert release.wait(5)

    def second():
        assert first_ready.wait(5)
        second_started.set()
        return settlement.statement(WORKSPACE, "2026-10-29", 2)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_job = pool.submit(settlement.statement, WORKSPACE, "2026-10-29", 1, hook=hook)
        second_job = pool.submit(second)
        try:
            assert second_started.wait(5)
        finally:
            release.set()
        assert decode(first_job.result(timeout=5))["version"] == 1
        assert decode(second_job.result(timeout=5))["version"] == 2


def test_commit_fault_rolls_back_entire_batch_and_statement(store, settlement):
    store.put(key(), body())

    def fail(phase):
        if phase == "before_commit":
            raise RuntimeError("injected commit fault")

    with pytest.raises(RuntimeError, match="injected"):
        settlement.assign_batch(BATCH, "2026-10-29T09:00:00Z", [row(1)], hook=fail)
    with pytest.raises(RuntimeError, match="injected"):
        settlement.statement(WORKSPACE, "2026-10-29", 1, hook=fail)
    with sqlite3.connect(store.path) as conn:
        for table in (
            "settlement_batch",
            "settlement_assignment",
            "settlement_statement",
        ):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    assert (
        decode(settlement.statement(WORKSPACE, "2026-10-29", 1))["effects"][0]["assignment"] is None
    )


@pytest.mark.parametrize(
    "table", ["settlement_batch", "settlement_assignment", "settlement_statement"]
)
@pytest.mark.parametrize("action", ["UPDATE", "DELETE"])
def test_retained_evidence_cannot_be_changed(store, settlement, table, action):
    store.put(key(), body())
    settlement.assign_batch(BATCH, "2026-10-29T09:00:00Z", [row(1)])
    settlement.statement(WORKSPACE, "2026-10-29", 1)
    field = {
        "settlement_batch": "batch_id",
        "settlement_assignment": "sequence",
        "settlement_statement": "version",
    }[table]
    sql = f"DELETE FROM {table}" if action == "DELETE" else f"UPDATE {table} SET {field}={field}"
    with (
        sqlite3.connect(store.path) as conn,
        pytest.raises(sqlite3.IntegrityError, match="immutable"),
    ):
        conn.execute(sql)


def test_bounded_complete_inventory_never_truncates(store, settlement, monkeypatch):
    for number in (1, 2):
        store.put(key(number), body(number))
    monkeypatch.setattr("provider_sim.settlement.MAX_EFFECTS", 1)
    with pytest.raises(ValidationError, match="inventory exceeds"):
        settlement.statement(WORKSPACE, "2026-10-29", 1)
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM settlement_statement").fetchone()[0] == 0


def test_size_bound_rolls_back_statement(store, settlement, monkeypatch):
    store.put(key(), body())
    monkeypatch.setattr("provider_sim.settlement.MAX_STATEMENT_BYTES", 100)
    with pytest.raises(ValidationError, match="byte bound"):
        settlement.statement(WORKSPACE, "2026-10-29", 1)
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM settlement_statement").fetchone()[0] == 0


def test_cli_persists_assignment_and_statement(store, tmp_path):
    store.put(key(), body())
    batch_file = tmp_path / "batch.json"
    batch_file.write_text(
        json.dumps(
            {
                "batch_id": BATCH,
                "effective_at": "2026-10-29T09:00:00Z",
                "rows": [row(1)],
                "fee_rule_version": "quoted_fee_v1",
            }
        )
    )
    prefix = [
        sys.executable,
        "-m",
        "provider_sim.settlement",
        "--database",
        str(store.path),
        "--namespace",
        NAMESPACE,
        "--key-id",
        KEY_ID,
    ]
    env = {**os.environ, "LEDGEROPS_PROVIDER_HMAC_KEY": SECRET.hex()}
    assigned = subprocess.run(
        [*prefix, "assign", "--batch-file", str(batch_file)],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(assigned.stdout)["batch_id"] == BATCH
    result = subprocess.run(
        [
            *prefix,
            "statement",
            "--workspace",
            WORKSPACE,
            "--date",
            "2026-10-29",
            "--version",
            "1",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert decode(json.loads(result.stdout))["rows"][0]["gross_minor"] == "62"


def test_corrupt_source_is_not_turned_into_signed_statement(store, settlement):
    store.put(key(), body())
    with sqlite3.connect(store.path) as conn:
        conn.execute("DROP TRIGGER effects_no_update")
        conn.execute("UPDATE effects SET principal_minor='1'")
    with pytest.raises(ValidationError, match="contradicts"):
        settlement.statement(WORKSPACE, "2026-10-29", 1)
    with pytest.raises(ValidationError, match="contradicts"):
        settlement.assign_batch(BATCH, "2026-10-29T09:00:00Z", [row(1)])


def test_missing_success_effect_is_corrupt_source_not_empty_complete_statement(store, settlement):
    store.put(key(), body())
    with sqlite3.connect(store.path) as conn:
        conn.execute("DROP TRIGGER effects_no_delete")
        conn.execute("DELETE FROM effects")
    with pytest.raises(ValidationError, match="contradicts"):
        settlement.statement(WORKSPACE, "2026-10-29", 1)


def test_failed_batch_does_not_partially_allocate_good_members(store, settlement):
    store.put(key(), body())
    with pytest.raises(NotFound):
        settlement.assign_batch(BATCH, "2026-10-29T09:00:00Z", [row(1, 2)])
    receipt = settlement.assign_batch(BATCH, "2026-10-29T09:00:00Z", [row(1)])
    assert receipt["assignments"] == [{"sequence": 1, "provider_key": key(), "row_id": ROW}]


def test_row_identity_cannot_be_reused_for_another_batch(store, settlement):
    store.put(key(), body())
    store.put(key(2), body(2))
    settlement.assign_batch(BATCH, "2026-10-29T09:00:00Z", [row(1)])
    with pytest.raises(Conflict, match="row identity"):
        settlement.assign_batch(str(UUID(int=201)), "2026-10-29T09:00:00Z", [row(2)])


def test_batch_member_and_row_bounds_are_explicit(store, settlement, monkeypatch):
    monkeypatch.setattr("provider_sim.settlement.MAX_EFFECTS", 1)
    with pytest.raises(ValidationError, match="member bound"):
        settlement.assign_batch(BATCH, "2026-10-29T09:00:00Z", [row(1, 2)])
    monkeypatch.setattr("provider_sim.settlement.MAX_ROWS", 1)
    with pytest.raises(ValidationError, match="row list"):
        settlement.assign_batch(BATCH, "2026-10-29T09:00:00Z", [row(1), row(2)])


@pytest.mark.parametrize("phase,stored_count", [("before_commit", 1), ("after_commit", 2)])
def test_real_process_death_retains_whole_statement_or_none(store, settlement, phase, stored_count):
    store.put(key(), body())
    first = settlement.statement(WORKSPACE, "2026-10-29", 1)
    code = """
import os,sys
from provider_sim.store import ProviderStore
from provider_sim.settlement import SettlementStore
store=ProviderStore(sys.argv[1],sys.argv[2],sys.argv[3],bytes.fromhex(os.environ['LEDGEROPS_PROVIDER_HMAC_KEY']))
def hook(phase):
    if phase==sys.argv[5]:
        print('ready',flush=True)
        sys.stdin.read(1)
SettlementStore(store).statement(sys.argv[4],'2026-10-29',2,hook=hook)
"""
    process = subprocess.Popen(
        [sys.executable, "-u", "-c", code, str(store.path), NAMESPACE, KEY_ID, WORKSPACE, phase],
        env={**os.environ, "LEDGEROPS_PROVIDER_HMAC_KEY": SECRET.hex()},
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            assert selector.select(10), "child did not reach durable boundary"
            assert process.stdout.readline() == b"ready\n"
        process.kill()
        assert process.wait(timeout=10) < 0
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)
    reopened = SettlementStore(ProviderStore(store.path, NAMESPACE, KEY_ID, SECRET))
    assert reopened.statement(WORKSPACE, "2026-10-29", 1) == first
    with sqlite3.connect(store.path) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM settlement_statement").fetchone()[0] == stored_count
        )
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_cli_duplicate_fields_fail_instead_of_discarding_metadata(store, tmp_path):
    batch_file = tmp_path / "ambiguous.json"
    batch_file.write_text('{"batch_id":"first","batch_id":"second"}')
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "provider_sim.settlement",
            "--database",
            str(store.path),
            "--namespace",
            NAMESPACE,
            "--key-id",
            KEY_ID,
            "assign",
            "--batch-file",
            str(batch_file),
        ],
        env={**os.environ, "LEDGEROPS_PROVIDER_HMAC_KEY": SECRET.hex()},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "settlement operation failed" in result.stderr
    assert result.stdout == ""


def test_cli_missing_database_is_not_created_and_signed_as_empty(tmp_path):
    missing = tmp_path / "typo.sqlite3"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "provider_sim.settlement",
            "--database",
            str(missing),
            "--namespace",
            NAMESPACE,
            "--key-id",
            KEY_ID,
            "statement",
            "--workspace",
            WORKSPACE,
            "--date",
            "2026-10-29",
            "--version",
            "1",
        ],
        env={**os.environ, "LEDGEROPS_PROVIDER_HMAC_KEY": SECRET.hex()},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert not missing.exists()
    assert result.stdout == ""


@pytest.mark.parametrize("kind", ["fifo", "deep_json", "symlink"])
def test_cli_rejects_unbounded_file_types_and_deep_json(store, tmp_path, kind):
    path = tmp_path / "input.json"
    if kind == "fifo":
        os.mkfifo(path)
    elif kind == "symlink":
        target = tmp_path / "target.json"
        target.write_text("{}")
        path.symlink_to(target)
    else:
        path.write_text("[" * 1500 + "0" + "]" * 1500)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "provider_sim.settlement",
            "--database",
            str(store.path),
            "--namespace",
            NAMESPACE,
            "--key-id",
            KEY_ID,
            "assign",
            "--batch-file",
            str(path),
        ],
        env={**os.environ, "LEDGEROPS_PROVIDER_HMAC_KEY": SECRET.hex()},
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert result.returncode == 2
    assert (
        result.stderr == "settlement operation failed: check local source and explicit metadata\n"
    )
    assert SECRET.hex() not in result.stderr
    assert result.stdout == ""
