"""Linux failures keep useful private bytes without publishing command secrets."""

import importlib.util
import json
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = next(
    parent
    for parent in Path(__file__).resolve().parents
    if (parent / "ops/linux/stack.py").is_file()
)


@pytest.fixture
def modules(monkeypatch):
    def load(name, path):
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    stack = load("diagnostic_stack", ROOT / "ops/linux/stack.py")
    monkeypatch.setitem(sys.modules, "stack", stack)
    acceptance = load("diagnostic_acceptance", ROOT / "ops/linux/acceptance.py")
    return SimpleNamespace(stack=stack, acceptance=acceptance)


@pytest.mark.parametrize("capture", [False, True])
def test_actual_failed_subprocess_output_is_private_bounded_and_not_echoed(
    modules, capsys, capture
):
    stack = modules.stack
    diagnostics = stack.FailureDiagnostics()
    secret = "private-token-do-not-publish"
    code = (
        "import sys; print('" + secret + "'+'x'*20000); "
        "sys.stderr.write('postgresql://owner:secret@db/test\\n'+'y'*20000); sys.exit(7)"
    )
    try:
        with diagnostics.scope(), pytest.raises(subprocess.CalledProcessError) as caught:
            stack.diagnostic_phase("startup.copy_proxy_ca")
            stack.run([sys.executable, "-c", code], capture=capture)
        assert caught.value.returncode == 7
        published = json.dumps(diagnostics.report())
        assert (
            secret not in published and "postgresql://" not in published and code not in published
        )
        assert not capsys.readouterr().out and not capsys.readouterr().err
        record = diagnostics.report()["failures"][0]
        assert record["phase"] == "startup.copy_proxy_ca"
        assert record["returncode"] == 7
        assert record["stdout"]["truncated"] and record["stderr"]["truncated"]
        assert record["stdout"]["retained_bytes"] == 8192
        assert record["command"] == {"program": "other", "action": "other"}
        directory = diagnostics.directory
        assert directory is not None and not directory.resolve().is_relative_to(ROOT)
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        path = directory / record["private_file"]
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        raw = json.loads(path.read_bytes())
        assert raw["stdout"].startswith(secret) and "postgresql://" in raw["stderr"]
        assert len(raw["stdout"].encode()) == len(raw["stderr"].encode()) == 8192
    finally:
        if diagnostics.directory is not None:
            shutil.rmtree(diagnostics.directory)


def test_successful_noncaptured_process_still_prints_and_leaves_no_private_directory(
    modules, capsys
):
    diagnostics = modules.stack.FailureDiagnostics()
    with diagnostics.scope():
        result = modules.stack.run(
            [
                sys.executable,
                "-c",
                "import sys; print('normal output'); sys.stderr.write('normal warning\\n')",
            ]
        )
    output = capsys.readouterr()
    assert output.out == "normal output\n" and output.err == "normal warning\n"
    assert result.returncode == 0 and result.stdout is None and result.stderr is None
    assert diagnostics.directory is None and diagnostics.report()["failures"] == []


def test_command_identity_is_allowlisted_and_storage_failure_does_not_mask_original(
    modules, monkeypatch
):
    diagnostics = modules.stack.FailureDiagnostics()
    original = subprocess.CalledProcessError(23, ["docker"], stderr="sensitive-error")

    def failed(*args, **kwargs):
        raise original

    monkeypatch.setattr(modules.stack.subprocess, "run", failed)
    monkeypatch.setattr(modules.stack.tempfile, "mkdtemp", lambda **kwargs: str(ROOT))
    # Avoid changing the repository itself when simulating a forbidden destination.
    monkeypatch.setattr(Path, "rmdir", lambda self: None)
    with diagnostics.scope(), pytest.raises(subprocess.CalledProcessError) as caught:
        modules.stack.run(
            [
                "docker",
                "compose",
                "--project-name",
                "secret-project-name",
                "exec",
                "api",
                "python",
                "-c",
                "print('secret-body')",
            ],
            capture=True,
        )
    assert caught.value is original
    record = diagnostics.report()["failures"][0]
    assert record["command"] == {"program": "docker", "action": "compose.exec", "service": "api"}
    assert record["private_output_unavailable"]
    assert "secret" not in json.dumps(diagnostics.report())


