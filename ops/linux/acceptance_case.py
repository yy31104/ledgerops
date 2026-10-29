"""Private disposable-fixture code supplied over stdin; never installed in images."""

import json
import os
import resource
import sys
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
from psycopg.conninfo import make_conninfo

from ledgerops.db.payout_resolution import receive_observation
from ledgerops.domain import ProviderNamespaceId
from ledgerops.export import ExportError, capture_snapshot
from ledgerops.http.config import bounded_file
from ledgerops.provider_client import ProviderHTTPClient
from ledgerops.provider_protocol import ProviderTrust
from ledgerops.stream_export import export_stream
from ledgerops.stream_verification import stage_stream, verify_store

os.environ["TMPDIR"] = "/var/lib/ledgerops-scratch"
state = json.loads(bounded_file(Path("/run/bootstrap/state.json")))
if not Path("/run/bootstrap/acceptance.json").is_file():
    raise ValueError("EXPLICIT_DISPOSABLE_SYNTHETIC_SEED_REQUIRED")
payout_id = UUID(sys.argv[1])


def connection(role):
    return psycopg.connect(
        make_conninfo(
            host="postgres",
            dbname="ledgerops",
            user=role,
            password=state["passwords"][role],
        ),
        autocommit=True,
    )


def require(condition, code):
    if not condition:
        raise ValueError(code)


with connection("ledger_evidence_reader") as conn:
    rows = conn.execute(
        "SELECT operation_id,provider_key,provider_request,knowledge FROM ledger.payout "
        "WHERE workspace_id=%s ORDER BY operation_id",
        (state["workspace_id"],),
    ).fetchall()
    require(len(rows) == 2, "EXACT_TWO_SYNTHETIC_PAYOUTS_REQUIRED")
    target = next(row for row in rows if row[0] == payout_id)
    require(target[3] == "NOT_SENT", "QUIESCENT_UNSENT_SECOND_PAYOUT_REQUIRED")
    require(
        conn.execute("SELECT count(*) FROM ledger.observation_quarantine").fetchone() == (0,),
        "FRESH_QUARANTINE_FIXTURE_REQUIRED",
    )
trust = ProviderTrust(
    ProviderNamespaceId.parse(state["namespace"]),
    {"deploy-v1": bytes.fromhex(state["provider_hmac"])},
)
client = ProviderHTTPClient("http://127.0.0.1:8765", state["provider_token"])
closed = client.close(target[1], target[2])
require(closed.http_status == 200, "AUTHENTICATED_NEGATIVE_CLOSE_REQUIRED")
with connection("ledger_dispatcher") as conn:
    terminal = receive_observation(
        conn, UUID(state["workspace_id"]), payout_id, closed.body, trust=trust, channel="CLOSE"
    )
    require(
        terminal.status == "APPLIED" and terminal.state == "CLOSED_ABSENT", "NEGATIVE_HOLD_RELEASE"
    )
    for ordinal in range(129):
        raw = (f"untrusted-diagnostic-{ordinal}-{uuid4().hex}-".encode() + b"x" * 65536)[:65536]
        result = receive_observation(
            conn, UUID(state["workspace_id"]), rows[ordinal % 2][0], raw, trust=trust
        )
        require(result.status == "QUARANTINED", "BOUNDED_65_AND_64_DIAGNOSTICS_REQUIRED")
legacy_rejected = False
with connection("ledger_evidence_reader") as conn:
    try:
        capture_snapshot(conn, state["workspace_id"])
    except ExportError:
        legacy_rejected = True
    provenance = json.loads(Path("/opt/ledgerops-provenance.json").read_bytes())
    path = export_stream(
        conn,
        state["workspace_id"],
        "/var/lib/ledgerops-inputs/capacity",
        code_revision=provenance["revision"],
        dirty=provenance["dirty"],
    )
require(legacy_rejected, "LEGACY_LIMIT_MUST_BE_OBSERVED")
with stage_stream(path, scratch_root=os.environ["TMPDIR"]) as store:
    report = verify_store(store)
    scratch_bytes = (
        store.conn.execute("PRAGMA page_count").fetchone()[0]
        * store.conn.execute("PRAGMA page_size").fetchone()[0]
    )
require(scratch_bytes > 32 * 1024 * 1024, "ACTUAL_INDEX_EXCEEDS_API_TMPFS_BUDGET")
manifest = json.loads((path / "manifest.json").read_bytes())
size = sum(item.stat().st_size for item in path.iterdir() if item.is_file())
require(size > 16 * 1024 * 1024, "ACTUAL_OVERSIZED_FINANCIAL_EXPORT_REQUIRED")
require(manifest["row_counts"]["observation_quarantines"] == 129, "COMPLETE_DIAGNOSTIC_ROW_COUNT")
require(report["healthy"] and report["verification_complete"], "FULL_STREAM_VERIFICATION_REQUIRED")
require(not list(Path(os.environ["TMPDIR"]).glob("ledgerops-stream-*")), "OWNED_SCRATCH_CLEANUP")
with connection("ledger_evidence_reader") as conn:
    money = conn.execute(
        "SELECT booked,reserved,booked-reserved FROM ledger.account_balance "
        "WHERE workspace_id=%s AND account_id=%s",
        (state["workspace_id"], state["merchant_account"]),
    ).fetchone()
require(money == (18, 0, 18), "DIAGNOSTICS_CANNOT_CHANGE_MONEY")
print(
    json.dumps(
        {
            "passed": True,
            "source_format": 10,
            "source_bytes": size,
            "sqlite_scratch_bytes": scratch_bytes,
            "quarantine_rows": 129,
            "legacy_limit_rejected": legacy_rejected,
            "verification_complete": True,
            "independent_healthy": True,
            "export_path": str(path),
            "merchant_booked": "18",
            "merchant_reserved": "0",
            "rss_peak_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
            "cgroup_peak_bytes": int(Path("/sys/fs/cgroup/memory.peak").read_text()),
            "scratch_cleaned": True,
        },
        sort_keys=True,
    )
)
