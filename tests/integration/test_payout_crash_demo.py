"""The public command demonstrates a real crash and owns all of its resources."""

import re
import subprocess

import pytest

from tests.integration.test_demo import ROOT, _containers, _demo_environment

pytestmark = pytest.mark.integration


def test_make_payout_crash_demo_has_real_recovery_and_removes_owned_resources(tmp_path):
    before = _containers()
    result = subprocess.run(
        ["make", "demo-payout-crash"],
        cwd=ROOT,
        env=_demo_environment(tmp_path),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    steps = re.findall(r"^\[(\d{2})\] (.+)$", result.stdout, flags=re.MULTILINE)
    assert [number for number, _ in steps] == [f"{number:02}" for number in range(1, 10)]
    messages = dict(steps)
    for step, markers in {
        "03": ("Provider SQLite committed", "reply", "paused"),
        "04": ("UNKNOWN", "HELD", "MAY_HAVE_SENT"),
        "05": ("SIGKILL", "after provider commit", "hold remains 62"),
        "06": ("restarted", "10-second claim lease"),
        "07": ("LOOKUP", "original payment key", "no second PUT"),
        "08": ("provider effects = 1", "ledger settlements = 1"),
        "09": ("PASS", "18 / 0 / 18", "verifier healthy"),
    }.items():
        assert all(marker in messages[step] for marker in markers), messages[step]
    assert _containers() == before
    assert result.stdout.splitlines()[-1] == (
        "Crash demo complete. Worker/provider stopped; owned PostgreSQL container removed."
    )


def test_make_payout_crash_demo_fails_when_docker_is_unavailable(tmp_path):
    environment = _demo_environment(tmp_path)
    for key in ("DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_TLS", "DOCKER_CERT_PATH"):
        environment.pop(key, None)
    environment["DOCKER_HOST"] = f"unix://{tmp_path / 'missing-docker.sock'}"
    result = subprocess.run(
        ["make", "demo-payout-crash"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert "require Docker" in result.stdout + result.stderr
    assert "[01]" not in result.stdout
