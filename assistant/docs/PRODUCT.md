# Run the operator assistant

## Local demo

From the repository root, with Docker running:

```sh
python3 assistant/scripts/compose.py up
python3 assistant/scripts/compose.py check
```

Open <http://127.0.0.1:8088> and choose **Try synthetic demo**. The stack contains
PostgreSQL/pgvector, an initialization job, a GET-only synthetic LedgerOps API and
the FastAPI/React panel. Only the loopback panel port is published. No real bank or
paid model API is involved. Initialization downloads pinned bge-small weights and
indexes the repository documents.

The script creates ignored `assistant/.env` with mode 0600. It holds random local
DB passwords and a synthetic token. The token endpoint exists only in explicit
demo mode with a loopback upstream. The runtime image uses a non-root user, a
read-only filesystem, cache volumes, locked dependencies and built panel assets.

```sh
python3 assistant/scripts/compose.py down
```

Database and model-cache volumes remain for restart. `up --hashing` uses stand-in
embeddings for plumbing checks, not retrieval-quality measurements.

## Connect an existing API

Use a separate assistant database: run `python -m ledgerops_assistant.runtime init`
with its owner login, then `python -m ledgerops_assistant.runtime serve` with its
runtime login. Initialization refuses any database containing the financial
`ledger` schema. The runtime role reads the index and writes draft/checkpoint
records; it cannot administer roles or databases.

Never give the assistant a financial/operator database DSN, provider signing key
or approval tool. Operators connect with their own existing token. The transport
uses fixed GET routes and rechecks current identity and scope for every read.
The panel keeps the token only in memory and clears it, results and drafts on
disconnect. Credentials do not enter model prompts or graph state.

| Variable | Meaning |
|---|---|
| `LEDGEROPS_ASSISTANT_DATABASE_URL` | Dedicated assistant PostgreSQL DSN |
| `LEDGEROPS_ASSISTANT_API_ORIGIN` | Fixed upstream origin; HTTPS except loopback HTTP |
| `LEDGEROPS_ASSISTANT_CONSOLE_URL` | Existing operator console; default localhost:8080 |
| `LEDGEROPS_ASSISTANT_CORPUS_ROOT` | Repository documents for an installed package |
| `LEDGEROPS_ASSISTANT_WEB_DIST` | Built panel assets for an installed package |
| `LEDGEROPS_ASSISTANT_PROVIDERS` | Enabled providers; default `fake` |
| `LEDGEROPS_ASSISTANT_EMBEDDER` | `bge`, or `hashing` for plumbing checks |
| `LEDGEROPS_ASSISTANT_SCOPE_VERIFIER` | `lexical` (default) or host-local `subscription` passage verification |
| `LEDGEROPS_ASSISTANT_DEADLINE_SECONDS` | Request deadline in seconds, from greater than zero through 120; default 60 |

Leave `LEDGEROPS_ASSISTANT_DEMO_TOKEN` unset with a real upstream. Empty recent
lists and 404 responses do not prove that no payment occurred.

## Models, limits and cost

