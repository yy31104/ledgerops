"""Bounded local reconciliation workers; only the parent publishes evidence."""

import multiprocessing
import os
import pickle
import signal
import sys
import time
from dataclasses import dataclass
from multiprocessing.connection import wait
from threading import Event, Lock, Timer
from uuid import UUID, uuid4

from ledgerops.reconciliation import PreparedInputs, match_partition, prepare_inputs
from ledgerops.reconciliation.publication import (
    Completion,
    accept_partition,
    begin_run,
    complete_run,
    fail_run,
    partition_envelope,
    register_object,
)
from ledgerops.reconciliation.statement import canonical, decode

MAX_PREPARED_BYTES = 64 * 1024 * 1024
MAX_RESULT_BYTES = 64 * 1024 * 1024


class RunError(RuntimeError):
    """Stable failure metadata; an unacknowledged COMMIT may already be visible."""

    def __init__(
        self, code, *, run_id=None, failure_recorded=False, outcome_unknown=False, metrics=None
    ):
        self.code = code
        self.run_id = run_id
        self.failure_recorded = failure_recorded
        self.outcome_unknown = outcome_unknown
        self.metrics = {} if metrics is None else metrics
        super().__init__(code)


@dataclass(frozen=True)
class RunResult:
    completion: Completion
    metrics: dict


@dataclass
class _Worker:
    process: object
    pipe: object
    pid: int
    ready: bool = False
    assignment: int | None = None


def _max_rss():
    try:
        import resource
    except ImportError:
        return None
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return value
    if sys.platform.startswith("linux"):
        return value * 1024
    return None


def _worker_main(pipe):
    """Trusted code receives only parent-owned pickle; responses are bounded JSON."""
    for name in list(os.environ):
        if name.startswith("LEDGEROPS_"):
            os.environ.pop(name)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    try:
        prepared = pickle.loads(pipe.recv_bytes(MAX_PREPARED_BYTES))
        if type(prepared) is not PreparedInputs:
            raise ValueError("prepared input required")
        pipe.send_bytes(canonical({"kind": "READY", "pid": os.getpid()}))
        while True:
            command = pipe.recv_bytes(32)
            if command == b"STOP":
                return
            partition_id = int(command)
            started = time.perf_counter()
            result = match_partition(prepared, partition_id)
            raw = canonical(
                {
                    "kind": "RESULT",
                    "pid": os.getpid(),
                    "partition": result,
                    "compute_seconds": time.perf_counter() - started,
                    "max_rss_bytes": _max_rss(),
                }
            )
            if len(raw) > MAX_RESULT_BYTES:
                raise ValueError("result size limit")
            pipe.send_bytes(raw)
    except BaseException:
        try:
            pipe.send_bytes(
                canonical(
                    {"kind": "ERROR", "pid": os.getpid(), "code": "WORKER_COMPUTATION_FAILED"}
                )
            )
        except (OSError, EOFError):
            pass
    finally:
        pipe.close()


def _limits(workers, timeout_seconds):
    if type(workers) is not int or workers not in {1, 2, 4}:
        raise RunError("INVALID_WORKER_COUNT")
    if type(timeout_seconds) not in {int, float} or not 0.1 <= timeout_seconds <= 3600:
        raise RunError("INVALID_POOL_TIMEOUT")


def _reap(owned, *, success):
    # Failed workers share no database/file locks; discard their dedicated pipes.
    for worker in owned:
        worker.pipe.close()
        if not success and worker.process.is_alive():
            worker.process.terminate()
    deadline = time.monotonic() + 2
    for worker in owned:
        worker.process.join(max(0, deadline - time.monotonic()))
    for worker in owned:
        if worker.process.is_alive():
            worker.process.kill()
    deadline = time.monotonic() + 2
    clean = True
    for worker in owned:
        worker.process.join(max(0, deadline - time.monotonic()))
        if worker.process.is_alive():
            clean = False
        else:
            worker.process.close()
    return clean


