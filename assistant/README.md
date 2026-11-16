# LedgerOps operator assistant

A separate package for investigating incidents with cited operating rules and
caller-scoped API observations. It combines PostgreSQL FTS, pinned local bge-small
embeddings, pgvector HNSW and RRF with a bounded LangGraph agent. Drafts pause with
`interrupt()` and persist in dedicated PostgreSQL; the operator submits the
reviewed payload through the existing LedgerOps console.

The operations-v4 corpus contains 20 explicitly selected operational files. The
repository homepage, navigation and article are excluded, so editing the homepage
does not invalidate the assistant's evaluation. Historical v2/v3 measurements
refer to their original corpus snapshots; they are not current answer-quality claims.
See the [current release measurements](docs/RETRIEVAL-QUALITY-V2.md#operations-v4).

Current development hybrid Recall@5 is **0.7448** on 32 documentation questions;
vector-only is 0.7552. A non-blind subscription sample has **27/32 fully complete
answers**, with five fact omissions. A separate fresh 40-question fake-generation
check gets **36/40 routes**, refusing all **20 action/injection cases** and making
no false refusal on six documentation controls. **Four of ten unsupported topics
are still admitted.** These synthetic samples are limited evidence, with no
independent human answer review or broad injection-safety claim. Historical v3
Recall@5 0.8894 belongs to the older homepage-inclusive corpus and is preserved in
the retrieval report rather than presented as current performance.

## Try the local product

From the repository root, with Docker running:

```sh
python3 assistant/scripts/compose.py up
```

Open <http://127.0.0.1:8088> and choose **Try synthetic demo**. The React panel shows
progress, checked references, uncertainty and estimated cost. Case 04 prepares a
draft for review. The demo uses a synthetic GET-only upstream and fake answer
generation; retrieval uses the real pinned local encoder. The first start downloads
public weights. Generated demo credentials stay in an ignored, owner-private
`assistant/.env`. No model API key is required.

```sh
python3 assistant/scripts/compose.py down
```

This stops the dedicated stack and retains database/model-cache volumes. See
[PRODUCT.md](docs/PRODUCT.md) for setup, SSE, telemetry and the stdio MCP server.

## Evidence and limits

Current CI uses the [operations-v4 development baseline](eval/baselines/operations-v4.json).
Approved operational-file edits require label review and a new baseline; changing
README.md does not. Existing question wording and required facts are retained in
[v4-dev](eval/v4-dev/manifest.json), with explicit source-path and excluded-homepage
label migration. Historical blind studies remain unchanged and are never rerun as
current evidence.

Raw evaluation/performance records live in checksum-verified release attachments;
Git contains [summaries](eval/summary.json), question sets, protocols and manifests.
See [evidence reproduction](eval/README.md). Citation checks establish provenance,
not semantic entailment; finite refusal rules cannot prove prompt-injection safety.

Generation expands distinct source sections with introductions and neighboring
chunks, bounded to 18 KB of serialized document context. The v3 artifact records
the actual provided context separately from raw ranking coverage.

Citation checks reject unknown passage IDs and uncited answers. They establish
provenance, not semantic entailment. Closed read tools and the upstream's current
permissions enforce access; prompt filtering is supplemental. A local draft digest
is distinct from the console's authoritative proposal hash.

## Develop and reproduce

```sh
cd assistant
uv sync --locked
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked python -m pytest
uv run --locked python -m pytest -m postgres
uv run --locked python scripts/evaluate_local.py --split dev \
  --baseline eval/baselines/operations-v4.json --output .cache/report.json
npm --prefix web ci
npm --prefix web run build
npm --prefix web test
```

Default Python tests use fake generation and stand-in embeddings. PostgreSQL tests
create their own disposable containers. Evaluation uses the pinned public encoder
and records experiments in local MLflow. CI also runs the full LedgerOps suite,
real authenticated API acceptance tests, the panel build and a Compose smoke test.

## Guides

| Guide | Contents |
|---|---|
| [RETRIEVAL-QUALITY-V2.md](docs/RETRIEVAL-QUALITY-V2.md) | Retrieval architecture, reproduction, historical comparison and new holdout |
| [AGENT-AND-REVIEW.md](docs/AGENT-AND-REVIEW.md) | Caller permissions, bounded tools, durable drafts and console handoff |
| [PRODUCT.md](docs/PRODUCT.md) | Local setup, HTTP/SSE, MCP, telemetry and verification |
| [PROVIDERS.md](docs/PROVIDERS.md) | Fake, OpenAI API, Ollama and local ChatGPT SDK adapter |
