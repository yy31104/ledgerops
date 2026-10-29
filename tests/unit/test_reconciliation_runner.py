"""Real spawned compute workers plus narrow orchestration failure checks."""

import multiprocessing
import os
import pickle
import signal
import struct
import time
from threading import Event
from uuid import uuid4

import pytest
from test_reconciliation import REVISION, SECRET, TRUST, signed
from test_reconciliation import data as data
from test_reconciliation import statement as statement
from test_verification import snapshot as snapshot

from ledgerops.export import load_export_bundle, publish_snapshot
from ledgerops.reconciliation import (
    merge_partition_results,
    prepare_inputs,
    runner,
    semantic_digest,
)
from ledgerops.reconciliation.publication import Completion, PublicationError


@pytest.fixture
def pinned(data, statement, tmp_path):
    bundle = load_export_bundle(publish_snapshot(data, tmp_path, code_revision=REVISION))
    raw = signed(statement)
    prepared = prepare_inputs(bundle, raw, trust=TRUST, code_revision=REVISION, partitions=4)
    return bundle, raw, prepared


def _crash(_pipe):
    os._exit(41)


def _hang(pipe):
    pipe.recv_bytes(runner.MAX_PREPARED_BYTES)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    Event().wait(60)


def _malformed(pipe):
    pipe.recv_bytes(runner.MAX_PREPARED_BYTES)
    pipe.send_bytes(b"not JSON")
    Event().wait(60)


def _paused_mid_frame(pipe):
    pipe.recv_bytes(runner.MAX_PREPARED_BYTES)
    pipe.send_bytes(runner.canonical({"kind": "READY", "pid": os.getpid()}))
    pipe.recv_bytes(32)
    # Deliberately corrupt the pinned CPython3.12 Connection wire stream: a
    # declared frame begins, then its writer stops before sending the body.
    os.write(pipe.fileno(), struct.pack("!i", 1_000_000))
    os.write(pipe.fileno(), b'{"kind":"RESULT"')
    os.kill(os.getpid(), signal.SIGSTOP)


@pytest.mark.parametrize("workers", [1, 2, 4])
def test_actual_spawn_pools_keep_fixed_plan_semantics_and_reap_owned_processes(pinned, workers):
    _, _, prepared = pinned
    baseline = semantic_digest(prepared.partition_plan)
    assert SECRET not in pickle.dumps(prepared)
    before = {process.pid for process in multiprocessing.active_children()}
    parts, metrics = runner._compute_partitions(prepared, workers=workers, timeout_seconds=15)
    report = merge_partition_results(prepared, parts)
    assert report["reconciled"] and len(report["matched_provider_keys"]) == 1
    assert semantic_digest(prepared.partition_plan) == baseline
    assert len(parts) == metrics["partitions"] == 4
    assert len(metrics["worker_pids"]) == workers and metrics["children_reaped"]
    assert {process.pid for process in multiprocessing.active_children()} == before
    assert len(metrics["partition_metrics"]) == 4
    assert metrics["largest_worker_max_rss_bytes"] == max(metrics["worker_max_rss_bytes"].values())


@pytest.mark.parametrize(
    "target,codes,timeout",
    [
        (_crash, {"WORKER_EXITED", "WORKER_IPC_FAILED"}, 10),
        (_hang, {"WORKER_TIMEOUT"}, 1),
        (_paused_mid_frame, {"WORKER_TIMEOUT"}, 1),
        (_malformed, {"WORKER_PROTOCOL_ERROR"}, 10),
    ],
)
def test_real_worker_crash_timeout_or_bad_frame_fails_whole_pool_and_cleans(
    pinned, target, codes, timeout
):
    before = {process.pid for process in multiprocessing.active_children()}
    started = time.monotonic()
    with pytest.raises(runner.RunError) as raised:
        runner._compute_partitions(pinned[2], workers=2, timeout_seconds=timeout, target=target)
    assert raised.value.code in codes
    assert raised.value.metrics["children_reaped"]
    assert time.monotonic() - started < 5
    assert {process.pid for process in multiprocessing.active_children()} == before


