# Read-only investigation and draft review

## Caller permissions and tools

The LangGraph agent loops `decide -> read -> decide`, then produces a cited answer.
Five named tools accept an optional business UUID. The model cannot choose a URL,
HTTP method, workspace, credential or write action. The transport sends GET only,
refuses redirects and contacts one configured API origin.

The caller supplies a token. `/v1/me` identifies its workspace, actor and client.
Every read rechecks identity and scope. Planning and answer generation recheck the
permissions behind cached observations before sending them to a model. The upstream
API makes the access decision. Tokens stay in runtime context; model prompts and
persisted graph state contain no credentials.

A 401 stops the run, 403 denies access and 404 leaves the outcome unknown. Retries
for 429 and timeouts have bounded attempts and backoff. Malformed or oversized
responses fail; recent lists have explicit limits. An empty list does not prove
that an event never happened. Input filters supplement these controls; they do not
replace them or prove prompt-injection resistance.

## Request bounds and usage

Limits cap steps, input bytes, HTTP attempts and elapsed time. API generation also
caps output tokens. The subscription SDK uses a deadline and concise-answer
instructions because its turn interface has no hard output-token cap. Cancellation
prevents further work; a timed-out SDK worker has its process group terminated.

API mode requires explicit token prices. Before each call, it reserves input bytes,
framing and the output cap at those prices, treating one byte as one input token.
This deliberately conservative bound remains reserved after ambiguous failures;
there are no automatic inference retries. `cost_usd` is that reservation, not a bill.

Provider counters add planning and answer usage, including malformed outputs.
Missing counters or a failed call mark the total incomplete. SSE and request spans
include counts, completeness and latency. Configured API prices produce a separate
uncached-token estimate. Subscription usage is `subscription_quota`; its billed
USD remains unknown.

Document context uses citable chunks from one corpus snapshot and has an 18 KB
serialized JSON cap. When API observations grow, planning and answering remove
whole trailing document chunks to fit the total request bound. Observations are
never removed to make space; if they alone exceed the bound, inference is refused.
Only IDs in the final supplied context can be cited.

## Prepare, pause and resume

The workflow prepares a local `REPROCESS_TERMINAL_LOCAL_BLOCKER` draft. It has no
LedgerOps POST tool. Its nodes are investigate, prepare and review. Prepare stores
the draft before review calls `interrupt()`; `PostgresSaver` checkpoints with sync
durability. Resume restarts review without regenerating the answer.

Workspace, actor and stable client identify the owner. Token rotation can preserve
that identity; a different owner cannot read the workflow. Owner/request identity
binds canonical intent and evidence. A crash after the draft INSERT commits but
before its checkpoint is saved still reuses the action UUID, payload digest and
absolute expiry. Reusing the request identity with different content fails.
Session advisory locks serialize concurrent invocations without holding a financial
transaction across network or model calls.

Preparation needs a non-refused, caller-scoped investigation of the requested
payout, selected terminal observation, its LOCAL_BLOCKER incident and the current
reconciliation run. UNKNOWN alone is insufficient. Overflow flags, malformed
projections and incompatible incidents stop preparation. Reasons retain their
exact bytes: nonblank, NUL-free and at most 256 UTF-8 bytes.

Every invocation rechecks maker scope. Review rejects expiry, changed digests,
revoked permissions and changed payout evidence. The fingerprint includes payout,
operation and investigation responses, including the payout schedule. Metrics and
account summaries can inform an answer but cannot establish a draft's eligibility;
they are excluded from fingerprint checks, including older saved checkpoints.
A changing `oldest_unresolved_seconds` therefore does not invalidate a draft.

## Submit through the existing console

Local review does not perform the ledger's two-person approval. The operator
submits the reviewed JSON in the existing console, reads the retained proposal and
follows its distinct-actor approval and execution checks. `draft_sha256` binds the
local draft; the server computes its own `proposal_sha256`.

The console checks current actor permissions, expiry, evidence epoch, original
observation authenticity and financial eligibility. Continuing a draft sends no
payment, releases no hold and executes no ledger action. Bounded API projections
cannot establish a complete history or authenticate external source evidence.

## Test recovery and access checks

From the repository root, with Docker running:

```sh
uv run --project assistant --locked python -m pytest -c assistant/pyproject.toml \
  assistant/tests -m postgres
uv run --project assistant --locked --with-editable . --with gunicorn==26.2.0 \
  python -m pytest -c pyproject.toml -p tests.conftest -q \
  assistant/scenarios/agent_acceptance.py assistant/scenarios/handoff_acceptance.py
```

Recovery tests crash after draft commit, rebuild the workflow, race invocations
and resume repeatedly. They check one action/registry row, identical payload,
digest and expiry, and no second generation. Access tests change owner, scope,
intent, evidence and expiry; changing metric age must leave the draft intact.
The real-service tests use disposable PostgreSQL and stop at proposal handoff.

References: [LangGraph interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts),
[PostgreSQL checkpointer](https://docs.langchain.com/oss/python/langgraph/add-memory).
