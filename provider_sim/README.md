# Independent synthetic payment provider

This is a loopback-only test authority with its own durable SQLite file. It does
not connect to LedgerOps, banks, or real funds. The stdlib HTTP adapter is not a
production HTTP server. Keep the file and namespace together across restarts.

## Run

Use Python3.12. Set three explicit environment values: `LEDGEROPS_PROVIDER_TOKEN`
(32–512 non-whitespace ASCII characters), `LEDGEROPS_PROVIDER_HMAC_KEY` (hex,
at least32 random bytes), and a matching `--key-id`. Generate secrets independently;
do not reuse real credentials or commit environment files.

```sh
python -m provider_sim --database /absolute/local/path/provider.sqlite3 \
  --namespace 00000000-0000-4000-8000-000000000001 --key-id local-v1 --port 0
```

The first JSON line reports the actual loopback URL, PID, namespace and SQLite
diagnostics, without credentials. SIGTERM or Ctrl-C closes owned HTTP handlers;
it does not delete history. A new namespace requires a different database file.
Changing a namespace/signing identity on an existing file fails instead of hiding
old outcomes. This release does not implement key rotation.

Every request requires `Authorization: Bearer <token>` and the exact URL Host.
PUT/POST bodies require `Content-Type: application/json` and Content-Length.
Maximum body32KiB; duplicate JSON keys, unknown request fields and chunked encoding
are rejected. At most16 handlers; socket timeout5s; SQLite busy timeout2s.
These are resource bounds, never proof that a payment failed.

## Protocol v1

Key: `v1.<namespace UUID>.<workspace UUID>.<operation UUID>`, canonical UUID text.
The request is an exact object with:

```json
{
  "workspace_id": "00000000-0000-4000-8000-000000000002",
  "operation_id": "00000000-0000-4000-8000-000000000003",
  "merchant_id": "00000000-0000-4000-8000-000000000004",
  "beneficiary_id": "00000000-0000-4000-8000-000000000005",
  "principal_minor": "60",
  "fee_minor": "2",
  "gross_minor": "62",
  "currency": "PLN",
  "contract_version": 1,
  "dispatch_deadline": "2026-10-29T12:00:00Z"
}
```

`merchant_id` identifies the ledger merchant account, not an authenticated actor.
The future ledger adapter must check its owner and beneficiary binding before
sending. The simulator validates the wire contract; it does not know ledger owners.
The deadline is immutable data for the ledger dispatch policy, not automatic
provider cancellation. An in-flight PUT can succeed after it unless closure wins.

| Method/path | Result |
|---|---|
| PUT `/v1/executions/<key>` | Defaults to synchronous synthetic success. Same key/request returns original event; changed request409. |
| GET `/v1/executions/<key>` | Current immutable signed event, or signed provisional NOT_FOUND. |
| POST `/v1/executions/<key>/close-if-absent` | Full request body. Serializes with PUT; creates permanent CLOSED_ABSENT if missing, otherwise returns existing event. |
| GET `/v1/history?after=0&limit=100` | First page chooses fixedwatermark; subsequent requests include that watermark. Signed proof binds cursor and event payload hashes. |

Provider HTTP200 means the protocol call returned; read the signed state. It is
not the ledger's future202 admission receipt. Event envelope has `key_id`, exact
UTF-8 `payload` string and lowercase hex HMAC-SHA256 `signature`. Signed bytes are
`ledgerops.provider-event:v1\n` + key ID + newline + payload bytes. Receiver must
independently validate all business bindings after verifying the signature.
Terminal event ID/sequence/payload/signature remain identical across deliveries.
The signing secret is not in the database; a digest pins its configured identity.

## Verification and fault controls

`make test-provider` runs the independent SQLite and real subprocess tests, without
using PostgreSQL as the provider. Full `make test` also discovers these cases.
The provider is included in the source archive for reproducible tests, not in the
installed ledger wheel.

`--test-control` additionally requires a distinct `LEDGEROPS_PROVIDER_CONTROL_TOKEN`.
Only that token can use `/__test/` endpoints. Tests exercise configured before/after
commit gates, lookup lag, pending results and deliberately contradictory signed
observations. Gates have a60s fail-safe; ordinary commands cannot select faults.
Read the subprocess tests for the exact control protocol rather than treating it
as an operator or production API.
