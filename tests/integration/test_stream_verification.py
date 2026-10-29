"""Full independent finance and bounded source validation over actual stream files."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from ledgerops.export import _json_bytes
from ledgerops.stream_export import export_stream
from ledgerops.stream_verification import StreamError, stage_stream, verify_stream
from tests.integration.payout_helpers import admit, payout_input
from tests.integration.payout_observation_helpers import observe, prepared_payout
from tests.integration.purchase_helpers import invoke, purchase_input
from tests.integration.test_operator_actions import approve, execute, operator_story, propose
from tests.integration.test_reconciliation_publication import REVISION

pytestmark = pytest.mark.integration


def isolated_consumer(path, root):
    from ledgerops import stream_verification as consumer
    from ledgerops import verification as audit

    minimal = root / "minimal-consumer"
    minimal.mkdir()
    for name in ("verification.py", "verification_external.py", "verification_operator.py"):
        shutil.copyfile(Path(audit.__file__).with_name(name), minimal / name)
    for name in ("stream_contract.py", "stream_verification.py"):
        shutil.copyfile(Path(consumer.__file__).with_name(name), minimal / name)
    driver = root / "isolated_driver.py"
    driver.write_text(
        "import json,resource,runpy,sys\n"
        "sys.argv=sys.argv[1:]\n"
        "try:\n runpy.run_path(sys.argv[0],run_name='__main__')\n"
        "except SystemExit as exc:\n code=exc.code\n"
        "value=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss\n"
        "value=value if sys.platform=='darwin' else value*1024\n"
        "print(json.dumps({'max_rss_bytes':value,'platform':sys.platform}),file=sys.stderr)\n"
        "raise SystemExit(code)\n"
    )
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("LEDGEROPS_") and key != "PYTHONPATH"
    }
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            str(driver),
            str(minimal / "stream_verification.py"),
            str(path),
        ],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    metrics = json.loads(result.stderr.splitlines()[-1])
    (root / "consumer-metrics.json").write_text(json.dumps(metrics, sort_keys=True) + "\n")
    assert metrics["max_rss_bytes"] < 256 * 1024 * 1024
    return json.loads(result.stdout), metrics


def publish(db, golden, root):
    with db.connect("reader") as conn:
        return export_stream(
            conn, golden["workspace"]["id"], root, code_revision=REVISION, dirty=True
        )


def rewrite(path, change):
    manifest = json.loads((path / "manifest.json").read_bytes())
    for chunk in manifest["chunks"]:
        rows = [json.loads(line) for line in (path / chunk["file"]).read_bytes().splitlines()]
        change(chunk["table"], rows)
        payload = b"".join(_json_bytes(row) for row in rows)
        (path / chunk["file"]).write_bytes(payload)
        chunk["bytes"] = len(payload)
        chunk["sha256"] = hashlib.sha256(payload).hexdigest()
    size = sum(chunk["bytes"] for chunk in manifest["chunks"])
    manifest["total_bytes"] = size
    while True:
        raw = _json_bytes(manifest)
        total = size + len(raw)
        if total == manifest["total_bytes"]:
            break
        manifest["total_bytes"] = total
    (path / "manifest.json").write_bytes(raw)


def test_full_financial_checker_matches_healthy_purchase_source(seeded_db, golden, tmp_path):
    assert invoke(seeded_db, *purchase_input(golden)).http_status == 201
    path = publish(seeded_db, golden, tmp_path)
    report = verify_stream(path)
    assert report["healthy"] and report["verification_complete"], report
    assert report["structure_ok"] and report["projection_ok"] and report["request_intent_ok"]
    assert report["unverified_scope_count"] == 0


def test_all_rows_over_10000_request_keys_are_verified_without_large_operation_array(
    seeded_db, golden, tmp_path
):
    assert invoke(seeded_db, *purchase_input(golden)).http_status == 201
    with seeded_db.connect("owner") as conn:
        conn.execute(
            "INSERT INTO ledger.request_result(workspace_id,client_id,route_family,key,"
            "operation_id,payload,payload_sha256,http_status,response_body) "
            "SELECT workspace_id,client_id,route_family,'bulk-'||g::text,operation_id,payload,"
            "payload_sha256,http_status,response_body FROM ledger.request_result "
            "CROSS JOIN generate_series(1,10005)g"
        )
    path = publish(seeded_db, golden, tmp_path)
    with stage_stream(path) as store:
        assert store.conn.execute("SELECT count(*) FROM requests").fetchone() == (10006,)
    report = verify_stream(path)
    assert report["healthy"] and report["verification_complete"], report


def test_actual_allowed_129_max_size_quarantines_remain_fully_verified(seeded_db, golden, tmp_path):
    first = prepared_payout(seeded_db, golden)
    arguments = payout_input(
        golden, key="small-second", operation_id=str(uuid4()), principal_minor="1"
    )
    assert admit(seeded_db, golden, arguments).http_status == 202
    second = arguments[1]
    for index in range(129):
        prefix = f"invalid-{index:03d}-".encode()
        raw = prefix + b"x" * (65536 - len(prefix))
        assert (
            observe(seeded_db, first if index < 65 else second, raw).code == "INVALID_OBSERVATION"
        )
    path = publish(seeded_db, golden, tmp_path)
    assert json.loads((path / "manifest.json").read_bytes())["total_bytes"] > 16 * 1024 * 1024
    report = verify_stream(path)
    assert report["healthy"] and report["verification_complete"], report
    assert report["external_evidence"]["accepted_payout_count"] == 2
    independent, metrics = isolated_consumer(path, tmp_path)
    assert independent == report
    print(
        json.dumps(
            {
                "isolated_consumer": metrics,
                "input_bytes": json.loads((path / "manifest.json").read_bytes())["total_bytes"],
            }
        )
    )


def test_executed_operator_disposition_full_chain_is_checked(seeded_db, golden, tmp_path):
    story = operator_story(seeded_db, golden, tmp_path)
    proposal = propose(story)
    approve(story, proposal)
    execute(story, proposal)
    path = publish(seeded_db, golden, tmp_path / "stream")
    report = verify_stream(path)
    assert report["healthy"] and report["verification_complete"], report
    assert report["external_evidence"]["payouts_resolved"]
    rewrite(
        path,
        lambda table, rows: (
            rows[0].update(checker_actor_id=story["maker"].principal.actor_id.value.__str__())
            if table == "operator_approvals" and rows
            else None
        ),
    )
    report = verify_stream(path)
    assert not report["healthy"] and not report["structure_ok"]
    assert "INVALID_OPERATOR_APPROVAL" in {row["code"] for row in report["issues"]}


@pytest.mark.parametrize(
    "mutation,flag,code",
    [
        (
            lambda table, rows: (
                rows[0].update(credits="1") if table == "balances" and rows else None
            ),
            "projection_ok",
            "PROJECTION_DRIFT",
        ),
        (
            lambda table, rows: (
                rows[0].update(response_body={"changed": True})
                if table == "requests" and rows
                else None
            ),
            "request_intent_ok",
            "REQUEST_OPERATION_MISMATCH",
        ),
    ],
)
def test_financial_corruption_fails_after_all_artifact_hashes_are_recomputed(
    seeded_db, golden, tmp_path, mutation, flag, code
):
    assert invoke(seeded_db, *purchase_input(golden)).http_status == 201
    path = publish(seeded_db, golden, tmp_path)
    rewrite(path, mutation)
    report = verify_stream(path)
    assert not report["healthy"] and not report[flag]
    assert code in {row["code"] for row in report["issues"]}


def test_scratch_cleanup_on_corrupt_missing_and_unsupported_stream(seeded_db, golden, tmp_path):
    path = publish(seeded_db, golden, tmp_path / "source")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    manifest = json.loads((path / "manifest.json").read_bytes())
    (path / manifest["chunks"][0]["file"]).unlink()
    with pytest.raises(StreamError):
        verify_stream(path, scratch_root=scratch)
    assert list(scratch.iterdir()) == []


def test_complete_manifest_before_rename_is_not_a_published_export(
    seeded_db, golden, tmp_path, monkeypatch
):
    from ledgerops import stream_export as producer

    real_rename = producer.os.rename
    observed = []

    def before_publish(stage, final):
        assert (stage / "manifest.json").is_file()
        with pytest.raises(StreamError, match="COMPLETED_EXPORT_DIRECTORY_REQUIRED"):
            verify_stream(stage)
        observed.append(stage)
        return real_rename(stage, final)

    monkeypatch.setattr(producer.os, "rename", before_publish)
    path = publish(seeded_db, golden, tmp_path)
    assert len(observed) == 1 and verify_stream(path)["healthy"]
