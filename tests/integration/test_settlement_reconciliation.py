"""Real PostgreSQL, provider HTTP and independently persisted settlement evidence."""

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from ledgerops.db.payout_dispatch import dispatch_once
from ledgerops.export import export_workspace, load_export
from ledgerops.provider_client import ProviderHTTPClient
from ledgerops.reconciliation import StatementTrust, reconcile_bundle
from tests.integration.payout_helpers import NOW, money
from tests.integration.payout_observation_helpers import financial_state, prepared_payout, trust
from tests.provider.test_process import ROOT, SECRET, TOKEN, ProcessProvider, effect_rows

pytestmark = pytest.mark.integration


def emit_statement(provider, command, root, *, days_later=0, assigned=True):
    """Run the independent issuer CLI against its existing store, without ledger inputs."""
    from os import environ
    from sys import executable

    effective = datetime.now(UTC).replace(microsecond=0) + timedelta(days=days_later)
    day = effective.astimezone(ZoneInfo("Europe/Warsaw")).date().isoformat()
    key = f"v1.{command.provider_namespace}.{command.workspace_id}.{command.operation_id}"
    batch = {
        "batch_id": str(uuid4()),
        "effective_at": effective.isoformat().replace("+00:00", "Z"),
        "rows": [{"row_id": str(uuid4()), "provider_keys": [key]}],
        "fee_rule_version": "quoted_fee_v1",
    }
    source = root / "batch.json"
    source.write_text(json.dumps(batch))
    base = [
        executable,
        "-m",
        "provider_sim.settlement",
        "--database",
        str(provider.path),
        "--namespace",
        str(command.provider_namespace),
        "--key-id",
        "test-v1",
    ]
    env = {**environ, "LEDGEROPS_PROVIDER_HMAC_KEY": SECRET.hex()}
    if assigned:
        assignment = subprocess.run(
            base + ["assign", "--batch-file", str(source)],
            cwd=ROOT,
            env=env,
            capture_output=True,
            timeout=15,
        )
        assert assignment.returncode == 0, assignment.stderr
    issued = subprocess.run(
        base
        + ["statement", "--workspace", str(command.workspace_id), "--date", day, "--version", "1"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        timeout=15,
    )
    assert issued.returncode == 0, issued.stderr
    return (
        issued.stdout,
        base
        + ["statement", "--workspace", str(command.workspace_id), "--date", day, "--version", "1"],
        env,
    )


def pinned_export(db, command, root):
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, capture_output=True, check=True
    ).stdout.strip()
    with db.connect("reader") as conn:
        return export_workspace(
            conn, str(command.workspace_id), root, code_revision=revision, dirty=True
        ), revision


@pytest.mark.parametrize(
    "scenario", ["same_day", "provider_ahead", "later_settlement", "unassigned"]
)
def test_real_payout_settlement_and_ledger_export_are_compared_without_changing_money(
    seeded_db, golden, tmp_path, scenario
):
    command = prepared_payout(seeded_db, golden)
    provider = ProcessProvider(tmp_path / "provider", namespace=str(command.provider_namespace))
    provider.directory.mkdir()
    provider.start()
    try:
        adapter = ProviderHTTPClient(provider.ready["url"], TOKEN)
        with seeded_db.connect("reader") as conn:
            key, raw = conn.execute(
                "SELECT provider_key,provider_request FROM ledger.payout"
            ).fetchone()
        if scenario == "provider_ahead":
            # A genuine external effect exists before its authenticated local observation.
            assert adapter.put(key, raw).http_status == 200
            assert tuple(money(seeded_db, golden)[:3]) == (80, 62, 18)
        else:
            with seeded_db.connect("worker") as conn:
                result = dispatch_once(
                    conn,
                    command.workspace_id,
                    command.operation_id,
                    adapter,
                    trust=trust(command),
                    clock=lambda: NOW,
                )
            assert result.resolution.status == "APPLIED"
            assert tuple(money(seeded_db, golden)[:3]) == (18, 0, 18)
        assert effect_rows(provider.path) == [(key, "60", "2", "62")]
        statement, replay_command, env = emit_statement(
            provider,
            command,
            tmp_path,
            days_later=int(scenario == "later_settlement"),
            assigned=scenario != "unassigned",
        )
        path, revision = pinned_export(seeded_db, command, tmp_path / "exports")
        frozen = {name: (Path(path) / name).read_bytes() for name in ("manifest.json", "data.json")}
        before = financial_state(seeded_db)
        report = reconcile_bundle(
            path,
            statement,
            trust=StatementTrust(str(command.provider_namespace), {"test-v1": SECRET}),
            code_revision=revision,
            dirty=True,
        )
        assert report["status"] == "COMPLETE" and report["usable"]
        categories = {issue["category"] for issue in report["issues"]}
        if scenario == "same_day":
            assert report["reconciled"] and categories == set(), report
            assert report["matched_provider_keys"] == [key]
        elif scenario == "provider_ahead":
            assert not report["reconciled"] and "MISSING_INTERNAL" in categories, report
            assert "TIMING_ONLY" not in categories
        elif scenario == "later_settlement":
            assert not report["reconciled"] and categories == {"TIMING_ONLY"}, report
        else:
            assert not report["reconciled"] and not report["settlement_complete"]
            assert report["unsettled_provider_keys"] == [key]
            assert report["matched_provider_keys"] == [key]
            assert categories == set() and report["groups"] == []
        if scenario != "unassigned":
            assert len(report["groups"]) == 1
            group = report["groups"][0]
            assert (group["principal_minor"], group["fee_minor"], group["gross_minor"]) == (
                "60",
                "2",
                "62",
            )
            assert group["provider_keys"] == [key]
        assert financial_state(seeded_db) == before
        assert frozen == {name: (Path(path) / name).read_bytes() for name in frozen}
        # Same version is immutable even when requested through a separate process.
        again = subprocess.run(replay_command, cwd=ROOT, env=env, capture_output=True, timeout=15)
        assert again.returncode == 0 and again.stdout == statement
        assert len(load_export(path)["payouts"]) == 1
        statement_file = tmp_path / "statement.json"
        statement_file.write_bytes(statement)
        keys = tmp_path / "keys.json"
        keys.write_text(json.dumps({"test-v1": SECRET.hex()}))
        keys.chmod(0o600)
        offline = subprocess.run(
            [
                sys.executable,
                "-m",
                "ledgerops.recon_cli",
                "match",
                "--ledger-export",
                str(path),
                "--statement",
                str(statement_file),
                "--namespace",
                str(command.provider_namespace),
                "--keys-file",
                str(keys),
                "--code-revision",
                revision,
                "--dirty",
            ],
            cwd=ROOT,
            capture_output=True,
            timeout=15,
        )
        assert offline.returncode == (0 if scenario == "same_day" else 1), offline.stderr
        assert json.loads(offline.stdout) == report
        assert SECRET.hex().encode() not in offline.stdout + offline.stderr
    finally:
        provider.stop()
