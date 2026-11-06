# LedgerOps operator assistant

A separate package for investigating incidents with cited operating rules and
caller-scoped API observations. It combines PostgreSQL FTS, pinned local bge-small
embeddings, pgvector HNSW and RRF with a bounded LangGraph agent. Drafts pause with
`interrupt()` and persist in dedicated PostgreSQL; the operator submits the
reviewed payload through the existing LedgerOps console.

On the current 21-file/179-chunk corpus, the once-run **v3 holdout** has default
hybrid **Recall@5 0.8894 (95% section-cluster CI [0.8227, 0.9455])** over 58 audited
documentation questions in 40 source-section groups. Three pre-freeze audit
exclusions reduced the planned 60+12 set to 58+11. Non-documentation routing and
refusal each succeed on **6/11**; an indirect action, one injection and three
unsupported questions are incorrectly answered as documentation. Generation is
fake; these are retrieval and routing measurements, not real-model answer quality.
See [the full v3 report](docs/RETRIEVAL-QUALITY-V2.md#v3-holdout), including failures
and all paired intervals. No policy or label was changed after this run.

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

The previous 175-chunk v2 holdout and development-only context studies are
preserved as [historical results](docs/RETRIEVAL-QUALITY-V2.md#new-held-out-comparison).
They describe their original corpus snapshots. Current v3 uses synthetic labels,
objective pre-freeze audit and fake generation; the author-only-bundle declaration
is self-reported, and source facts overlap earlier sets. A limited historical
subscription smoke test does not establish current real-model answer quality.

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
