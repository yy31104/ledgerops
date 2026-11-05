"""Independent, immutable settlement evidence from the provider's own SQLite file.

Settlement effective time is an administrator-assigned business time. It is NOT
the execution time (which execution-event v1 deliberately does not contain).
The provider key, owner and quoted amounts always come from retained executions.
"""

import argparse
import hashlib
import hmac
import json
import os
import re
import sqlite3
import stat
import sys
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .protocol import (
    Conflict,
    NotFound,
    ValidationError,
    canonical_json,
    canonical_uuid,
    validate_key,
    validate_request,
)
from .store import ProviderStore

DOMAIN = b"ledgerops.provider-statement:v1\n"
FEE_RULE = "quoted_fee_v1"
MAX_EFFECTS = 10_000
MAX_ROWS = 1_000
MAX_STATEMENT_BYTES = 16 * 1024 * 1024
_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS settlement_batch (
       batch_id TEXT PRIMARY KEY, request_bytes BLOB NOT NULL,
       effective_at TEXT NOT NULL, recorded_at TEXT NOT NULL,
       fee_rule_version TEXT NOT NULL CHECK(fee_rule_version='quoted_fee_v1'))""",
    """CREATE TABLE IF NOT EXISTS settlement_assignment (
       sequence INTEGER PRIMARY KEY, provider_key TEXT UNIQUE NOT NULL
       REFERENCES effects(provider_key), batch_id TEXT NOT NULL
       REFERENCES settlement_batch(batch_id), row_id TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS settlement_assignment_batch "
    "ON settlement_assignment(batch_id,row_id)",
    """CREATE TABLE IF NOT EXISTS settlement_statement (
       workspace_id TEXT NOT NULL, business_date TEXT NOT NULL, timezone TEXT NOT NULL,
       version INTEGER NOT NULL CHECK(version>0), envelope_bytes BLOB NOT NULL,
       PRIMARY KEY(workspace_id,business_date,timezone,version))""",
)


def _utc(value):
    if type(value) is not str or not re.fullmatch(
        r"[0-9]{4}(-[0-9]{2}){2}T[0-9]{2}(:[0-9]{2}){2}Z", value
    ):
        raise ValidationError("Canonical UTC seconds required")
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError("Invalid settlement instant") from exc


def _text(instant):
    return instant.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def day_bounds(business_date, timezone_name="Europe/Warsaw"):
    """Half-open local-calendar day; DST days are not necessarily 86,400 seconds."""
    if timezone_name != "Europe/Warsaw":
        raise ValidationError("Only the explicit Europe/Warsaw business calendar is supported")
    if type(business_date) is not str or not re.fullmatch(r"[0-9]{4}(-[0-9]{2}){2}", business_date):
        raise ValidationError("Canonical business date required")
    try:
        day = date.fromisoformat(business_date)
        zone = ZoneInfo(timezone_name)
        start = datetime.combine(day, time(), zone)
        end = datetime.combine(day + timedelta(days=1), time(), zone)
        return _text(start), _text(end)
    except (ValueError, OverflowError, ZoneInfoNotFoundError) as exc:
        raise ValidationError("Unsupported business date") from exc


def _batch_request(namespace, batch_id, effective_at, rows, fee_rule_version):
    canonical_uuid(batch_id)
    _utc(effective_at)
    if fee_rule_version != FEE_RULE:
        raise ValidationError("Unsupported settlement fee rule")
    if type(rows) is not list or not 1 <= len(rows) <= MAX_ROWS:
        raise ValidationError("Settlement requires a bounded nonempty row list")
    seen_rows, seen_keys, normalized = set(), set(), []
    for row in rows:
        if type(row) is not dict or row.keys() != {"row_id", "provider_keys"}:
            raise ValidationError("Settlement rows accept only identity and explicit members")
        row_id = canonical_uuid(row["row_id"])
        keys = row["provider_keys"]
        if row_id in seen_rows or type(keys) is not list or not keys:
            raise ValidationError("Duplicate row identity or empty members")
        if len(keys) > MAX_EFFECTS or len(seen_keys) + len(keys) > MAX_EFFECTS:
            raise ValidationError("Settlement member bound exceeded")
        seen_rows.add(row_id)
        for key in keys:
            validate_key(key, namespace)
            if key in seen_keys:
                raise ValidationError("Each provider effect may be allocated only once")
            seen_keys.add(key)
        normalized.append({"row_id": row_id, "provider_keys": sorted(keys)})
    return canonical_json(
        {
            "batch_id": batch_id,
            "effective_at": effective_at,
            "fee_rule_version": fee_rule_version,
            "rows": sorted(normalized, key=lambda row: row["row_id"]),
        }
    ).encode()


