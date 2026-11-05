# Local operator product

## Start and stop

From the repository root with Docker available:

```sh
python3 assistant/scripts/compose.py up
python3 assistant/scripts/compose.py check
```

Open <http://127.0.0.1:8088> and choose **Try synthetic demo**. Only this loopback
port is published. The stack contains dedicated PostgreSQL 18 / pgvector, an
initialization job, a synthetic GET-only LedgerOps API and the FastAPI/React app.
Initialization indexes repository documents with pinned bge-small weights. The
runtime role reads RAG tables and persists workflow/checkpoint records; it cannot
administer roles or databases. Initialization refuses a database containing the
financial `ledger` schema.

The script creates ignored `assistant/.env` with mode 0600 on first start. It
contains random local DB passwords and a synthetic operator token. Keep it private.
The demo token is served only in explicitly configured demo mode. No real bank is
involved.

```sh
python3 assistant/scripts/compose.py down
```

Volumes remain for restart and replay. `up --hashing` substitutes a stand-in
encoder for CI wiring checks; it is not retrieval-quality evidence. The image uses
a non-root runtime, read-only filesystem and dedicated cache volumes, installs
locked dependencies, and includes packaged SQL migrations and built React assets.

## Connect the existing LedgerOps API

Compose is a synthetic demonstration. For an existing local instance, run
`python -m ledgerops_assistant.runtime init` with a dedicated assistant database
owner, then `python -m ledgerops_assistant.runtime serve` with its runtime login.
Never supply the financial database DSN. Configuration is administrator-owned:

| Variable | Meaning |
|---|---|
| `LEDGEROPS_ASSISTANT_DATABASE_URL` | Dedicated assistant PostgreSQL DSN |
| `LEDGEROPS_ASSISTANT_API_ORIGIN` | Fixed upstream origin; HTTPS except loopback HTTP |
| `LEDGEROPS_ASSISTANT_CONSOLE_URL` | Existing operator console; default localhost:8080 |
| `LEDGEROPS_ASSISTANT_CORPUS_ROOT` | Repository documents for an installed package |
| `LEDGEROPS_ASSISTANT_WEB_DIST` | Built panel assets for an installed package |
| `LEDGEROPS_ASSISTANT_PROVIDERS` | Comma-separated enabled providers; default `fake` |
| `LEDGEROPS_ASSISTANT_EMBEDDER` | Default `bge`; `hashing` for wiring checks |

Leave `LEDGEROPS_ASSISTANT_DEMO_TOKEN` unset with a real upstream. Operators
connect with their own existing scoped credentials. The panel keeps the token in
memory, clears its input after connection and clears investigation/draft state on
disconnect. It uses no browser credential storage. Credentials never enter model
prompts or graph state. Each read and review rechecks identity and current upstream
permissions. Recent lists are bounded; empty lists and 404 do not prove no payment.

## Model selection and bounds

The panel lists server-enabled `fake`, `openai_api` and `ollama`. The official
local `chatgpt_subscription` SDK adapter is CLI-only and disabled in HTTP/CI.
See [PROVIDERS.md](PROVIDERS.md) for its explicit opt-in. No subscription turn was
run during implementation.

API generation requires the optional `openai` dependency, an out-of-Git
`OPENAI_API_KEY`, explicit `LEDGEROPS_ASSISTANT_OPENAI_MODEL`, and positive
`MODEL_INPUT_USD_PER_MILLION` / `MODEL_OUTPUT_USD_PER_MILLION` matching that model.
`MODEL_MAX_COST_USD` defaults to 0.10 per investigation. The bundled image enables
fake only; install the optional dependency in a separately configured environment
before enabling API generation. Ollama requires a reachable configured server and
`LEDGEROPS_ASSISTANT_OLLAMA_MODEL`.

Each investigation permits at most six model steps, a 24,000-byte context bound,
512 output tokens per call and a 60-second graph deadline. Calls have bounded
timeouts and automatic API inference retries are disabled. Reads bound size,
attempts and delay. A 401 stops, 403 denies scope, 404 stays ambiguous, and 429 or
timeouts cannot loop indefinitely. The service admits four simultaneous
investigations/draft preparations in one process. Cancellation prevents subsequent
work; in-flight calls remain subject to their timeout. This is a local service,
not a distributed admission controller or real cloud deployment.

Cost is a conservative reservation from input bytes, framing allowance, configured
prices and the output cap. Failed or ambiguous inference retains its reservation.
It is not an invoice. Fake/Ollama report zero API dollars; subscription mode
consumes personal plan quota, not an estimate derived from its subscription fee.

