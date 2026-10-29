"""Test-only subprocess fault injection; every gate is killed externally."""

import json
import os
import signal
import sys
from uuid import UUID, uuid4

import psycopg

from ledgerops.reconciliation import publication

phase, root, workspace = sys.argv[1:]
connection = psycopg.connect(os.environ["RECON_TEST_DSN"], autocommit=True)


def gate():
    print(
        json.dumps(
            {"phase": phase, "pid": os.getpid(), "backend_pid": connection.info.backend_pid}
        ),
        flush=True,
    )
    signal.pause()
    raise AssertionError("fault child must be killed, never resumed")


if phase == "partial_file":
    original = publication.os.fdopen

    class PartialFile:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def write(self, raw):
            self.stream.write(raw[: len(raw) // 2])
            self.stream.flush()
            gate()

    publication.os.fdopen = lambda *args, **kwargs: PartialFile(original(*args, **kwargs))
elif phase == "after_rename":
    original = publication.ObjectStore._promote

    def promote(self, *args):
        original(self, *args)
        gate()

    publication.ObjectStore._promote = promote
elif phase in {"registration_before_commit", "acceptance_before_commit"}:

    class GateCursor(psycopg.Cursor):
        def execute(self, query, params=None, **kwargs):
            result = super().execute(query, params, **kwargs)
            target = (
                "INSERT INTO ledger.evidence_pin"
                if phase == "registration_before_commit"
                else "DELETE FROM ledger.evidence_pin"
            )
            if type(query) is str and query.startswith(target):
                gate()
            return result

    connection.cursor_factory = GateCursor

store = publication.ObjectStore(root)
if phase.startswith("acceptance_"):
    args = json.loads(os.environ["RECON_TEST_ACCEPTANCE"])
    ref = publication.ObjectRef(args["sha256"], args["bytes"], UUID(args["pin_id"]))
    assert (
        publication.accept_partition(
            connection,
            workspace,
            store,
            run_id=args["run_id"],
            partition_id=args["partition_id"],
            result_ref=ref,
        )
        == "ACCEPTED"
    )
    gate()
else:
    publication.register_object(
        connection, workspace, store, b"publication-owned-test-bytes\n" * 200, publisher_id=uuid4()
    )
    raise AssertionError("registration failed to reach its causal gate")
