"""Local consistent-export and offline-check commands; no implicit database setup."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import psycopg

from ledgerops.db.maintenance import (
    MaintenanceError,
    apply_rebuild,
    freeze_workspace,
    preview_rebuild,
    resume_workspace,
)
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


def _code_version(args):
    if args.dirty and not args.code_revision:
        raise ExportError("An explicit dirty flag requires an explicit code revision")
    return (args.code_revision, args.dirty) if args.code_revision else _revision()


def _maintenance(args):
    dsn = os.environ.get("LEDGEROPS_MAINTENANCE_DSN")
    if not dsn:
        raise ExportError("LEDGEROPS_MAINTENANCE_DSN is required")
    with psycopg.connect(dsn, autocommit=True) as conn:
        if args.command == "freeze":
            return freeze_workspace(conn, args.workspace, args.freeze_id, args.actor, args.reason)
        if args.command == "preview-rebuild":
            revision, dirty = _code_version(args)
            return preview_rebuild(
                conn,
                args.workspace,
                args.freeze_id,
                args.output_root,
                code_revision=revision,
                dirty=dirty,
            )
        if args.command == "apply-rebuild":
            return apply_rebuild(
                conn,
                args.workspace,
                args.freeze_id,
                args.repair_id,
                args.actor,
                args.reason,
                args.preview_export_id,
            )
        return resume_workspace(conn, args.workspace, args.freeze_id, args.actor, args.reason)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export", help="export using LEDGEROPS_EXPORT_DSN read-only login")
    export.add_argument("--workspace", required=True)
    export.add_argument("--output-root", type=Path, required=True)
    export.add_argument("--code-revision", help="full Git hash when running an installed package")
    export.add_argument("--dirty", action="store_true", help="mark an explicit revision as dirty")
    export.add_argument("--format", choices=("legacy", "stream10"), default="legacy")
    verify = commands.add_parser("verify", help="verify a completed bundle without database access")
    verify.add_argument("path", type=Path)
    verify.add_argument("--format", choices=("legacy", "stream10"), default="legacy")
    verify.add_argument("--scratch-root", type=Path, help="private disk scratch for stream10")
    for name in ("freeze", "preview-rebuild", "apply-rebuild", "resume"):
        command = commands.add_parser(
            name, help="explicit local maintenance using its dedicated login"
        )
        command.add_argument("--workspace", required=True)
        command.add_argument("--freeze-id", required=True)
        if name != "preview-rebuild":
            command.add_argument("--actor", required=True)
            command.add_argument("--reason", required=True)
        if name == "preview-rebuild":
            command.add_argument("--output-root", type=Path, required=True)
            command.add_argument("--code-revision")
            command.add_argument("--dirty", action="store_true")
        if name == "apply-rebuild":
            command.add_argument("--repair-id", required=True)
            command.add_argument("--preview-export-id", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "verify":
            if args.format == "stream10":
                from ledgerops.stream_verification import verify_stream

                report = verify_stream(args.path, scratch_root=args.scratch_root)
                print(json.dumps(report, ensure_ascii=False, sort_keys=True, allow_nan=False))
                return 0 if report["healthy"] else 1
            if args.scratch_root is not None:
                raise ExportError("Scratch root is only supported for stream10")
            report = verify_snapshot(load_export(args.path))
            print(json.dumps(report.as_dict(), ensure_ascii=False, sort_keys=True, allow_nan=False))
            return 0 if report.healthy else 1
        if args.command != "export":
            print(
                json.dumps(_maintenance(args), ensure_ascii=False, sort_keys=True, allow_nan=False)
            )
            return 0
        dsn = os.environ.get("LEDGEROPS_EXPORT_DSN")
        if not dsn:
            raise ExportError("LEDGEROPS_EXPORT_DSN is required")
        revision, dirty = _code_version(args)
        producer = export_workspace
        if args.format == "stream10":
            from ledgerops.stream_export import export_stream

            producer = export_stream
        with psycopg.connect(dsn, autocommit=True) as conn:
            destination = producer(
                conn, args.workspace, args.output_root, code_revision=revision, dirty=dirty
            )
        print(json.dumps({"export_path": str(destination)}, sort_keys=True))
        return 0
    except MaintenanceError as exc:
        print(json.dumps({"error": exc.code, "report": exc.report}), file=sys.stderr)
        return 2
    except (ExportError, ValueError, MemoryError, RecursionError, psycopg.Error, OSError) as exc:
        # Connection exceptions may contain environment/host details; never print
        # their raw contents or a credential-bearing DSN into an evidence log.
        print(
            json.dumps({"error": "EXPORT_OR_VERIFY_FAILED", "type": type(exc).__name__}),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