## HTTP and SSE

Bearer authentication is required for operator routes. Strict request models
reject unknown fields; bodies are capped at 32 KiB. Requests cannot choose an
origin, credential, tool path or raised budget. Responses use no-store and a
self-only content security policy. Uvicorn access logging is disabled.

| Route | Behavior |
|---|---|
| `GET /health/live` | Process health |
| `GET /health/ready` | Dedicated DB/corpus availability |
| `GET /v1/session` | Current caller identity, read scopes and providers |
| `POST /v1/assist` | `{question, request_id, provider}`; SSE investigation |
| `DELETE /v1/runs/{request_id}` | Cancel only this caller's active request |
| `POST /v1/drafts/{thread_id}` | Ask fields plus structured `intent`; prepare/interrupt |
| `GET /v1/drafts/{thread_id}` | Read this identity's persisted workflow |
| `POST /v1/drafts/{thread_id}/review` | `{continue_review, draft_sha256}`; recheck/resume |

SSE sends `started`, phase `step` updates, validated `text` deltas, then the
structured `result`, or a redacted `error`. Text begins after schema/citation
checks; unvalidated model tokens are not exposed. Keepalives cover upstream waits.
The panel parser handles fragmented UTF-8, CRLF, multiline data and truncation.
References include source IDs, excerpts and revision/observation timestamps.
These checks establish supplied sources, not semantic entailment.

## Human review and console handoff

Choose synthetic case 04, investigate and open **Prepare a reprocessing draft**.
Supply payout, terminal observation and current reconciliation run IDs plus the
operator's reason. The workflow verifies eligible authorized evidence, stores one
content-bound action and pauses with LangGraph `interrupt()`.

Review exact bytes, digest and expiry. **Review & continue** rechecks the same
maker, current permissions, evidence fingerprints and absolute expiry. It marks
the local draft ready for handoff and sends no financial POST. Restart/retry
retains the same action ID. Changing bound content requires **Start a new request**.

**Copy payload** and **Open console** hand control to the operator. In the existing
console's proposal form, enter `action_id`, `payout_id`, `observation_id`,
`evidence_run_id`, `reason` and `ttl_seconds` from the JSON for
`REPROCESS_TERMINAL_LOCAL_BLOCKER`. The server computes its own authoritative
proposal hash, distinct from `draft_sha256`. Another actor reviews that retained
proposal before execution. [HUMAN-REVIEW.md](HUMAN-REVIEW.md) covers expiry, replay
and changed-evidence behavior.

## MCP server

The stdio MCP server exposes one `investigate(question)` read-only tool. It calls
the fixed assistant origin with fake generation and returns checked structured
output. It has no approval, execute, credential, method or URL argument. The
credential JSON has the `{"token": ...}` shape produced by LedgerOps credential
administration. It must be a regular owner-owned file without group/other access;
symlinks and oversized files are rejected.

Configure a local MCP client to launch this process from `assistant/`:

```sh
LEDGEROPS_ASSISTANT_URL=http://127.0.0.1:8088 \
LEDGEROPS_ASSISTANT_TOKEN_FILE=/absolute/private/operator-credential.json \
uv run --locked python -m ledgerops_assistant.mcp_server
```

Use an existing operator credential file; do not put its contents in MCP arguments
or repository configuration. Stdio is the only supported transport. The adapter
re-reads the private file for rotation and refuses redirects. Integration tests
launch the actual stdio SDK server to check discovery, structured output and access
denial without external inference.

## Observability and evaluation

OpenTelemetry emits request spans to stderr. Each records full request duration
(including SSE), HTTP method/status, a random request identifier, steps and
`gen_ai.cost.estimated_usd`. It records neither token/header, question, body, URL
nor source text. Inspect local spans with:

```sh
docker compose --env-file assistant/.env -f assistant/compose.yml logs api
```

No external exporter is configured. Checkpoints do contain incident questions and
authorized evidence; protect the dedicated DB and volumes with appropriate local
access and retention.

Experiments use local MLflow SQLite and artifacts under `assistant/.cache/mlflow`.
CI preserves evaluation JSON and MLflow records for seven days. Its quality gate
uses the development split only and compares Recall with committed FTS/vector/
hybrid baselines; changed labels/model or hashing substitutes fail.
[EVALUATION-V1.md](EVALUATION-V1.md) reports the single held-out run. Fake answers
and synthetic security tests establish neither real-model answer quality nor
comprehensive injection resistance.