class SettlementStore:
    """Additive evidence storage; old provider metadata and execution bytes stay intact."""

    def __init__(self, provider):
        self.provider = provider
        with provider._connection() as conn, provider._write(conn):
            for statement in _SCHEMA:
                conn.execute(statement)
            for table in (
                "settlement_batch",
                "settlement_assignment",
                "settlement_statement",
            ):
                for action in ("UPDATE", "DELETE"):
                    conn.execute(
                        f"CREATE TRIGGER IF NOT EXISTS {table}_no_{action.lower()} "
                        f"BEFORE {action} ON {table} "
                        "BEGIN SELECT RAISE(ABORT, 'immutable settlement evidence'); END"
                    )

    def _effect(self, conn, key):
        row = conn.execute(
            "SELECT x.request_bytes,x.request_sha256,x.state,x.current_event,"
            "e.principal_minor,e.fee_minor,e.gross_minor,v.provider_key AS event_key,"
            "v.state AS event_state FROM executions x "
            "LEFT JOIN effects e USING(provider_key) "
            "LEFT JOIN events v ON v.sequence=x.current_event "
            "WHERE x.provider_key=?",
            (key,),
        ).fetchone()
        if row is None or (row["state"] != "SUCCEEDED" and row["gross_minor"] is None):
            raise NotFound("Settlement member has no successful provider effect")
        try:
            request = json.loads(row["request_bytes"])
            raw = validate_request(key, request, self.provider.namespace)
        except (ValueError, TypeError, UnicodeError) as exc:
            raise ValidationError("Corrupt retained provider request") from exc
        if (
            raw != row["request_bytes"]
            or hashlib.sha256(raw).hexdigest() != row["request_sha256"]
            or row["state"] != "SUCCEEDED"
            or row["event_state"] != "SUCCEEDED"
            or row["event_key"] != key
            or any(
                row[field] != request[field]
                for field in ("principal_minor", "fee_minor", "gross_minor")
            )
        ):
            raise ValidationError(
                "Provider effect contradicts its retained request or terminal event"
            )
        return {
            "provider_key": key,
            "execution_sequence": row["current_event"],
            "request_sha256": row["request_sha256"],
            "request": request,
        }

    def assign_batch(self, batch_id, effective_at, rows, *, fee_rule_version=FEE_RULE, hook=None):
        """Freeze explicit successful-effect allocation atomically; no caller-supplied money."""
        raw = _batch_request(
            self.provider.namespace, batch_id, effective_at, rows, fee_rule_version
        )
        request = json.loads(raw)
        with self.provider._connection() as conn, self.provider._write(conn, hook):
            previous = conn.execute(
                "SELECT * FROM settlement_batch WHERE batch_id=?", (batch_id,)
            ).fetchone()
            if previous is not None:
                if previous["request_bytes"] != raw:
                    raise Conflict("Settlement batch identity belongs to different metadata")
                return self._batch_receipt(conn, previous)
            batch_workspace = None
            for row in request["rows"]:
                if conn.execute(
                    "SELECT 1 FROM settlement_assignment WHERE row_id=? LIMIT 1",
                    (row["row_id"],),
                ).fetchone():
                    raise Conflict("Settlement row identity is already allocated")
                group = None
                for key in row["provider_keys"]:
                    effect = self._effect(conn, key)
                    body = effect["request"]
                    identity = (
                        body["workspace_id"],
                        body["merchant_id"],
                        body["currency"],
                    )
                    if group is not None and group != identity:
                        raise ValidationError(
                            "A settlement row must contain one workspace/merchant/currency group"
                        )
                    group = identity
                    if batch_workspace is not None and batch_workspace != body["workspace_id"]:
                        raise ValidationError("A settlement batch must contain one workspace")
                    batch_workspace = body["workspace_id"]
                    if conn.execute(
                        "SELECT 1 FROM settlement_assignment WHERE provider_key=?",
                        (key,),
                    ).fetchone():
                        raise Conflict("Provider effect already belongs to a settlement batch")
            recorded_at = _text(datetime.now(UTC))
            conn.execute(
                "INSERT INTO settlement_batch VALUES(?,?,?,?,?)",
                (batch_id, raw, effective_at, recorded_at, fee_rule_version),
            )
            sequence = conn.execute(
                "SELECT COALESCE(MAX(sequence),0) FROM settlement_assignment"
            ).fetchone()[0]
            for row in request["rows"]:
                for key in row["provider_keys"]:
                    sequence += 1
                    conn.execute(
                        "INSERT INTO settlement_assignment VALUES(?,?,?,?)",
                        (sequence, key, batch_id, row["row_id"]),
                    )
            previous = conn.execute(
                "SELECT * FROM settlement_batch WHERE batch_id=?", (batch_id,)
            ).fetchone()
            return self._batch_receipt(conn, previous)

    @staticmethod
    def _batch_receipt(conn, batch):
        assignments = conn.execute(
            "SELECT sequence,provider_key,row_id FROM settlement_assignment "
            "WHERE batch_id=? ORDER BY sequence",
            (batch["batch_id"],),
        ).fetchall()
        return {
            **json.loads(batch["request_bytes"]),
            "recorded_at": batch["recorded_at"],
            "assignments": [dict(row) for row in assignments],
        }

    def statement(
        self,
        workspace_id,
        business_date,
        version,
        *,
        timezone_name="Europe/Warsaw",
        hook=None,
    ):
        """Return a retained signed version, or create it from one SQLite transaction.

        The complete effects index covers this workspace at BOTH fixed watermarks;
        an assignment of None means unassigned at that source snapshot, not unpaid.
        """
        canonical_uuid(workspace_id)
        start, end = day_bounds(business_date, timezone_name)
        if type(version) is not int or not 1 <= version <= 2**31 - 1:
            raise ValidationError("Statement version must be a positive bounded integer")
        identity = (workspace_id, business_date, timezone_name, version)
        with self.provider._connection() as conn, self.provider._write(conn, hook):
            previous = conn.execute(
                "SELECT envelope_bytes FROM settlement_statement "
                "WHERE workspace_id=? AND business_date=? AND timezone=? AND version=?",
                identity,
            ).fetchone()
            if previous is not None:
                return json.loads(previous[0])
            latest = conn.execute(
                "SELECT COALESCE(MAX(version),0) FROM settlement_statement "
                "WHERE workspace_id=? AND business_date=? AND timezone=?",
                identity[:3],
            ).fetchone()[0]
            if version != latest + 1:
                raise Conflict("New statement version must be the next consecutive version")
            effect_watermark = conn.execute(
                "SELECT COALESCE(MAX(sequence),0) FROM events"
            ).fetchone()[0]
            assignment_watermark = conn.execute(
                "SELECT COALESCE(MAX(sequence),0) FROM settlement_assignment"
            ).fetchone()[0]
            prefix = f"v1.{self.provider.namespace}.{workspace_id}.%"
            keys = conn.execute(
                "SELECT provider_key FROM executions WHERE provider_key LIKE ? "
                "AND (state='SUCCEEDED' OR EXISTS "
                "(SELECT 1 FROM effects WHERE effects.provider_key=executions.provider_key)) "
                "ORDER BY provider_key LIMIT ?",
                (prefix, MAX_EFFECTS + 1),
            ).fetchall()
            if len(keys) > MAX_EFFECTS:
                raise ValidationError("Complete workspace effect inventory exceeds statement bound")
            effects = []
            for key in keys:
                effect = self._effect(conn, key[0])
                row = conn.execute(
                    "SELECT a.sequence,a.batch_id,a.row_id,b.effective_at,"
                    "b.recorded_at,b.fee_rule_version "
                    "FROM settlement_assignment a JOIN settlement_batch b USING(batch_id) "
                    "WHERE a.provider_key=?",
                    (key[0],),
                ).fetchone()
                effect["assignment"] = dict(row) if row else None
                effects.append(effect)
            rows = self._statement_rows(effects, start, end)
            payload = {
                "kind": "settlement_statement",
                "issuer": "ledgerops-provider-sim",
                "schema_version": 1,
                "scope": "COMPLETE_WORKSPACE_EFFECTS_AT_WATERMARK",
                "time_basis": "SETTLEMENT_EFFECTIVE_TIME",
                "namespace": self.provider.namespace,
                "workspace_id": workspace_id,
                "business_date": business_date,
                "timezone": timezone_name,
                "start_inclusive": start,
                "end_exclusive": end,
                "version": version,
                "generated_at": _text(datetime.now(UTC)),
                "effect_watermark": effect_watermark,
                "assignment_watermark": assignment_watermark,
                "fee_rule_version": FEE_RULE,
                "rows": rows,
                "effects": effects,
            }
            raw = canonical_json(payload)
            if len(raw.encode()) > MAX_STATEMENT_BYTES:
                raise ValidationError("Complete statement exceeds byte bound")
            signature = hmac.new(
                self.provider._secret,
                DOMAIN + self.provider.key_id.encode() + b"\n" + raw.encode(),
                hashlib.sha256,
            ).hexdigest()
            envelope = {
                "key_id": self.provider.key_id,
                "payload": raw,
                "signature": signature,
            }
            envelope_bytes = canonical_json(envelope).encode()
            if len(envelope_bytes) > MAX_STATEMENT_BYTES:
                raise ValidationError("Signed statement exceeds byte bound")
            conn.execute(
                "INSERT INTO settlement_statement VALUES(?,?,?,?,?)",
                (*identity, envelope_bytes),
            )
            return envelope

    def _statement_rows(self, effects, start, end):
        rows = {}
        for effect in effects:
            assignment = effect["assignment"]
            if assignment is None or not start <= assignment["effective_at"] < end:
                continue
            request = effect["request"]
            row_id = assignment["row_id"]
            group = {
                "namespace": self.provider.namespace,
                "batch_id": assignment["batch_id"],
                "currency": request["currency"],
                "merchant_id": request["merchant_id"],
                "fee_rule_version": assignment["fee_rule_version"],
            }
            if row_id not in rows:
                rows[row_id] = {
                    "row_id": row_id,
                    "group": group,
                    "provider_keys": [],
                    "principal_minor": 0,
                    "fee_minor": 0,
                    "gross_minor": 0,
                }
            row = rows[row_id]
            if row["group"] != group:
                raise ValidationError("Corrupt settlement row group")
            row["provider_keys"].append(effect["provider_key"])
            for field in ("principal_minor", "fee_minor", "gross_minor"):
                row[field] += int(request[field])
        if len(rows) > MAX_ROWS:
            raise ValidationError("Statement row bound exceeded")
        return [
            {
                **row,
                **{
                    field: str(row[field])
                    for field in ("principal_minor", "fee_minor", "gross_minor")
                },
            }
            for _, row in sorted(rows.items())
        ]


