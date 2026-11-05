# Durable draft review and console handoff

This workflow prepares a local draft of the existing
`REPROCESS_TERMINAL_LOCAL_BLOCKER` action. It never calls a LedgerOps POST endpoint.
Reviewing the draft is not the ledger's maker/checker approval. An operator must
submit the payload in the existing console, read the retained server proposal and
follow the original two-actor approval/execution checks there.

The graph has three nodes: investigate, prepare, review. Prepare writes the draft
before review calls `interrupt()`. A PostgresSaver checkpoints the state with sync
durability. Resume restarts the review node; it does not rerun the model or prepare
node. Even a crash before the prepare checkpoint cannot duplicate a draft: the
database binds the stable owner/request ID to canonical intent and evidence hashes.
The deterministic action UUID, original payload digest and absolute expiry are
reused. Different content under the same request identity fails explicitly.

The owner key includes the workspace, actor and stable client, not the token. Token
rotation can preserve ownership; switching actors/workspaces cannot read another
workflow. Credentials are runtime context only and are absent from checkpoints.
Every invocation rechecks the current maker scope through `/v1/me`. Resume also
re-reads the original API sources and rejects changed evidence, expired drafts,
changed digests or revoked permissions. The console still verifies the full
authoritative current state, original observation authenticity and maker/checker
versions: bounded API navigation data cannot replace those checks.

Preparation requires an authorized investigation showing the selected terminal
observation, its LOCAL_BLOCKER incident and the selected current reconciliation
reference, without overflow flags. UNKNOWN without a terminal observation is
insufficient. References/hashes do not prove source authenticity. The local
`draft_sha256` binds this review only; it is never used as `proposal_sha256` to
approve a ledger action.

Cross-process session advisory locks serialize a workflow invocation without a
transaction spanning network/model work. They live in the assistant database.
No financial database transaction, core approval code or existing console changes.

Run the real PostgreSQL recovery and concurrency tests:

```sh
cd assistant
uv run --locked python -m pytest -m postgres
```

They rebuild the workflow object after an interrupt, resume repeatedly, race two
draft writers, alter the original intent/evidence, revoke permission, change actor,
expire a draft and check that only one row/action identity remains. All model and
upstream replies in these recovery tests are deterministic fakes; the separate API
acceptance test exercises the actual LedgerOps service.

References: [LangGraph interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts),
[PostgreSQL checkpointer](https://docs.langchain.com/oss/python/langgraph/add-memory).
