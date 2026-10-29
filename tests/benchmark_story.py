"""Explicit, disposable real-publication benchmark; run with make benchmark."""

import json
import os
import platform
import subprocess
import time
from pathlib import Path

from ledgerops.export import load_export_bundle, publish_snapshot
from ledgerops.reconciliation import StatementTrust
from ledgerops.reconciliation.publication import ObjectStore, pin_run, release_reader_pin
from ledgerops.reconciliation.runner import RunError, run_reconciliation
from tests.benchmark.workload import PROVIDER, SECRET, WORKSPACE, workload
from tests.integration.conftest import seeded_db as seeded_db


def tree_bytes(path):
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def test_measure_same_bytes_one_two_four_workers(seeded_db, tmp_path):
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    report = {
        "version": 1,
        "code_revision": revision,
        "dirty": True,
        "platform": platform.platform(),
        "python": platform.python_version(),
        "scope": (
            "Generated offline evidence through real PG publication; "
            "not financial admission throughput"
        ),
        "rss_scope": (
            "Parent lifetime peak and each worker peak are separate; not a simultaneous aggregate"
        ),
        "cases": [],
    }
    output = Path(
        os.environ.get("LEDGEROPS_BENCHMARK_OUTPUT", "/private/tmp/ledgerops-benchmark.json")
    )
    for name, count, skew in [("small", 16, False), ("balanced", 256, False), ("skew", 768, True)]:
        data, statement = workload(count, skew=skew)
        case_dir = tmp_path / name
        path = publish_snapshot(data, case_dir / "export", code_revision=revision, dirty=True)
        bundle = load_export_bundle(path)
        expected = None
        for workers in (1, 2, 4):
            objects = ObjectStore(case_dir / f"objects-{workers}")
            before = tree_bytes(objects.root)
            started = time.perf_counter()
            row = {
                "case": name,
                "payouts": count,
                "workers": workers,
                "skew": skew,
                "source_bytes": len(bundle.manifest_bytes)
                + len(bundle.data_bytes)
                + len(statement),
            }
            try:
                with seeded_db.connect("reconciler") as conn:
                    result = run_reconciliation(
                        conn,
                        objects,
                        bundle,
                        statement,
                        trust=StatementTrust(PROVIDER, {"bench-v1": SECRET}),
                        code_revision=revision,
                        dirty=True,
                        workers=workers,
                        partitions=16,
                        timeout_seconds=120,
                    )
                row.update(
                    status="COMPLETE",
                    completion={
                        "run_id": str(result.completion.run_id),
                        "semantic_sha256": result.completion.semantic_sha256,
                        "result_sha256": result.completion.result_sha256,
                        "published": result.completion.published,
                    },
                    metrics=result.metrics,
                )
                with seeded_db.connect("reader") as conn:
                    from uuid import uuid4

                    reader = uuid4()
                    pinned = pin_run(
                        conn, WORKSPACE, objects, reader_id=reader, run_id=result.completion.run_id
                    )
                    assert pinned.result["reconciled"], pinned.result["issues"]
                    assert len(pinned.result["matched_provider_keys"]) == count
                    release_reader_pin(conn, WORKSPACE, reader_id=reader)
                if expected is None:
                    expected = result.completion.semantic_sha256
                assert expected == result.completion.semantic_sha256
            except RunError as exc:
                row.update(
                    status="FAILED",
                    error=exc.code,
                    outcome_unknown=exc.outcome_unknown,
                    metrics=exc.metrics,
                )
                raise
            finally:
                row.update(
                    wall_seconds=time.perf_counter() - started,
                    retained_object_bytes=tree_bytes(objects.root) - before,
                    remaining_temp_files=len(list(objects.root.rglob(".recon-*.tmp"))),
                )
                report["cases"].append(row)
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n")
                print(
                    json.dumps(
                        {
                            k: row[k]
                            for k in ("case", "workers", "status", "wall_seconds", "source_bytes")
                        }
                    ),
                    flush=True,
                )
