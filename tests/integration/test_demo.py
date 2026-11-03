"""Smoke the public make target, including its own disposable resources and failure mode."""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]


def _containers():
    result = subprocess.run(
        [
            "docker",
            "ps",
            "-a",
            "--filter",
            "label=ledgerops.scope=disposable-test",
            "--format",
            "{{.ID}}",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    return set(result.stdout.splitlines())


def _demo_environment(tmp_path):
    temporary_root = tmp_path / "demo-temp"
    temporary_root.mkdir()
    return {
        **os.environ,
        "UV_CACHE_DIR": str(ROOT / ".cache/uv"),
        "UV_OFFLINE": "1",
        # Reuse this already provisioned test interpreter, independently of the
        # caller's uv settings or whether the repository cache is populated.
        "UV_NO_SYNC": "1",
        "UV_PROJECT_ENVIRONMENT": sys.prefix,
        "PYTEST_ADDOPTS": "",
        "TMPDIR": str(temporary_root),
    }


def test_make_demo_runs_the_story_in_order_and_cleans_its_own_resources(tmp_path):
    # A normal regression run may already own its session-scoped test container.
    # The demo must preserve that container and remove only the one it creates.
    before = _containers()
    env = _demo_environment(tmp_path)
    result = subprocess.run(
        ["make", "demo"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    steps = re.findall(r"^\[(\d{2})\]\s+(.+)$", result.stdout, flags=re.MULTILINE)
    assert [number for number, _ in steps] == [f"{number:02}" for number in range(1, 13)]
    by_number = dict(steps)
    for number, markers in {
        "01": ("开户", "100"),
        "02": ("购买", "201", "80"),
        "03": ("422", "INSUFFICIENT_AVAILABLE"),
        "04": ("冲正", "201"),
        "05": ("422", "ALREADY_REVERSED"),
        "06": ("漂移", "120"),
        "07": ("离线", "A", "失败"),
        "08": ("冻结", "FROZEN"),
        "09": ("预览", "120", "100"),
        "10": ("重建", "epoch", "2"),
        "11": ("恢复", "NORMAL"),
        "12": ("离线", "健康"),
    }.items():
        assert all(marker in by_number[number] for marker in markers), by_number[number]
    assert _containers() == before
    assert not list(Path(env["TMPDIR"]).rglob("ledgerops-demo-*"))


def test_make_demo_fails_explicitly_when_docker_is_unavailable(tmp_path):
    env = _demo_environment(tmp_path)
    # An inherited context could otherwise override the deliberately absent host.
    for key in ("DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_TLS", "DOCKER_CERT_PATH"):
        env.pop(key, None)
    env["DOCKER_HOST"] = f"unix://{tmp_path / 'no-docker-daemon.sock'}"
    result = subprocess.run(
        ["make", "demo"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert "require Docker" in result.stdout + result.stderr
    assert "[01]" not in result.stdout
    assert not list(Path(env["TMPDIR"]).rglob("ledgerops-demo-*"))
