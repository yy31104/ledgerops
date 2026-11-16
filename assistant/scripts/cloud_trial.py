"""Collect read-only HTTPS smoke evidence; record an owner-supplied billing export."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit


def origin(value: str) -> str:
    parts = urlsplit(value)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
    ):
        raise ValueError("an HTTPS origin without credentials, path or query is required")
    return value.rstrip("/")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def get_status(url: str, token: str | None = None) -> int:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with opener.open(request, timeout=15) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


def smoke(api_origin: str, assistant_origin: str, token: str, *, request=get_status) -> dict:
    api, assistant = origin(api_origin), origin(assistant_origin)
    if not token or any(character in token for character in "\r\n"):
        raise ValueError("provide a caller token using LEDGEROPS_OPERATOR_TOKEN")
    checks = [
        ("api_identity", f"{api}/v1/me", token, 200),
        ("assistant_live", f"{assistant}/health/live", None, 200),
        ("assistant_ready", f"{assistant}/health/ready", None, 200),
        ("anonymous_session_denied", f"{assistant}/v1/session", None, 401),
        ("caller_session", f"{assistant}/v1/session", token, 200),
        ("remote_demo_absent", f"{assistant}/v1/demo", None, 404),
    ]
    records = []
    for name, url, credential, expected in checks:
        started = time.perf_counter()
        status = request(url, credential)
        records.append(
            {
                "name": name,
                "status": status,
                "expected_status": expected,
                "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                "passed": status == expected,
            }
        )
    return {
        "schema_version": 1,
        "kind": "read_only_cloud_smoke",
        "created_at": datetime.now(UTC).isoformat(),
        "checks": records,
        "passed": all(check["passed"] for check in records),
        "scope": "GET-only health/authentication checks; no financial or model calls",
    }


def cost_record(amount: str, currency: str, export: Path, resource_group: str) -> dict:
    try:
        parsed = Decimal(amount)
    except InvalidOperation:
        raise ValueError("amount must be a nonnegative decimal") from None
    if not parsed.is_finite() or parsed < 0:
        raise ValueError("amount must be a nonnegative decimal")
    if len(currency) != 3 or not currency.isascii() or not currency.isalpha():
        raise ValueError("currency must have three ASCII letters")
    payload = export.read_bytes()
    return {
        "schema_version": 1,
        "kind": "owner_reported_cloud_cost",
        "created_at": datetime.now(UTC).isoformat(),
        "resource_group": resource_group,
        "actual_cost": str(parsed),
        "currency": currency.upper(),
        "source_sha256": hashlib.sha256(payload).hexdigest(),
        "source_bytes": len(payload),
        "limitation": "Owner-supplied billing export and total; not independently verified.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("smoke")
    check.add_argument("--api-origin", required=True)
    check.add_argument("--assistant-origin", required=True)
    check.add_argument("--output", type=Path, required=True)
    cost = commands.add_parser("record-cost")
    cost.add_argument("--amount", required=True)
    cost.add_argument("--currency", required=True)
    cost.add_argument("--export", type=Path, required=True)
    cost.add_argument("--resource-group", required=True)
    cost.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "smoke":
        result = smoke(
            args.api_origin, args.assistant_origin, os.environ.get("LEDGEROPS_OPERATOR_TOKEN", "")
        )
    else:
        result = cost_record(args.amount, args.currency, args.export, args.resource_group)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as handle:
        json.dump(result, handle, indent=2)
        handle.write("\n")
    if result.get("passed") is False:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