def test_owned_cleanup_failure_cannot_be_returned_as_success(pinned, monkeypatch):
    actual = runner._reap

    def report_failure(owned, *, success):
        assert actual(owned, success=success)
        return False

    monkeypatch.setattr(runner, "_reap", report_failure)
    with pytest.raises(runner.RunError, match="CHILD_CLEANUP_FAILED"):
        runner._compute_partitions(pinned[2], workers=1, timeout_seconds=15)


@pytest.mark.parametrize(
    "workers,timeout", [(0, 5), (3, 5), (True, 5), (1, 0), (1, 3601), (1, float("nan"))]
)
def test_resource_limits_reject_before_creating_children(pinned, workers, timeout):
    before = {process.pid for process in multiprocessing.active_children()}
    with pytest.raises(runner.RunError):
        runner._compute_partitions(pinned[2], workers=workers, timeout_seconds=timeout)
    assert {process.pid for process in multiprocessing.active_children()} == before


def mock_publication(monkeypatch):
    calls = []
    monkeypatch.setattr(
        runner, "register_object", lambda *args, **kwargs: calls.append("object") or object()
    )
    monkeypatch.setattr(runner, "begin_run", lambda *args, **kwargs: calls.append("begin"))
    monkeypatch.setattr(runner, "accept_partition", lambda *args, **kwargs: calls.append("accept"))
    monkeypatch.setattr(
        runner,
        "complete_run",
        lambda *args, **kwargs: (
            calls.append("complete") or Completion(kwargs["run_id"], "a" * 64, "b" * 64, True, 1)
        ),
    )
    monkeypatch.setattr(
        runner, "fail_run", lambda *args, **kwargs: calls.append("failed") or "FAILED"
    )
    return calls


def test_orchestrator_registers_all_inputs_then_partitions_and_completes_only_after_pool(
    pinned, monkeypatch
):
    calls = mock_publication(monkeypatch)
    result = runner.run_reconciliation(
        object(),
        object(),
        pinned[0],
        pinned[1],
        trust=TRUST,
        code_revision=REVISION,
        workers=1,
        partitions=4,
        timeout_seconds=15,
    )
    assert calls[:4] == ["object", "object", "object", "begin"]
    assert calls.count("accept") == 4 and calls[-1] == "complete" and "failed" not in calls
    assert result.completion.published and result.metrics["children_reaped"]


def test_parent_keyboard_interrupt_reaps_children_and_records_failed_run(pinned, monkeypatch):
    calls = mock_publication(monkeypatch)

    def interrupt(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(runner, "accept_partition", interrupt)
    before = {process.pid for process in multiprocessing.active_children()}
    with pytest.raises(runner.RunError) as raised:
        runner.run_reconciliation(
            object(),
            object(),
            pinned[0],
            pinned[1],
            trust=TRUST,
            code_revision=REVISION,
            workers=2,
            partitions=4,
            timeout_seconds=15,
        )
    assert raised.value.code == "RUN_INTERRUPTED" and raised.value.failure_recorded
    assert not raised.value.outcome_unknown and calls[-1] == "failed" and "complete" not in calls
    assert raised.value.metrics["children_reaped"]
    assert {process.pid for process in multiprocessing.active_children()} == before


@pytest.mark.parametrize("failure_recorded", [True, False])
def test_lost_completion_acknowledgement_is_never_reported_as_proven_rollback(
    pinned, monkeypatch, failure_recorded
):
    calls = mock_publication(monkeypatch)
    identity = uuid4()

    def lost_ack(*_args, **_kwargs):
        raise OSError("must not expose secret connection information")

    def fail(*_args, **_kwargs):
        if not failure_recorded:
            raise PublicationError("RUN_NOT_STAGING")
        return "FAILED"

    monkeypatch.setattr(runner, "complete_run", lost_ack)
    monkeypatch.setattr(runner, "fail_run", fail)
    with pytest.raises(runner.RunError) as raised:
        runner.run_reconciliation(
            object(),
            object(),
            pinned[0],
            pinned[1],
            trust=TRUST,
            code_revision=REVISION,
            workers=1,
            partitions=4,
            timeout_seconds=15,
            run_id=identity,
        )
    assert raised.value.code == "RUN_COMPLETION_FAILED" and raised.value.run_id == identity
    assert raised.value.failure_recorded == failure_recorded
    assert raised.value.outcome_unknown != failure_recorded
    assert "secret" not in str(raised.value)
    assert calls.count("accept") == 4
