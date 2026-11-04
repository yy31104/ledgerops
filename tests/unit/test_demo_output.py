"""The narration-only pytest plugin must retain diagnostics on failure."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("fails", [False, True])
def test_demo_reporter_preserves_narration_and_failure_diagnostics(tmp_path, fails):
    story = tmp_path / "test_story.py"
    story.write_text(
        "def test_story():\n"
        "    print('[01] Narration stays visible')\n"
        + ("    assert False, 'demo assertion is visible'\n" if fails else "")
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-qq",
            "-s",
            "--no-header",
            "--tb=short",
            "-p",
            "tests.demo_output",
            "--confcutdir",
            str(tmp_path),
            str(story),
        ],
        cwd=ROOT,
        env={**os.environ, "PYTEST_ADDOPTS": "", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert "[01] Narration stays visible" in result.stdout
    if fails:
        assert result.returncode == 1
        assert "demo assertion is visible" in result.stdout + result.stderr
        assert "FAILED" in result.stdout
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout.strip() == "[01] Narration stays visible"
