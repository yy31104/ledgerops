"""The lab observes actual psycopg calls, including commit failures and retries."""

import ast
import json
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

from lab import trace
from lab.trace import TracedDatabase
from ledgerops.db.admission import submit_purchase
from ledgerops.db.maintenance import MaintenanceError, freeze_workspace
from tests.integration.helpers import post_fixture, seed_accounts
from tests.integration.purchase_helpers import purchase_input
from tests.integration.test_purchase_retries import _install_terminal_fault

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]


def _transaction_source(path, function):
    module = ast.parse((ROOT / path).read_text())
    definition = next(
        n for n in module.body if isinstance(n, ast.FunctionDef) and n.name == function
    )
    transaction = next(
        n
        for n in ast.walk(definition)
        if isinstance(n, ast.With)
        and any(
            isinstance(item.context_expr, ast.Call)
            and isinstance(item.context_expr.func, ast.Attribute)
            and item.context_expr.func.attr == "transaction"
            for item in n.items
        )
    )
    return {"path": path, "function": function, "line": transaction.lineno}


def test_trace_observes_purchase_transaction_and_code(db, golden):
    seed_accounts(db, golden)
    post_fixture(db, golden, "OPENING")
    traced = TracedDatabase(db)
    with traced.record() as record, traced.connect("app") as conn:
        result = submit_purchase(conn, *purchase_input(golden))
    assert result.http_status == 201
    assert record[0]["sql"] == "BEGIN"
    assert record[-1]["sql"] == "COMMIT"
    boundary = _transaction_source("src/ledgerops/db/admission.py", "_submit_once")
    assert record[0]["code"] == record[-1]["code"] == boundary
    assert [entry["index"] for entry in record] == list(range(1, len(record) + 1))
    assert all(entry["status"] == "ok" for entry in record)
    assert all(entry["attempt"] == 1 for entry in record)
    assert all(entry["role"] == "ledger_app" for entry in record)
    assert all(entry["duration_ms"] >= 0 for entry in record)
    locks = [entry for entry in record if "FOR UPDATE" in entry["sql"]]
    assert locks
    assert any(entry["code"]["function"] == "_lock_balances" for entry in locks)
    assert db.app_password not in json.dumps(record)


@pytest.mark.parametrize(
    "library_path", [".venv/lib/psycopg/transaction.py", "lib/site-packages/x.py"]
)
def test_source_skips_dependencies_even_when_installed_inside_repository(library_path):
    # Reproduces an in-repository virtualenv even when CI uses an external venv.
    namespace = {"source": trace._source}
    exec(
        compile("def library_hook():\n    return source()\n", str(ROOT / library_path), "exec"),
        namespace,
    )
    source, _ = namespace["library_hook"]()
    assert source["path"] == "tests/integration/test_lab_trace.py"
    assert source["function"] == (
        "test_source_skips_dependencies_even_when_installed_inside_repository"
    )


def test_purchase_rollback_points_to_actual_transaction_with_statement(db, golden):
    seed_accounts(db, golden)
    post_fixture(db, golden, "OPENING")
    _install_terminal_fault(db, golden, failures=1, sqlstate="23514")
    traced = TracedDatabase(db)
    with traced.record() as record, traced.connect("app") as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            submit_purchase(conn, *purchase_input(golden))
    assert record[0]["sql"] == "BEGIN" and record[-1]["sql"] == "ROLLBACK"
    boundary = _transaction_source("src/ledgerops/db/admission.py", "_submit_once")
    assert record[0]["code"] == record[-1]["code"] == boundary


def test_maintenance_boundaries_point_to_actual_transaction_with_statement(db, golden):
    seed_accounts(db, golden)
    traced = TracedDatabase(db)
    freeze, actor = str(uuid4()), str(uuid4())
    with traced.record() as record, traced.connect("maintenance") as conn:
        freeze_workspace(conn, golden["workspace"]["id"], freeze, actor, "inspect")
        with pytest.raises(MaintenanceError, match="IDENTITY_CONFLICT"):
            freeze_workspace(conn, golden["workspace"]["id"], freeze, actor, "changed reason")
    boundaries = [entry for entry in record if entry["sql"] in {"BEGIN", "COMMIT", "ROLLBACK"}]
    assert [entry["sql"] for entry in boundaries] == ["BEGIN", "COMMIT", "BEGIN", "ROLLBACK"]
    expected = _transaction_source("src/ledgerops/db/maintenance.py", "_transaction")
    assert all(entry["code"] == expected for entry in boundaries)


def test_trace_retains_real_query_failure_and_rollback(db):
    traced = TracedDatabase(db)
    with traced.record() as record, traced.connect("reader") as conn:
        with pytest.raises(psycopg.errors.DivisionByZero), conn.transaction():
            conn.execute("SELECT 1 / %s", (0,))
    assert [entry["sql"] for entry in record] == ["BEGIN", "SELECT 1 / %s", "ROLLBACK"]
    assert record[1]["params"] == [0]
    assert record[1]["status"] == "error"
    assert record[1]["sqlstate"] == "22012"
    assert record[2]["status"] == "ok"


def test_trace_observes_commit_failure(db):
    traced = TracedDatabase(db)
    with traced.connect("owner") as conn:
        conn.execute("CREATE TABLE trace_deferred (id int UNIQUE DEFERRABLE INITIALLY DEFERRED)")
    with traced.record() as record, traced.connect("owner") as conn:
        with pytest.raises(psycopg.errors.UniqueViolation), conn.transaction():
            conn.execute("INSERT INTO trace_deferred VALUES (1), (1)")
    assert [entry["sql"] for entry in record][-1] == "COMMIT"
    assert record[-1]["status"] == "error"
    assert record[-1]["sqlstate"] == "23505"


def test_suspend_omits_state_queries_and_records_nested_scope(db):
    traced = TracedDatabase(db)
    with traced.record() as outer, traced.connect("reader") as conn:
        conn.execute("SELECT 1")
        with traced.suspend():
            conn.execute("SELECT 2")
        with traced.record() as inner:
            conn.execute("SELECT 3")
        conn.execute("SELECT 4")
    assert [entry["sql"] for entry in outer] == ["SELECT 1", "SELECT 4"]
    assert [entry["sql"] for entry in inner] == ["SELECT 3"]


@pytest.mark.parametrize("deferred", [False, True], ids=["write-fault", "commit-fault"])
def test_trace_records_actual_abort_then_whole_transaction_retry(db, golden, deferred):
    seed_accounts(db, golden)
    post_fixture(db, golden, "OPENING")
    _install_terminal_fault(db, golden, failures=1, sqlstate="40001", deferred=deferred)
    traced = TracedDatabase(db)
    with traced.record() as record, traced.connect("app") as conn:
        result = submit_purchase(conn, *purchase_input(golden))
    assert result.http_status == 201
    starts = [entry for entry in record if entry["sql"] == "BEGIN"]
    assert [entry["attempt"] for entry in starts] == [1, 2]
    errors = [entry for entry in record if entry["status"] == "error"]
    assert len(errors) == 1 and errors[0]["sqlstate"] == "40001"
    assert errors[0]["attempt"] == 1
    assert (errors[0]["sql"] == "COMMIT") is deferred
    if not deferred:
        assert any(entry["sql"] == "ROLLBACK" for entry in record)
    # A failed COMMIT already aborts server state; psycopg issues no invented rollback.
    assert record[-1]["sql"] == "COMMIT" and record[-1]["attempt"] == 2
    assert record[-1]["status"] == "ok"
