"""Offline settlement comparison; no database credentials or financial write path."""

import argparse
import json
import os
import stat
import sys
from pathlib import Path

from ledgerops.cli import _code_version
from ledgerops.reconciliation import StatementTrust, reconcile_bundle
from ledgerops.reconciliation.statement import MAX_BYTES, CorruptInput
from ledgerops.worker import _read_keys


def _statement_bytes(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_BYTES:
            raise CorruptInput("INVALID_STATEMENT_FILE")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise CorruptInput("STATEMENT_BYTE_LIMIT")
        return raw
    finally:
        os.close(descriptor)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    match = commands.add_parser("match", help="Compare fixed local files and print a JSON report")
    match.add_argument("--ledger-export", type=Path, required=True)
    match.add_argument("--statement", type=Path, required=True)
    match.add_argument("--namespace", required=True)
    match.add_argument("--keys-file", type=Path, required=True)
    match.add_argument(
        "--code-revision", help="Full matcher Git revision; required outside checkout"
    )
    match.add_argument("--dirty", action="store_true")
    args = parser.parse_args(argv)
    try:
        revision, dirty = _code_version(args)
        trust = StatementTrust(args.namespace, _read_keys(args.keys_file))
        report = reconcile_bundle(
            args.ledger_export,
            _statement_bytes(args.statement),
            trust=trust,
            code_revision=revision,
            dirty=dirty,
        )
        print(json.dumps(report, sort_keys=True, separators=(",", ":")), flush=True)
        return 2 if not report["usable"] else 0 if report["reconciled"] else 1
    except (OSError, ValueError):
        # Key material, DSNs, parser payloads and raw exception text stay out of logs.
        print(
            json.dumps({"error": "RECONCILIATION_INPUT_OR_CONFIGURATION_INVALID"}), file=sys.stderr
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
