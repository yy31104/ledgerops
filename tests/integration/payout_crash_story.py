"""Public crash demonstration, using real processes and literal financial checks.

Invoked explicitly by make demo-payout-crash; the normal smoke test runs that
command in a child process. No existing database or real payment is accepted.
"""

import signal
import sqlite3
from datetime import UTC, datetime

import pytest

from ledgerops.export import capture_snapshot
from ledgerops.verification import verify_snapshot
from tests.integration.payout_helpers import admit, money
from tests.integration.test_payout_resolution import check_final
from tests.integration.test_payout_worker_process import assert_durable_claim, live_admission
from tests.provider.test_process import effect_rows, wait_for_gate

pytestmark = pytest.mark.integration


def narrate(step, message):
    print(f"[{step:02}] {message}", flush=True)


def test_payout_crash_story(seeded_db, golden, payment_provider, worker_factory):
    args, receipt, key, raw = live_admission(seeded_db, golden)
    assert money(seeded_db, golden)[:3] == (80, 62, 18)
    narrate(1, "Synthetic PLN minor units: merchant booked 80; payout 60 + fee 2 accepted.")
    narrate(2, "Hold 62 committed: booked / reserved / available = 80 / 62 / 18.")

    provider = payment_provider
    assert (
        provider.control("arm", {"provider_key": key, "point": "after_commit", "action": "pause"})[
            0
        ]
        == 200
    )
    original_worker = worker_factory(provider.ready["url"])
    original_worker.ready()
    wait_for_gate(provider)
    assert effect_rows(provider.path) == [(key, "60", "2", "62")]
    assert_durable_claim(seeded_db, golden, original_worker)
    narrate(3, "Provider SQLite committed the payment; its HTTP reply is deliberately paused.")
    narrate(4, "Ledger still UNKNOWN / HELD; durable transport record is MAY_HAVE_SENT.")

    original_worker.process.kill()
    original_worker.wait(-signal.SIGKILL)
    assert money(seeded_db, golden)[:3] == (80, 62, 18)
    narrate(5, "SIGKILL worker after provider commit: hold remains 62; no assumed failure.")
    assert provider.control("release", {})[0] == 200

    recovered = worker_factory(provider.ready["url"], "--max-calls", "1")
    recovered.ready()
    narrate(6, "Worker restarted; waiting for the real 10-second claim lease before recovery.")
    recovered.wait()
    check_final(seeded_db, golden, args[1], "SUCCEEDED")
    with seeded_db.connect("reader") as reader:
        attempts = reader.execute(
            "SELECT action FROM ledger.transport_attempt ORDER BY started_at"
        ).fetchall()
        assert attempts == [("PUT",), ("LOOKUP",)]
        assert reader.execute(
            "SELECT provider_key,provider_request FROM ledger.payout"
        ).fetchone() == (key, raw)
    narrate(7, "LOOKUP with the original payment key confirms SUCCEEDED; no second PUT.")

    replay = admit(seeded_db, golden, args, now=datetime.now(UTC))
    assert replay.http_status == 202 and replay.replayed and replay.body == receipt.body
    worker_factory(provider.ready["url"], "--once").wait()
    check_final(seeded_db, golden, args[1], "SUCCEEDED")
    with sqlite3.connect(provider.path) as connection:
        assert connection.execute("SELECT count(*) FROM executions").fetchone() == (1,)
    assert effect_rows(provider.path) == [(key, "60", "2", "62")]
    with seeded_db.connect("reader") as reader:
        assert reader.execute("SELECT count(*) FROM ledger.payout_resolution").fetchone() == (1,)
        assert reader.execute(
            "SELECT count(*) FROM ledger.journal WHERE operation_id=%s",
            (str(args[1].operation_id),),
        ).fetchone() == (1,)
        assert reader.execute("SELECT count(*) FROM ledger.transport_attempt").fetchone() == (2,)
        snapshot = capture_snapshot(reader, golden["workspace"]["id"])
    assert verify_snapshot(snapshot).healthy
    narrate(8, "Replay + another worker scan: provider effects = 1; ledger settlements = 1.")
    narrate(9, "PASS: booked / reserved / available = 18 / 0 / 18; independent verifier healthy.")