The panel lists server-enabled `fake`, `openai_api` and `ollama` providers.
Subscription answer generation is available from the CLI. The host-local HTTP
service can use the [subscription passage verifier](PROVIDERS.md#verify-passages-in-the-local-http-service)
before calling the selected answer provider. The bundled image uses fake
generation. API mode needs its optional dependency, model/key configuration and
positive input/output token prices; Ollama needs a configured model/server.

Each investigation allows at most six model steps, 24,000 input bytes, 512 output
tokens per API call and a 60-second graph deadline. The process admits four active
investigations/draft preparations. Reads cap response size, attempts and backoff.
A 401 stops, 403 denies access and 404 leaves the outcome unknown. Timeouts and 429
cannot retry forever. Cancellation prevents further work; in-flight calls retain
their own timeout.

With subscription verification enabled, documentation questions use a larger
56,000-byte passage pack within a 64,000-byte answer input ceiling. The same
fitted passages reach the verifier and answer provider. The separate SSE
`verification` object records status, duration and reported subscription usage.

`cost_usd` is a conservative budget reservation: input bytes and framing are priced
as tokens, plus the output cap. Failed or ambiguous calls retain that reservation;
inference retries are disabled. A separate configured-price estimate uses reported
tokens. Neither value is an invoice. Fake and Ollama use no paid model API;
subscription calls consume plan quota, with unknown per-request billed USD.

## HTTP and SSE

Operator routes require Bearer authentication. Request schemas reject unknown
fields and cap bodies at 32 KiB. Requests cannot choose an origin, credential, tool
path or raised budget. Responses use no-store and a self-only content security
policy. Uvicorn access logging is disabled.

| Route | Behavior |
|---|---|
| `GET /health/live` | Process health |
| `GET /health/ready` | Assistant DB and corpus availability |
| `GET /v1/session` | Caller identity, scopes and providers |
| `POST /v1/assist` | `{question, request_id, provider}`; SSE investigation |
| `DELETE /v1/runs/{request_id}` | Cancel this caller's active request |
| `POST /v1/drafts/{thread_id}` | Ask fields plus `intent`; prepare and pause |
| `GET /v1/drafts/{thread_id}` | Read this caller's saved workflow |
| `POST /v1/drafts/{thread_id}/review` | `{continue_review, draft_sha256}`; recheck and resume |

SSE sends `started`, phase `step` updates, checked `text` deltas and a structured
`result`, or a redacted `error`. Text starts after schema/citation checks. Keepalives
cover upstream waits. The parser handles fragmented UTF-8, CRLF, multiline frames
and truncated streams. References include source IDs, excerpts and timestamps.
Valid citations establish provenance, not correctness.

## Draft review and console handoff

Choose case 04 and open **Prepare a reprocessing draft**. Supply the payout,
terminal observation and current reconciliation run IDs, plus a reason. The
workflow checks caller-scoped evidence, stores one content-bound action and pauses
with LangGraph `interrupt()`.

Review the exact bytes, digest and expiry. **Review & continue** rechecks identity,
permissions, evidence fingerprints and expiry; it sends no financial POST.
Restart and retry retain the same action ID. Changed content needs a new request.

**Copy payload** and **Open console** hand control to the operator. Enter the JSON
fields in the console's `REPROCESS_TERMINAL_LOCAL_BLOCKER` proposal form. The server
computes `proposal_sha256`, distinct from the local `draft_sha256`. A different
actor must review the retained proposal before execution. See
[AGENT-AND-REVIEW.md](AGENT-AND-REVIEW.md) for replay and invalidation rules.

## MCP

The stdio server exposes one read-only `investigate(question)` tool. It calls a
fixed assistant origin and returns checked structured output. Set
`LEDGEROPS_ASSISTANT_MCP_PROVIDER` to `fake`, `openai_api` or `ollama`; the HTTP
service must also enable that answer provider. The HTTP service controls passage
checking for documentation questions.
The tool has no approval, execution, credential, method or URL argument.

The credential file uses LedgerOps's `{"token": ...}` shape. It must be a regular,
owner-owned file with no group/other access. Symlinks and oversized files fail.
From `assistant/`:

```sh
LEDGEROPS_ASSISTANT_URL=http://127.0.0.1:8088 \
LEDGEROPS_ASSISTANT_TOKEN_FILE=/absolute/private/operator-credential.json \
LEDGEROPS_ASSISTANT_MCP_PROVIDER=fake \
uv run --locked python -m ledgerops_assistant.mcp_server
```

Keep credential contents out of arguments and repository configuration. The
adapter rereads the file for rotation and refuses redirects. Tests launch the real
stdio server to check discovery, structured output and access denial.

## Logs and evaluation

OpenTelemetry writes request spans to stderr: HTTP method/status, request ID,
elapsed time, steps, budget reservation and reported provider usage. Usage includes
input/output counts, completeness, model calls and a configured-price estimate.
No credential, authentication header, question/body, URL or source text is logged.

```sh
docker compose --env-file assistant/.env -f assistant/compose.yml logs api
```

Checkpoints contain incident questions and caller-scoped evidence. Protect the
assistant DB and its volumes. Local MLflow stores experiments under
`assistant/.cache/mlflow`. CI compares development retrieval metrics against a
pinned corpus, labels, model and configuration; it does not score new holdouts.
See the [retrieval report](RETRIEVAL-QUALITY-V2.md) for current measurements.

## Reproduce the checks

`make verify-release` runs the paid-provider/crashed-worker demo and an isolated
Compose acceptance test. It checks SSE usage/latency fields, prepares a draft,
restarts the API container and reviews the identical draft twice. It submits no
operator action. The command removes only its own containers and volumes; port
8088 must be free. Fake generation and hashing verify plumbing, not answer quality.

The [scenario snapshots](../scenarios/snapshots) come from real API calls against
disposable PostgreSQL. They cover `LIMIT_EXCEEDED`, an uncertain dispatch and
`MISSING_INTERNAL`. A retained refusal needs a new operation/key for a fresh
attempt. UNKNOWN stays held while original-key LOOKUP resolves the outcome.
Reconciliation reports differences; it does not repair them or release money.

```sh
make check
make test
LEDGEROPS_SCENARIOS_CHECK=1 uv run --locked python -m pytest -c pyproject.toml \
  -p tests.conftest -q assistant/scenarios/capture.py
uv run --project assistant --locked python -m pytest -c assistant/pyproject.toml assistant/tests
uv run --project assistant --locked python -m pytest -c assistant/pyproject.toml \
  assistant/tests -m 'postgres or model'
npm --prefix assistant/web ci
npm --prefix assistant/web run build
npm --prefix assistant/web test
```

Tests revoke scopes, switch owners, expire drafts, change evidence and reuse IDs
with different content. They cover 401/404/429, timeouts, bad responses, crashes
after INSERT and concurrent invocations. These local checks do not establish
production throughput or cloud availability. The
[Azure recipe](../../deploy/terraform/azure/README.md) is preparation-only; its
runtime behavior and cost remain unmeasured.