def _compute_partitions(prepared, *, workers, timeout_seconds, on_result=None, target=None):
    """A fixed spawn pool with one outstanding job per worker and no reassignment."""
    _limits(workers, timeout_seconds)
    if type(prepared) is not PreparedInputs:
        raise RunError("PREPARED_INPUTS_REQUIRED")
    raw_prepared = pickle.dumps(prepared, protocol=5)
    if len(raw_prepared) > MAX_PREPARED_BYTES:
        raise RunError("PREPARED_INPUT_SIZE_LIMIT")
    target = _worker_main if target is None else target
    context = multiprocessing.get_context("spawn")
    owned, lock, expired = [], Lock(), Event()
    started = time.perf_counter()
    deadline = time.monotonic() + timeout_seconds
    plan = prepared.partition_plan["partitions"]
    waiting = iter(part["partition_id"] for part in plan)
    completed, results = set(), []
    metrics = {
        "workers": workers,
        "partitions": len(plan),
        "prepared_pickle_bytes": len(raw_prepared),
        "pool_timeout_seconds": timeout_seconds,
        "result_ipc_bytes": 0,
        "partition_metrics": [],
        "worker_pids": [],
        "worker_max_rss_bytes": {},
    }
    success = False

    def expire():
        # Also bounds a worker paused midway through a large framed IPC write:
        # killing that owned process makes recv_bytes observe EOF instead of hang.
        expired.set()
        with lock:
            active = list(owned)
        for worker in active:
            try:
                if worker.process.is_alive():
                    worker.process.kill()
            except (OSError, ValueError):
                pass

    timer = Timer(timeout_seconds, expire)
    timer.daemon = True
    timer.start()
    try:
        for _ in range(workers):
            if expired.is_set():
                raise RunError("WORKER_TIMEOUT")
            parent_pipe, child_pipe = context.Pipe()
            process = context.Process(
                target=target, args=(child_pipe,), name="ledgerops-recon-worker"
            )
            try:
                process.start()
            except BaseException:
                parent_pipe.close()
                child_pipe.close()
                raise
            child_pipe.close()
            worker = _Worker(process, parent_pipe, process.pid)
            with lock:
                owned.append(worker)
            metrics["worker_pids"].append(process.pid)
            if expired.is_set():
                raise RunError("WORKER_TIMEOUT")
            parent_pipe.send_bytes(raw_prepared)
        while len(completed) != len(plan):
            remaining = deadline - time.monotonic()
            if expired.is_set() or remaining <= 0:
                raise RunError("WORKER_TIMEOUT")
            handles = [item for worker in owned for item in (worker.pipe, worker.process.sentinel)]
            ready = wait(handles, timeout=remaining)
            if not ready or expired.is_set():
                raise RunError("WORKER_TIMEOUT")
            if any(worker.process.sentinel in ready for worker in owned):
                raise RunError("WORKER_EXITED")
            for worker in owned:
                if worker.pipe not in ready:
                    continue
                raw = worker.pipe.recv_bytes(MAX_RESULT_BYTES)
                metrics["result_ipc_bytes"] += len(raw)
                if metrics["result_ipc_bytes"] > MAX_RESULT_BYTES:
                    raise RunError("TOTAL_RESULT_SIZE_LIMIT")
                try:
                    message = decode(raw)
                except ValueError as exc:
                    raise RunError("WORKER_PROTOCOL_ERROR") from exc
                if type(message) is not dict or message.get("pid") != worker.pid:
                    raise RunError("WORKER_PROTOCOL_ERROR")
                if message.get("kind") == "ERROR":
                    raise RunError("WORKER_COMPUTATION_FAILED")
                if message.get("kind") == "READY" and not worker.ready:
                    if message.keys() != {"kind", "pid"}:
                        raise RunError("WORKER_PROTOCOL_ERROR")
                    worker.ready = True
                elif message.get("kind") == "RESULT" and worker.ready:
                    if message.keys() != {
                        "kind",
                        "pid",
                        "partition",
                        "compute_seconds",
                        "max_rss_bytes",
                    }:
                        raise RunError("WORKER_PROTOCOL_ERROR")
                    part = message["partition"]
                    if (
                        type(part) is not dict
                        or part.get("partition_id") != worker.assignment
                        or worker.assignment in completed
                    ):
                        raise RunError("WORKER_PROTOCOL_ERROR")
                    elapsed, rss = message["compute_seconds"], message["max_rss_bytes"]
                    if (
                        type(elapsed) not in {int, float}
                        or not 0 <= elapsed <= 3600
                        or (rss is not None and (type(rss) is not int or rss < 0))
                    ):
                        raise RunError("WORKER_PROTOCOL_ERROR")
                    completed.add(worker.assignment)
                    metrics["partition_metrics"].append(
                        {
                            "partition_id": worker.assignment,
                            "compute_seconds": elapsed,
                            "worker_pid": worker.pid,
                        }
                    )
                    if rss is not None:
                        previous = metrics["worker_max_rss_bytes"].get(str(worker.pid), 0)
                        metrics["worker_max_rss_bytes"][str(worker.pid)] = max(previous, rss)
                    if on_result is None:
                        results.append(part)
                    else:
                        try:
                            on_result(part)
                        except Exception as exc:
                            raise RunError("PARTITION_PUBLICATION_FAILED", metrics=metrics) from exc
                else:
                    raise RunError("WORKER_PROTOCOL_ERROR")
                worker.assignment = next(waiting, None)
                if worker.assignment is not None:
                    worker.pipe.send_bytes(str(worker.assignment).encode("ascii"))
        if expired.is_set():
            raise RunError("WORKER_TIMEOUT")
        for worker in owned:
            worker.pipe.send_bytes(b"STOP")
        for worker in owned:
            worker.process.join(max(0, deadline - time.monotonic()))
            if expired.is_set() or worker.process.is_alive():
                raise RunError("WORKER_TIMEOUT")
            if worker.process.exitcode != 0:
                raise RunError("WORKER_EXITED")
        success = True
        return results, metrics
    except (EOFError, BrokenPipeError, OSError) as exc:
        raise RunError(
            "WORKER_TIMEOUT" if expired.is_set() else "WORKER_IPC_FAILED", metrics=metrics
        ) from exc
    except RunError as exc:
        exc.metrics = metrics
        raise
    except BaseException as exc:
        code = (
            "RUN_INTERRUPTED"
            if isinstance(exc, (KeyboardInterrupt, SystemExit))
            else "WORKER_FAILED"
        )
        raise RunError(code, metrics=metrics) from exc
    finally:
        timer.cancel()
        timer.join()
        metrics["children_reaped"] = _reap(owned, success=success)
        metrics["pool_seconds"] = time.perf_counter() - started
        metrics["partition_metrics"].sort(key=lambda item: item["partition_id"])
        metrics["largest_worker_max_rss_bytes"] = max(
            metrics["worker_max_rss_bytes"].values(), default=None
        )
        metrics["parent_process_lifetime_max_rss_bytes"] = _max_rss()
        if success and not metrics["children_reaped"]:
            raise RunError("CHILD_CLEANUP_FAILED", metrics=metrics)


