"""The checker against hand-written histories, before trusting it on recorded ones.

Each history is small enough to verify by hand. Every anomaly class the checker
reports has one positive case here, and the serial histories prove it does not flag
ordinary dependencies.
"""

import json

import pytest

from tests.history.checker import check, dump_history, load_history, main


def txn(index, *ops, status="ok", process=None):
    return {
        "index": index,
        "process": index if process is None else process,
        "status": status,
        "ops": [list(op) for op in ops],
    }


def names(report):
    return set(report["anomalies"])


def test_serial_read_modify_write_chain_is_valid():
    history = [
        txn(0, ("r", "x", 0), ("w", "x", 1)),
        txn(1, ("r", "x", 1), ("w", "x", 2)),
        txn(2, ("r", "x", 2)),
    ]
    report = check(history)
    assert report["valid"], report
    # Each reader is also the next writer, so its anti-dependency would be a self-edge.
    assert report["stats"]["edges"] == {"ww": 1, "wr": 2, "rw": 0}


def test_reader_of_old_version_before_writer_is_valid():
    # T1 read x0 and T0 then installed x1: rw T1 -> T0 only, no cycle.
    history = [txn(0, ("r", "x", 0), ("w", "x", 1)), txn(1, ("r", "x", 0))]
    assert check(history)["valid"]


def test_classic_lost_update_with_absolute_writes():
    # Both read x0 and both wrote "x1": the second write overwrote the first.
    history = [
        txn(0, ("r", "x", 0), ("w", "x", 1)),
        txn(1, ("r", "x", 0), ("w", "x", 1)),
    ]
    report = check(history)
    assert {"lost-update", "duplicate-version", "G2-item"} <= names(report)
    assert report["anomalies"]["lost-update"] == [{"key": "x", "read_version": 0, "txns": [0, 1]}]


def test_lost_update_hidden_by_relative_sql_update_is_g_single():
    # SQL "version=version+1" re-reads the row, so both writes survive (x1 then x2),
    # but T1 decided on x0 while T0 was installing x1. Final values look fine.
    history = [
        txn(0, ("r", "x", 0), ("w", "x", 1)),
        txn(1, ("r", "x", 0), ("w", "x", 2)),
    ]
    report = check(history)
    assert {"lost-update", "non-adjacent-rmw", "G-single"} <= names(report)
    cycle = report["anomalies"]["G-single"][0]["cycle"]
    assert sorted(step["kinds"][0] for step in cycle) == ["rw", "ww"]


def test_write_skew_is_g2_item_without_lost_update():
    history = [
        txn(0, ("r", "x", 0), ("r", "y", 0), ("w", "x", 1)),
        txn(1, ("r", "x", 0), ("r", "y", 0), ("w", "y", 1)),
    ]
    report = check(history)
    assert names(report) == {"G2-item"}
    assert all(step["kinds"] == ["rw"] for step in report["anomalies"]["G2-item"][0]["cycle"])


def test_g0_write_cycle():
    history = [
        txn(0, ("w", "x", 1), ("w", "y", 2)),
        txn(1, ("w", "x", 2), ("w", "y", 1)),
    ]
    assert "G0" in names(check(history))


def test_g1c_circular_information_flow():
    history = [
        txn(0, ("w", "x", 1), ("r", "y", 1)),
        txn(1, ("w", "y", 1), ("r", "x", 1)),
    ]
    report = check(history)
    assert "G1c" in names(report)
    assert "G0" not in names(report)


def test_g1a_aborted_read():
    history = [txn(0, ("w", "x", 1), status="fail"), txn(1, ("r", "x", 1))]
    assert names(check(history)) == {"G1a"}


def test_g1b_intermediate_read():
    history = [txn(0, ("w", "x", 1), ("w", "x", 2)), txn(1, ("r", "x", 1))]
    assert "G1b" in names(check(history))


def test_failed_transactions_do_not_create_dependencies():
    history = [
        txn(0, ("r", "x", 0), ("w", "x", 1)),
        txn(1, ("r", "x", 0), ("w", "x", 1), status="fail"),
    ]
    assert check(history)["valid"]


def test_indeterminate_write_counts_only_when_observed():
    unobserved = [txn(0, ("r", "x", 0), ("w", "x", 1)), txn(1, ("r", "x", 0), status="info")]
    assert check(unobserved)["valid"]
    observed = [
        txn(0, ("r", "x", 0), ("w", "x", 1), status="info"),
        txn(1, ("r", "x", 0), ("w", "x", 1)),
        txn(2, ("r", "x", 1)),
    ]
    assert "duplicate-version" in names(check(observed))


def test_malformed_history_is_rejected():
    with pytest.raises(ValueError):
        check([{"index": 0, "status": "committed", "ops": []}])
    with pytest.raises(ValueError):
        check([txn(0, ("r", "x", "1"))])


def test_cli_reads_the_file_offline(tmp_path, capsys):
    path = tmp_path / "history.jsonl"
    dump_history([txn(0, ("r", "x", 0), ("w", "x", 1)), txn(1, ("r", "x", 0), ("w", "x", 2))], path)
    assert load_history(path)[1]["ops"] == [["r", "x", 0], ["w", "x", 2]]
    assert main([str(path)]) == 1
    assert "lost-update" in json.loads(capsys.readouterr().out)["anomalies"]
    dump_history([txn(0, ("r", "x", 0), ("w", "x", 1))], path)
    assert main([str(path)]) == 0
