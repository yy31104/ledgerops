# LedgerOps operator assistant

A separate package for investigating incidents with cited operating rules and
caller-scoped API observations. It combines PostgreSQL FTS, pinned local bge-small
embeddings, pgvector HNSW and RRF with a bounded LangGraph agent. Drafts pause with
`interrupt()` and persist in dedicated PostgreSQL; the operator submits the
reviewed payload through the existing LedgerOps console.

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

Current CI uses an explicit 21-file corpus and the reviewed main-docs development
baseline, [v3-main-docs-dev.json](eval/results/v3-main-docs-dev.json). New documents
are not indexed automatically; approved-file edits require label review and a new
baseline. The [main integration comparison](docs/RETRIEVAL-QUALITY-V2.md#current-main-document-integration)
records the remaining v1-011 missing-source regression and the lower Recall.

The original set has 40 development and 20 held-out questions. A development-only
fusion study selected RRF k=10, then froze code/configuration before evaluating a
new 60-question retrieval holdout. On that same new set, Recall@5 is **0.8639 for
FTS, 0.8361 for vectors, 0.8639 for old hybrid and 0.8889 for selected hybrid**.
MRR slightly trails the old hybrid; actual five-chunk coverage has wider
uncertainty. See [RETRIEVAL-QUALITY-V2.md](docs/RETRIEVAL-QUALITY-V2.md) for component
rank traces, topic-cluster intervals, per-question regressions and audit limits.
These held-out scores apply to the historical 175-chunk corpus, not the current
main-document revision. The current refresh uses fake generation only.
Labels are synthetic and not independently human-reviewed. Fake generation tests
plumbing and permissions. A limited real-subscription smoke test does not establish
answer quality or comprehensive injection resistance.

Those retrieval scores measure unchanged rankings. Generation now expands distinct
source sections with their introductions and neighboring document chunks, bounded
to 18 KB of serialized document context. This addresses conditions outside the
initial five chunks; see the development-only context study in the retrieval guide.
The previously used v2 holdout does not validate this new context policy.

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
  --baseline eval/results/v3-main-docs-dev.json --output .cache/report.json
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