def run_reconciliation(
    conn,
    store,
    bundle,
    statement_bytes,
    *,
    trust,
    code_revision,
    dirty=False,
    workers=1,
    partitions=16,
    timeout_seconds=300,
    run_id=None,
):
    """Prepare pinned facts, compute bounded partitions, and publish only a complete run."""
    _limits(workers, timeout_seconds)
    try:
        identifier = uuid4() if run_id is None else UUID(str(run_id))
        if run_id is not None and str(identifier) != str(run_id):
            raise ValueError("noncanonical run ID")
    except (ValueError, TypeError, AttributeError) as exc:
        raise RunError("INVALID_RUN_ID") from exc
    started, beginning_attempted = time.perf_counter(), False
    workspace, phase = None, "PREPARE"
    metrics = {"workers": workers, "partitions": partitions, "accepted_partition_bytes": 0}
    try:
        prepared = prepare_inputs(
            bundle,
            statement_bytes,
            trust=trust,
            code_revision=code_revision,
            dirty=dirty,
            partitions=partitions,
        )
        workspace = prepared.input_manifest["workspace_id"]
        metrics["prepare_seconds"] = time.perf_counter() - started
        phase = "INPUT_REGISTRATION"
        sources = {
            "ledger_manifest": bundle.manifest_bytes,
            "ledger_data": bundle.data_bytes,
            "statement": statement_bytes,
        }
        metrics["input_bytes"] = sum(len(raw) for raw in sources.values())
        pins = {
            name: register_object(conn, workspace, store, raw, publisher_id=identifier)
            for name, raw in sources.items()
        }
        phase, beginning_attempted = "BEGIN", True
        begin_run(conn, store, prepared, run_id=identifier, input_pins=pins)
        phase = "PARTITIONS"

        def publish(part):
            envelope = partition_envelope(prepared, identifier, part)
            raw = canonical(envelope)
            ref = register_object(conn, workspace, store, raw, publisher_id=identifier)
            accept_partition(
                conn,
                workspace,
                store,
                run_id=identifier,
                partition_id=part["partition_id"],
                result_ref=ref,
            )
            metrics["accepted_partition_bytes"] += len(raw)

        _, pool_metrics = _compute_partitions(
            prepared, workers=workers, timeout_seconds=timeout_seconds, on_result=publish
        )
        metrics.update(pool_metrics)
        phase = "COMPLETE"
        before_complete = time.perf_counter()
        completion = complete_run(conn, workspace, store, prepared, run_id=identifier)
        metrics["completion_seconds"] = time.perf_counter() - before_complete
        metrics["elapsed_seconds"] = time.perf_counter() - started
        return RunResult(completion, metrics)
    except BaseException as exc:
        if isinstance(exc, RunError):
            code = exc.code
            metrics.update(exc.metrics)
        elif isinstance(exc, (KeyboardInterrupt, SystemExit)):
            code = "RUN_INTERRUPTED"
        else:
            code = {
                "PREPARE": "INPUT_VALIDATION_FAILED",
                "INPUT_REGISTRATION": "INPUT_REGISTRATION_FAILED",
                "BEGIN": "RUN_BEGIN_FAILED",
                "PARTITIONS": "PARTITION_PUBLICATION_FAILED",
                "COMPLETE": "RUN_COMPLETION_FAILED",
            }[phase]
        failed = False
        if beginning_attempted:
            try:
                failed = fail_run(conn, workspace, run_id=identifier, code=code) in {
                    "FAILED",
                    "ALREADY_FAILED",
                }
            except Exception:
                pass  # Never infer rollback or STAGING from a lost COMMIT acknowledgement.
        metrics.update(elapsed_seconds=time.perf_counter() - started, failed_phase=phase)
        raise RunError(
            code,
            run_id=identifier,
            failure_recorded=failed,
            outcome_unknown=beginning_attempted and not failed,
            metrics=metrics,
        ) from exc
