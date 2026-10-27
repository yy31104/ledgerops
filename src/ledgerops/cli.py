"""Local consistent-export and offline-check commands; no implicit database setup."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import psycopg

from ledgerops.export import ExportError, export_workspace, load_export
from ledgerops.verification import verify_snapshot


def _revision():
    """Record the checkout actually running this command, never guess a version."""
    module = Path(__file__).resolve()
    root = module.parents[2]
    try:
        checkout = Path(
            subprocess.check_output(
                ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
        ).resolve()
        if module != checkout / "src" / "ledgerops" / "cli.py":
            raise ExportError("Installed packages require an explicit Git revision")
        commit = subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL
        ).strip()
        dirty = bool(
            subprocess.check_output(
                ["git", "-C", str(root), "status", "--porcelain"],
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ExportError("An explicit Git revision is required outside a checkout") from exc
    return commit, dirty


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export", help="export using LEDGEROPS_EXPORT_DSN read-only login")
    export.add_argument("--workspace", required=True)
    export.add_argument("--output-root", type=Path, required=True)
    export.add_argument("--code-revision", help="full Git hash when running an installed package")
    export.add_argument("--dirty", action="store_true", help="mark an explicit revision as dirty")
    verify = commands.add_parser("verify", help="verify a completed bundle without database access")
    verify.add_argument("path", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "verify":
            report = verify_snapshot(load_export(args.path))
            print(json.dumps(report.as_dict(), ensure_ascii=False, sort_keys=True, allow_nan=False))
            return 0 if report.healthy else 1
        dsn = os.environ.get("LEDGEROPS_EXPORT_DSN")
        if not dsn:
            raise ExportError("LEDGEROPS_EXPORT_DSN is required")
        if args.dirty and not args.code_revision:
            raise ExportError("An explicit dirty flag requires an explicit code revision")
        revision, dirty = (args.code_revision, args.dirty) if args.code_revision else _revision()
        with psycopg.connect(dsn, autocommit=True) as conn:
            destination = export_workspace(
                conn, args.workspace, args.output_root, code_revision=revision, dirty=dirty
            )
        print(json.dumps({"export_path": str(destination)}, sort_keys=True))
        return 0
    except (ExportError, psycopg.Error, OSError) as exc:
        # Connection exceptions may contain environment/host details; never print
        # their raw contents or a credential-bearing DSN into an evidence log.
        print(
            json.dumps({"error": "EXPORT_OR_VERIFY_FAILED", "type": type(exc).__name__}),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