def test_timeout_and_failure_count_are_bounded(modules):
    diagnostics = modules.stack.FailureDiagnostics()
    try:
        for _ in range(18):
            diagnostics.record(
                ["docker", "exec", "api"],
                subprocess.TimeoutExpired(
                    "secret-command", 3, output=b"secret-stdout", stderr=b"secret-stderr"
                ),
            )
        report = diagnostics.report()
        assert len(report["failures"]) == 16 and report["omitted_failures"] == 2
        assert all(row["category"] == "TimeoutExpired" for row in report["failures"])
        assert "secret" not in json.dumps(report)
        assert len(list(diagnostics.directory.iterdir())) == 16
    finally:
        if diagnostics.directory is not None:
            shutil.rmtree(diagnostics.directory)


def test_startup_observes_ca_and_live_endpoints_before_returning(modules, tmp_path, monkeypatch):
    acceptance = modules.acceptance
    copies, probes, requests, sleeps = [], [], [], []
    ca_ready = iter([False, False, True])

    class Stack:
        state = {
            "environment": {"LEDGEROPS_HTTPS_PORT": "18443", "LEDGEROPS_OPERATOR_PORT": "19443"}
        }

        def copy_volume_file(self, volume, source, destination):
            copies.append(volume)
            if volume == "bootstrap-secrets":
                Path(destination).write_text('{"tokens": {}}')
            else:
                assert len(probes) == 3
                Path(destination).write_text("public-ca")

        def compose(self, *args, **kwargs):
            probes.append(args)
            assert kwargs["timeout"] == 5
            if not next(ca_ready):
                raise subprocess.CalledProcessError(1, ["docker"], stderr="not ready")
            return SimpleNamespace(returncode=0)

    def request(self, path, **kwargs):
        requests.append((path, kwargs["operator"]))
        return 200, {"status": "live"}

    monkeypatch.setattr(acceptance.time, "sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setattr(acceptance.ssl, "create_default_context", lambda **kwargs: object())
    monkeypatch.setattr(acceptance.Acceptance, "request", request)
    acceptance.Acceptance(Stack(), tmp_path)
    assert copies == ["bootstrap-secrets", "caddy-data"]
    assert requests == [("/health/live", False), ("/health/live", True)]
    assert sleeps == [0.05, 0.05]  # only after an observed not-ready result


def test_ca_readiness_timeout_has_a_fixed_phase_and_named_failure(modules, tmp_path, monkeypatch):
    ac, stack_module = modules.acceptance, modules.stack
    diagnostics = stack_module.FailureDiagnostics()
    ticks = iter([0, 21])

    class Stack:
        def copy_volume_file(self, *args):
            pass

        def compose(self, *args, **kwargs):
            raise subprocess.CalledProcessError(1, ["docker"], stderr="secret-from-proxy")

    monkeypatch.setattr(ac.time, "monotonic", lambda: next(ticks))
    with diagnostics.scope(), pytest.raises(ac.AcceptanceFailure, match="PROXY_CA_NOT_READY"):
        ac.Acceptance(Stack(), tmp_path)
    assert diagnostics.phase == "startup.wait_proxy_ca"


def test_constructor_failure_is_saved_and_disposal_failure_never_masks_it(
    modules, tmp_path, monkeypatch
):
    ac, stack_module = modules.acceptance, modules.stack
    output = tmp_path / "verification.json"
    original = subprocess.CalledProcessError(
        19, ["docker"], output="sensitive-stdout", stderr="secret"
    )
    calls = []

    class Stack:
        def __init__(self, *args):
            pass

        def dispose(self):
            calls.append("dispose")
            raise PermissionError("cleanup secret")

    def constructor(*args):
        stack_module.diagnostic_phase("startup.copy_bootstrap_credentials")
        raise original

    monkeypatch.setattr(ac, "Stack", Stack)
    monkeypatch.setattr(ac, "Acceptance", constructor)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "acceptance.py",
            "--project",
            "ledgerops-test",
            "--state",
            str(tmp_path / "state.json"),
            "--output",
            str(output),
            "--dispose-owned-test-data",
        ],
    )
    with pytest.raises(subprocess.CalledProcessError) as caught:
        ac.main()
    assert caught.value is original and calls == ["dispose"]
    result = json.loads(output.read_bytes())
    assert not result["passed"] and result["checks"] == []
    assert result["failure"]["phase"] == "startup.copy_bootstrap_credentials"
    assert result["failure"]["category"] == "CalledProcessError"
    assert result["cleanup_failures"][0]["phase"] == "cleanup.owned_stack"
    assert "secret" not in output.read_text() and "sensitive" not in output.read_text()
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_generic_assertion_message_is_never_a_public_diagnostic_code(modules):
    summary = modules.acceptance.failure_summary(AssertionError("private-token"), "startup")
    assert summary == {
        "phase": "startup",
        "category": "AssertionError",
        "code": "LOCAL_DIAGNOSTICS_REQUIRED",
    }
