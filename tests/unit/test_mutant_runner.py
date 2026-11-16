"""A mutation is evidence only after a passing control and a valid failing test."""

import ast
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from scripts import mutants


@pytest.mark.parametrize("name", mutants.MUTANTS)
def test_each_patch_is_exact_compiles_and_leaves_original_source_unchanged(tmp_path, name):
    before = mutants.digest_sources(mutants.ROOT)
    for path, _, _ in mutants.MUTANTS[name].edits:
        source = mutants.ROOT / path
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    patch = mutants.apply_mutant(tmp_path, mutants.MUTANTS[name])
    assert patch.startswith("--- a/src/") and "+++ b/src/" in patch
    for path, _, _ in mutants.MUTANTS[name].edits:
        if path.endswith(".py"):
            ast.parse((tmp_path / path).read_text())
    assert mutants.digest_sources(mutants.ROOT) == before


def test_changed_or_ambiguous_anchor_never_partially_applies_a_patch(tmp_path):
    path = tmp_path / "example.py"
    path.write_text("value = 1\nvalue = 1\n")
    mutation = mutants.Mutant((("example.py", "value = 1", "value = 2"),), (), "unit")
    with pytest.raises(ValueError, match="exactly once"):
        mutants.apply_mutant(tmp_path, mutation)
    assert path.read_text() == "value = 1\nvalue = 1\n"
    mutation = mutants.Mutant(
        (
            ("example.py", "value = 1\nvalue = 1", "value = 2"),
            ("example.py", "missing anchor", "value = 3"),
        ),
        (),
        "unit",
    )
    with pytest.raises(ValueError, match="exactly once"):
        mutants.apply_mutant(tmp_path, mutation)
    assert path.read_text() == "value = 1\nvalue = 1\n"


@pytest.mark.parametrize(
    "node,code,status",
    [
        ("", 0, "passed"),
        ('<failure message="assert 2 == 1">AssertionError</failure>', 1, "failed"),
        ("<failure>psycopg.OperationalError: database disappeared</failure>", 1, "invalid"),
        ("<failure>AssertionError</failure>", 2, "invalid"),
        ('<error message="fixture broke"/>', 1, "invalid"),
        ("<skipped/>", 0, "invalid"),
    ],
)
def test_junit_does_not_count_infrastructure_collection_or_skips_as_kills(
    tmp_path, node, code, status
):
    report = tmp_path / "junit.xml"
    report.write_text(
        '<testsuites><testsuite><testcase classname="tests.unit.test_a" name="test_a">'
        + node
        + "</testcase></testsuite></testsuites>"
    )
    result = mutants.junit_result(report, code)
    assert result["status"] == status
    assert result["test_ids"] == ["tests/unit/test_a.py::test_a"]


def test_missing_or_truncated_report_is_not_a_kill(tmp_path):
    report = tmp_path / "junit.xml"
    assert mutants.junit_result(report, 1)["status"] == "invalid"
    report.write_text("<testsuites>")
    assert mutants.junit_result(report, 1)["status"] == "invalid"


def test_cleanup_selects_only_its_exact_generated_run_label(monkeypatch):
    calls = []

    def docker(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout="owned-id\n")

    monkeypatch.setattr(mutants.subprocess, "run", docker)
    mutants.cleanup_containers("one-generated-run")
    assert calls == [
        ["docker", "ps", "-aq", "--filter", "label=ledgerops.mutant-run=one-generated-run"],
        ["docker", "rm", "--force", "owned-id"],
    ]


def test_existing_evidence_is_never_overwritten(tmp_path):
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("keep")
    result = subprocess.run(
        [
            sys.executable,
            str(mutants.ROOT / "scripts/mutants.py"),
            "all",
            "--output",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2 and "already exists" in result.stderr
    assert sentinel.read_text() == "keep"


def test_runner_deadline_is_invalid_and_still_cleans_owned_resources(tmp_path, monkeypatch):
    copy = tmp_path / "copy"
    source = copy / "src/ledgerops"
    source.mkdir(parents=True)
    (source / "__init__.py").write_text("")
    (copy / "test_slow.py").write_text("import time\ndef test_slow(): time.sleep(60)\n")
    cleaned = []
    monkeypatch.setattr(mutants, "cleanup_containers", cleaned.append)
    result = mutants.run_tests(copy, ("test_slow.py",), tmp_path / "evidence", "owned-run", 0.1)
    assert result["status"] == "invalid" and result["reason"] == "runner deadline exceeded"
    assert result["exit_code"] != 0
    assert cleaned == ["owned-run"]
    assert (tmp_path / "evidence/output.txt").exists()


@pytest.mark.parametrize(
    "control,changed,status,executions",
    [
        ("failed", "failed", "invalid", 1),
        ("passed", "invalid", "invalid", 2),
        ("passed", "failed", "killed", 2),
        ("passed", "passed", "survived", 2),
        ("passed", "different-tests", "invalid", 2),
    ],
)
def test_only_matching_tests_after_a_passing_control_can_kill(
    tmp_path, monkeypatch, control, changed, status, executions
):
    root = tmp_path / "source"
    (root / "src").mkdir(parents=True)
    (root / "src/rule.py").write_text("guard = True\n")
    monkeypatch.setitem(
        mutants.MUTANTS,
        "unit",
        mutants.Mutant(
            (("src/rule.py", "guard = True", "guard = False"),), ("a.py::test_guard",), "unit"
        ),
    )
    calls = []

    def copy_source(source, target, label):
        shutil.copytree(source / "src", target / "src")

    def run_tests(copy, tests, artifacts, label, timeout):
        trial = control if not calls else changed
        calls.append(trial)
        return {
            "status": "failed" if trial == "different-tests" else trial,
            "test_ids": ["other-test"] if trial == "different-tests" else ["a.py::test_guard"],
        }

    monkeypatch.setattr(mutants, "copy_source", copy_source)
    monkeypatch.setattr(mutants, "run_tests", run_tests)
    record = mutants.exercise(root, "unit", tmp_path / "evidence")
    assert record["status"] == status and len(calls) == executions
    assert record["source_unchanged"] and (root / "src/rule.py").read_text() == "guard = True\n"
