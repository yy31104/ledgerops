"""Record a client history for every test in this directory and check it offline.

`db` wraps the shared per-test database fixture, so imported existing tests run
unchanged. At teardown the history is written to JSON Lines (also to
$LEDGEROPS_HISTORY_DIR when set), read back from that file and checked.
"""

import os
import re
from pathlib import Path

import pytest

from tests.history.checker import check, dump_history, load_history
from tests.history.recorder import Recorder, RecordingDatabase
from tests.integration.helpers import post_fixture, seed_accounts


@pytest.fixture
def history_recorder():
    return Recorder()


@pytest.fixture
def db(db, history_recorder, request, tmp_path):
    yield RecordingDatabase(db, history_recorder)
    with db.connect(role="admin") as admin:
        history = history_recorder.history(admin)
    path = tmp_path / "history.jsonl"
    dump_history(history, path)
    if os.environ.get("LEDGEROPS_HISTORY_DIR"):
        target = Path(os.environ["LEDGEROPS_HISTORY_DIR"])
        target.mkdir(parents=True, exist_ok=True)
        dump_history(
            history, target / (re.sub(r"[^A-Za-z0-9_.-]+", "_", request.node.nodeid) + ".jsonl")
        )
    request.node.history_path = path
    if history_recorder.expect_anomalies:
        return  # the test checks its own (deliberately broken) history
    recorded = load_history(path)
    # Guard against a vacuous pass: if either SQL marker stops matching, reads or writes
    # vanish from the history and no committed read-modify-write would remain.
    assert any(
        txn["status"] == "ok"
        and any(op[0] == "r" for op in txn["ops"])
        and any(op[0] == "w" for op in txn["ops"])
        for txn in recorded
    ), "recording observed no committed balance read-modify-write"
    report = check(recorded)
    assert report["valid"], f"isolation anomalies in recorded history {path}: {report['anomalies']}"


@pytest.fixture
def seeded_db(db, golden):
    """Same setup as tests/integration/conftest.py, on the recording database."""
    seed_accounts(db, golden)
    post_fixture(db, golden, "OPENING")
    return db
