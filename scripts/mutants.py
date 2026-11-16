"""Remove one guard in a disposable source copy and compare its focused tests."""

import argparse
import ast
import difflib
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
DISPATCH = "src/ledgerops/db/payout_dispatch.py"
RESOLUTION = "src/ledgerops/db/payout_resolution.py"
FOUNDATION = "src/ledgerops/db/sql/001_foundation.sql"
ADMISSION = "src/ledgerops/db/admission.py"
DISPATCH_TEST = "tests/integration/test_payout_dispatch.py::"


@dataclass(frozen=True)
class Mutant:
    edits: tuple[tuple[str, str, str], ...]
    tests: tuple[str, ...]
    rule: str


MUTANTS = {
    "claim-before-commit": Mutant(
        (
            (
                DISPATCH,
                "def claim_payout(conn, workspace, payout_id, *, clock=utc_now, lease_seconds=30):",
                (
                    "def claim_payout(conn, workspace, payout_id, *, cloc"
                    "k=utc_now, lease_seconds=30, before_commit=None):"
                ),
            ),
            (
                DISPATCH,
                '                payout["dispatch_deadline"],\n            )\n        return claim',
                (
                    '                payout["dispatch_deadline"],\n       '
                    "     )\n            if before_commit is not None:\n   "
                    "             before_commit(claim)\n        return cla"
                    "im"
                ),
            ),
            (
                DISPATCH,
                (
                    "    claim = claim_payout(conn, workspace, payout_id,"
                    " clock=clock, lease_seconds=lease_seconds)"
                ),
                (
                    "    early = []\n    claim = claim_payout(conn, worksp"
                    "ace, payout_id, clock=clock, lease_seconds=lease_sec"
                    "onds,\n                         before_commit=lambda "
                    "c: early.append(_transport(client, c)))"
                ),
            ),
            (
                DISPATCH,
                "        response = _transport(client, claim)",
                "        response = early[0]",
            ),
        ),
        (
            DISPATCH_TEST
            + (
                "test_network_begins_only_after_claim_is_committed_an"
                "d_no_financial_transaction_is_open"
            ),
        ),
        "Commit MAY_HAVE_SENT before transport",
    ),
    "provider-key": Mutant(
        (
            (
                DISPATCH,
                "def _transport(client, claim):\n",
                (
                    "def _transport(client, claim):\n    from dataclasses "
                    "import replace\n    claim = replace(claim, provider_k"
                    "ey=claim.provider_key.rsplit('.', 1)[0] + '.' + uuid"
                    "4().hex * 2)\n"
                ),
            ),
        ),
        (
            DISPATCH_TEST
            + "test_authenticated_not_found_resends_exact_original_identity_before_deadline",
        ),
        "Reuse the retained provider key",
    ),
    "timeout-hold": Mutant(
        (
            (
                DISPATCH,
                (
                    "            now = utc_instant(clock())\n            c"
                    'ur.execute(\n                "UPDATE ledger.transport'
                    '_attempt SET "'
                ),
                (
                    "            now = utc_instant(clock())\n            i"
                    'f outcome == "TIMEOUT":\n                cur.execute('
                    '\n                    "UPDATE ledger.account_balance '
                    'SET reserved=reserved-%s,version=version+1 "\n       '
                    '             "WHERE workspace_id=%s AND account_id=%'
                    's",\n                    (payout["gross_minor"], clai'
                    'm.workspace_id, payout["merchant_account_id"]),\n    '
                    "            )\n            cur.execute(\n             "
                    '   "UPDATE ledger.transport_attempt SET "'
                ),
            ),
        ),
        (
            DISPATCH_TEST
            + "test_transport_failures_keep_hold_and_store_transport_fact[TimeoutError]",
        ),
        "Keep reserved funds after transport timeout",
    ),
    "claim-lease": Mutant(
        (
            (
                DISPATCH,
                '            if now < intent["next_due_at"]:\n                return None',
                (
                    "            if False:  # mutant: ignore the persiste"
                    "d lease\n                return None"
                ),
            ),
        ),
        (
            DISPATCH_TEST
            + "test_abandoned_claim_is_due_after_lease_and_recovers_by_lookup_not_new_put",
        ),
        "Wait until the persisted claim lease expires",
    ),
    "observation-replay": Mutant(
        (
            (
                RESOLUTION,
                "        if resolution is not None:",
                "        if False:  # mutant: re-enter settlement for a resolved payout",
            ),
            (
                RESOLUTION,
                "        if not new:\n            return ObservationResult",
                (
                    "        if False:  # mutant: re-enter settlement for"
                    " an existing event\n            return ObservationRes"
                    "ult"
                ),
            ),
        ),
        (
            (
                "tests/integration/test_payout_resolution.py::test_id"
                "entical_event_across_receiving_channels_does_not_rep"
                "eat_money[CALLBACK]"
            ),
        ),
        "Replay terminal observations without another settlement",
    ),
    "balance-lock": Mutant(
        (
            (
                ADMISSION,
                "WHERE a.workspace_id=%s AND a.id=%s FOR UPDATE OF b",
                "WHERE a.workspace_id=%s AND a.id=%s",
            ),
        ),
        (
            "tests/integration/test_purchase_concurrency.py::"
            "test_two_distinct_debits_of_80_from_100_commit_one_success_and_one_saved_rejection",
        ),
        "Lock balance rows before checking available funds",
    ),
    "balance-floor": Mutant(
        (
            (
                FOUNDATION,
                (
                    "  CHECK ((CASE normal_side WHEN 'D' THEN debits - cr"
                    "edits ELSE credits - debits END)\n         >= reserve"
                    "d)"
                ),
                "  CHECK (true)",
            ),
        ),
        ("tests/integration/test_schema.py::test_projection_row_bounds_and_generated_booked",),
        "Enforce booked >= reserved in PostgreSQL",
    ),
    "idempotency-key": Mutant(
        (
            (
                FOUNDATION,
                "  PRIMARY KEY (workspace_id, client_id, route_family, key),",
                "  PRIMARY KEY (workspace_id, client_id, route_family, key, operation_id),",
            ),
        ),
        (
            (
                "tests/integration/test_purchase_admission.py::test_s"
                "ame_key_changed_payload_conflicts_and_preserves_ever"
                "y_row[operation]"
            ),
        ),
        "Make the scoped idempotency key unique across operations",
    ),
}


