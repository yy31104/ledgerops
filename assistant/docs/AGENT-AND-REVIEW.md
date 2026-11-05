# Read-only investigation and durable draft review

## Caller authority and bounded tools

The LangGraph agent loops `decide -> read -> decide`, then produces a cited answer.
Five named tools accept an optional business UUID. The model cannot select a URL,
HTTP method, workspace, credential or write action. The transport issues GET only,
does not follow redirects and contacts one administrator-configured API origin.

The operator supplies their own token. `/v1/me` establishes the stable workspace,
actor and client. Every read rechecks identity and the required scope; the upstream
remains the authorization authority. Planning and answer generation also recheck
ownership and all scopes required by cached successful observations before sending
evidence to the configured model or reserving cost. Cancellation and deadline are
checked again after the potentially blocking identity lookup.

A 401 stops the run, 403 denies scope, 404 remains ambiguous, and 429/timeouts have
bounded attempts and backoff. Oversized or malformed responses fail explicitly;
recent lists are bounded. Credentials are runtime context, absent from model prompts
and persisted graph state. Prompt injection detection supplements these capability
and permission boundaries; it does not replace them.

Limits cap steps, context bytes, HTTP attempts and wall time. API generation also
sets its output-token cap; the local subscription SDK uses concise-answer
instructions and deadlines rather than a hard output-token cap. API mode
requires explicit token prices and reserves a conservative input-byte/framing/output
bound before inference. Each input byte is charged at one token's configured rate,
so this deliberately overestimates typical text tokenization rather than estimating
actual token count. Failed or ambiguous inference retains its reservation and
automatic inference retries are disabled. `cost_usd` is an estimated upper bound,
not an invoice. Local subscription use consumes plan quota, with no dollar estimate
derived from the subscription fee. Cancellation prevents subsequent work;
provider deadlines bound in-flight requests and terminate the SDK subprocess group.

## Prepare, interrupt and resume

The workflow prepares a local `REPROCESS_TERMINAL_LOCAL_BLOCKER` draft. It never
calls a LedgerOps POST endpoint. Its nodes are investigate, prepare and review.
Prepare writes the draft before review calls `interrupt()`; PostgresSaver persists
state with sync durability. Resume restarts review without rerunning the model.

The stable owner/request identity binds canonical intent and evidence hashes. Even
a crash after the INSERT commits but before its checkpoint persists reuses the
same deterministic action UUID, payload digest and absolute expiry on retry.
Different content under the same request identity fails. Cross-process session
advisory locks serialize an invocation without holding a financial transaction
across network or model work.

The owner includes workspace, actor and stable client, not a particular token.
Token rotation can preserve ownership; switching owner cannot inspect another
workflow. Every invocation rechecks maker scope. Review rejects expired drafts,
wrong digests, revoked permissions and changed payout evidence. Fingerprints cover
the full payout, operation and investigation responses, including business state
and the persisted payout schedule. Metrics and account summaries may inform an
answer but do not authorize a payout draft and are excluded from its fingerprint
and review rereads. This also applies to older paused checkpoints, so a changing
`oldest_unresolved_seconds` alone cannot invalidate a draft.

Preparation requires an authorized, non-refused investigation for exactly the
requested payout, the selected terminal observation, its LOCAL_BLOCKER incident and
the selected current reconciliation run. Overflow flags, malformed projections or
incompatible incidents reject preparation. UNKNOWN without a terminal observation
is insufficient. Reasons preserve their exact bytes, must be nonblank and NUL-free,
and may contain no more than 256 UTF-8 bytes.

## Existing console remains authoritative

Local review is not the ledger's maker/checker approval. The operator submits the
reviewed JSON through the existing console, reads its retained proposal and follows
the original two-actor approval/execution checks. The local `draft_sha256` binds
this review; it is never the console's authoritative `proposal_sha256`.

Bounded API projections cannot establish complete history or source authenticity.
The console independently checks current proposal, actor, expiry, evidence epoch,
original observation authenticity and financial eligibility. A review continuation
sends no payment, releases no hold and executes no ledger action.

## Verification

From the repository root, with Docker running:

```sh
uv run --project assistant --locked python -m pytest -c assistant/pyproject.toml \
  assistant/tests -m postgres
uv run --project assistant --locked --with-editable . --with gunicorn==26.2.0 \
  python -m pytest -c pyproject.toml -p tests.conftest -q \
  assistant/scenarios/agent_acceptance.py assistant/scenarios/handoff_acceptance.py
```

Recovery tests inject a crash after draft commit, rebuild the workflow, race full
invocations and resume repeatedly. They assert one action and registry row, the
original payload/digest/expiry and no second model generation. Other tests change
intent, business evidence, owner, scope, expiry and metrics age. Deterministic fake
model/upstream responses test these control paths. The separate real-service
acceptance pair verifies caller permissions and one idempotent proposal handoff in
disposable PostgreSQL; it does not approve or execute a financial proposal.

References: [LangGraph interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts),
[PostgreSQL checkpointer](https://docs.langchain.com/oss/python/langgraph/add-memory).
