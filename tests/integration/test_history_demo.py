"""The public history command reports both cases and leaves no containers behind."""

import json
import subprocess

import pytest

from tests.integration.test_demo import ROOT, _containers, _demo_environment

pytestmark = pytest.mark.integration


def test_history_demo_checks_locked_control_and_unlocked_variant(tmp_path):
    before = _containers()
    result = subprocess.run(
        ["make", "demo-lost-update"],
        cwd=ROOT,
        env=_demo_environment(tmp_path),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    reports = [
        json.loads(line.removeprefix("Checker report: "))
        for line in result.stdout.splitlines()
        if line.startswith("Checker report: ")
    ]
    assert len(reports) == 2
    assert reports[0]["valid"] and not reports[0]["anomalies"]
    assert not reports[1]["valid"]
    assert {"lost-update", "G-single", "non-adjacent-rmw"} <= set(reports[1]["anomalies"])
    assert all(report["stats"]["committed"] == 2 for report in reports)
    assert result.stdout.count("Customer=40; merchant=60; independent fold=MATCH.") == 2
    assert _containers() == before


def test_history_demo_fails_clearly_without_docker(tmp_path):
    env = _demo_environment(tmp_path)
    for key in ("DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_TLS", "DOCKER_CERT_PATH"):
        env.pop(key, None)
    env["DOCKER_HOST"] = f"unix://{tmp_path / 'missing-docker.sock'}"
    result = subprocess.run(
        ["make", "demo-lost-update"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert "require Docker" in result.stdout + result.stderr
    assert "Checker report:" not in result.stdout