def digest_sources(root):
    digest = hashlib.sha256()
    for path in sorted((Path(root) / "src").rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            digest.update(path.relative_to(root).as_posix().encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()


def apply_mutant(root, mutant):
    """Validate every anchor before changing any file, then return the exact patch."""
    texts, originals = {}, {}
    for name, before, after in mutant.edits:
        path = Path(root) / name
        if name not in texts:
            originals[name] = texts[name] = path.read_text()
        if texts[name].count(before) != 1:
            raise ValueError(f"mutation anchor must occur exactly once: {name}")
        texts[name] = texts[name].replace(before, after, 1)
    for name, text in texts.items():
        if name.endswith(".py"):
            ast.parse(text, filename=name)
    patch = "".join(
        "".join(
            difflib.unified_diff(
                originals[name].splitlines(True),
                text.splitlines(True),
                fromfile="a/" + name,
                tofile="b/" + name,
            )
        )
        for name, text in texts.items()
    )
    for name, text in texts.items():
        (Path(root) / name).write_text(text)
    return patch


def copy_source(root, target, label):
    names = (
        subprocess.check_output(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=root
        )
        .decode()
        .split("\0")
    )
    for name in filter(None, names):
        path = Path(name)
        source = root / path
        if path.is_absolute() or ".." in path.parts or source.is_symlink():
            raise ValueError("only regular repository files can enter the temporary copy")
        if source.is_file():
            destination = target / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(source.read_bytes())
    # Only this copy's fixtures get a unique cleanup label, including on timeout.
    fixture = target / "tests/conftest.py"
    text = fixture.read_text()
    before = '                "ledgerops.scope=disposable-test",'
    if text.count(before) != 1:
        raise ValueError("owned-container fixture marker changed")
    fixture.write_text(
        text.replace(
            before,
            before
            + f'\n                "--label",\n                "ledgerops.mutant-run={label}",',
        )
    )


def cleanup_containers(label):
    result = subprocess.run(
        ["docker", "ps", "-aq", "--filter", f"label=ledgerops.mutant-run={label}"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode:
        raise RuntimeError("cannot verify owned test-container cleanup")
    ids = result.stdout.split()
    if ids:
        subprocess.run(
            ["docker", "rm", "--force", *ids], check=True, capture_output=True, timeout=30
        )


def junit_result(path, returncode):
    if not path.exists():
        return {"status": "invalid", "reason": "missing JUnit report", "failed_tests": []}
    try:
        report = ET.parse(path)
    except ET.ParseError:
        return {"status": "invalid", "reason": "malformed JUnit report", "failed_tests": []}
    cases = report.findall(".//testcase")
    errors = [case for case in cases if case.find("error") is not None]
    failures = [case for case in cases if case.find("failure") is not None]
    skipped = [case for case in cases if case.find("skipped") is not None]
    infrastructure = any(
        word in (failure.text or "")
        for case in failures
        for failure in case.findall("failure")
        for word in ("psycopg.OperationalError", "psycopg.InterfaceError", "docker.errors.")
    )
    status = (
        "passed"
        if returncode == 0 and cases and not skipped and not errors and not failures
        else "failed"
        if returncode == 1 and failures and not errors and not infrastructure
        else "invalid"
    )
    return {
        "status": status,
        "cases": len(cases),
        "test_ids": [
            case.get("classname", "").replace(".", "/") + ".py::" + case.get("name", "")
            for case in cases
        ],
        "failed_tests": [
            case.get("classname", "").replace(".", "/") + ".py::" + case.get("name", "")
            for case in failures
        ],
        "errors": len(errors),
        "skipped": len(skipped),
    }


def run_tests(copy, tests, artifacts, label, timeout):
    artifacts = artifacts.resolve()
    artifacts.mkdir()
    xml = artifacts / "junit.xml"
    env = {
        **os.environ,
        "PYTHONPATH": str(copy / "src") + os.pathsep + str(copy),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTEST_ADDOPTS": "",
        "LEDGEROPS_HISTORY_DIR": str(artifacts / "histories"),
    }
    started = time.monotonic()
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys,pathlib,ledgerops; "
            "assert pathlib.Path(ledgerops.__file__).resolve().is_relative_to("
            "pathlib.Path.cwd()/'src'); "
            "import pytest; sys.exit(pytest.main(sys.argv[1:]))",
            "-q",
            "--tb=short",
            "--junitxml",
            str(xml),
            "--basetemp",
            str(artifacts / "runtime"),
            *tests,
        ],
        cwd=copy,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    was_interrupted = False
    try:
        log, _ = proc.communicate(timeout=timeout)
    except (subprocess.TimeoutExpired, KeyboardInterrupt) as interrupted:
        was_interrupted = isinstance(interrupted, KeyboardInterrupt)
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            log, _ = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            log, _ = proc.communicate()
        result = {
            "status": "invalid",
            "failed_tests": [],
            "reason": "interrupted"
            if isinstance(interrupted, KeyboardInterrupt)
            else "runner deadline exceeded",
        }
    else:
        result = junit_result(xml, proc.returncode)
    finally:
        try:
            cleanup_containers(label)
        except (RuntimeError, OSError, subprocess.SubprocessError):
            result = {
                "status": "invalid",
                "reason": "owned-container cleanup failed",
                "failed_tests": [],
            }
    # Raw evidence remains outside Git, but local path metadata is made portable.
    for source, replacement in [
        (str(copy), "$SOURCE_COPY"),
        (str(ROOT), "$REPO_ROOT"),
        (str(artifacts), "$EVIDENCE"),
        (str(Path.home()), "$USER_HOME"),
    ]:
        log = log.replace(source, replacement)
        if xml.exists():
            xml.write_text(xml.read_text().replace(source, replacement))
    if xml.exists():
        report = ET.parse(xml)
        for suite in report.findall(".//testsuite"):
            if "hostname" in suite.attrib:
                suite.set("hostname", "redacted-host")
        report.write(xml, encoding="unicode")
    (artifacts / "output.txt").write_text(log)
    return {**result, "exit_code": proc.returncode, "seconds": round(time.monotonic() - started, 3)}


def exercise(root, name, artifacts, timeout=120):
    mutant = MUTANTS[name]
    before = digest_sources(root)
    record = {
        "name": name,
        "rule": mutant.rule,
        "tests": mutant.tests,
        "source_sha256": before,
        "status": "invalid",
    }
    artifacts.mkdir()
    try:
        for trial in ("control", "mutant"):
            label = uuid4().hex
            with tempfile.TemporaryDirectory(prefix="ledgerops-mutant-") as directory:
                copy = Path(directory)
                copy_source(root, copy, label)
                if trial == "mutant":
                    patch = apply_mutant(copy, mutant)
                    (artifacts / "mutation.patch").write_text(patch)
                    record["patch_sha256"] = hashlib.sha256(patch.encode()).hexdigest()
                record[trial] = run_tests(copy, mutant.tests, artifacts / trial, label, timeout)
                if record[trial]["status"] in {"passed", "failed"} and record[trial].get(
                    "test_ids"
                ) != list(mutant.tests):
                    record[trial]["status"] = "invalid"
                    record[trial]["reason"] = "executed tests differ from the selected nodes"
            if record[trial].get("interrupted") or record[trial].get("reason") == "interrupted":
                raise KeyboardInterrupt
            if trial == "control" and record[trial]["status"] != "passed":
                break
        if record.get("control", {}).get("status") == "passed":
            control, changed = record["control"], record.get("mutant", {})
            if control.get("test_ids") == changed.get("test_ids"):
                if changed.get("status") == "failed":
                    record["status"] = "killed"
                elif changed.get("status") == "passed":
                    record["status"] = "survived"
    except (ValueError, SyntaxError, OSError, RuntimeError, subprocess.SubprocessError) as error:
        record["error_type"] = type(error).__name__
    finally:
        record["source_unchanged"] = before == digest_sources(root)
        if not record["source_unchanged"]:
            record["status"] = "invalid"
        (artifacts / "result.json").write_text(json.dumps(record, indent=2) + "\n")
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name", choices=[*MUTANTS, "all"])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args()
    output = args.output or ROOT / ".cache/mutants" / (
        time.strftime("%Y%m%d-%H%M%S-") + uuid4().hex[:8]
    )
    if args.timeout < 1 or output.resolve().is_relative_to(ROOT / "src"):
        parser.error("positive deadline and an evidence directory outside src are required")
    output = output.resolve()
    if output.exists():
        parser.error("evidence directory already exists; choose a new path")
    output.mkdir(parents=True, exist_ok=False)
    names = list(MUTANTS) if args.name == "all" else [args.name]
    records = [exercise(ROOT, name, output / name, args.timeout) for name in names]
    summary = {
        "schema_version": 1,
        "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "source_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "rules": len(records),
        "killed": sum(r["status"] == "killed" for r in records),
        "survived": sum(r["status"] == "survived" for r in records),
        "invalid": sum(r["status"] == "invalid" for r in records),
        "records": records,
    }
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps({key: summary[key] for key in ("rules", "killed", "survived", "invalid")}))
    return 2 if summary["invalid"] else 1 if summary["survived"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