def _unique_fields(pairs):
    value = {}
    for field, item in pairs:
        if field in value:
            raise ValidationError("Duplicate batch JSON field")
        value[field] = item
    return value


def _batch_bytes(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_STATEMENT_BYTES:
            raise ValidationError("Batch input must be a bounded regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            raw = source.read(MAX_STATEMENT_BYTES + 1)
        if len(raw) > MAX_STATEMENT_BYTES:
            raise ValidationError("Batch input exceeds byte bound")
        return raw
    finally:
        os.close(descriptor)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--key-id", required=True)
    command = parser.add_subparsers(dest="command", required=True)
    assign = command.add_parser(
        "assign", help="Assign explicit existing effects to settlement rows"
    )
    assign.add_argument(
        "--batch-file",
        required=True,
        help="JSON batch_id/effective_at/rows/fee_rule_version",
    )
    statement = command.add_parser("statement", help="Print a retained signed statement to stdout")
    statement.add_argument("--workspace", required=True)
    statement.add_argument("--date", required=True)
    statement.add_argument("--version", required=True, type=int)
    args = parser.parse_args(argv)
    try:
        database = Path(args.database)
        if database.is_symlink() or not database.is_file():
            raise ValidationError("Settlement requires an existing provider database")
        provider = ProviderStore(
            args.database,
            args.namespace,
            args.key_id,
            bytes.fromhex(os.environ.get("LEDGEROPS_PROVIDER_HMAC_KEY", "")),
        )
        settlements = SettlementStore(provider)
        if args.command == "assign":
            raw = _batch_bytes(args.batch_file)
            value = json.loads(raw, object_pairs_hook=_unique_fields)
            if type(value) is not dict or value.keys() != {
                "batch_id",
                "effective_at",
                "rows",
                "fee_rule_version",
            }:
                raise ValidationError("Invalid batch input shape")
            result = settlements.assign_batch(**value)
        else:
            result = settlements.statement(args.workspace, args.date, args.version)
        print(canonical_json(result))
        return 0
    except (ValueError, OSError, sqlite3.DatabaseError, RecursionError):
        print(
            "settlement operation failed: check local source and explicit metadata",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
