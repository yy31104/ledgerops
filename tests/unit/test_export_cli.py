"""Code provenance must identify the executing checkout rather than an old wheel."""

from pathlib import Path

import pytest

from ledgerops import cli
from ledgerops.export import ExportError


def test_source_checkout_reports_current_commit_and_actual_dirty_flag():
    revision, dirty = cli._revision()
    assert len(revision) == 40 and all(c in "0123456789abcdef" for c in revision)
    assert type(dirty) is bool


def test_installed_package_inside_checkout_cannot_claim_checkout_head(monkeypatch):
    root = Path(__file__).resolve().parents[2]
    installed = root / ".venv/lib/python3.12/site-packages/ledgerops/cli.py"
    monkeypatch.setattr(cli, "__file__", str(installed))
    with pytest.raises(ExportError, match="explicit Git revision"):
        cli._revision()


def test_missing_dsn_produces_only_structured_error_without_traceback(monkeypatch, capsys):
    monkeypatch.delenv("LEDGEROPS_EXPORT_DSN", raising=False)
    assert (
        cli.main(
            [
                "export",
                "--workspace",
                "10000000-0000-4000-8000-000000000001",
                "--output-root",
                "/unused",
            ]
        )
        == 2
    )
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == '{"error": "EXPORT_OR_VERIFY_FAILED", "type": "ExportError"}\n'
