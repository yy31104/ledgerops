# LedgerOps operator assistant

An assistant for the person on call, not for the ledger. It answers questions such as
"why was this payout refused?" or "does a timeout release the hold?" from the LedgerOps
docs, and cites the section each answer comes from. It never approves, executes or
releases anything; those stay in the two-person approval workflow.

It lives in this repository as a separate package so it can read the docs and replay
real incidents without adding dependencies to the ledger.

## How a question flows

[`routing.py`](src/ledgerops_assistant/routing.py) refuses requests to act and sends
questions about one payout's current state to the API →
[`retrieval.py`](src/ledgerops_assistant/retrieval.py) ranks versioned doc chunks with PostgreSQL FTS,
pgvector (HNSW or exact), and RRF; in-memory BM25 remains a comparison baseline →
[`answer.py`](src/ledgerops_assistant/answer.py) refuses if nothing is close enough, asks
the configured [provider](docs/PROVIDERS.md) for an answer, then removes any citation of
a passage the provider was not given.

The invariant: **an answer without a citation of a retrieved passage is never shown.** It
becomes a refusal.

## Results so far

On 57 answerable questions, hybrid retrieval puts a relevant section first for 0.62–0.68
of them and in the top five for 0.86–0.89. With this many questions, that is
statistically indistinguishable from plain BM25. A cosine-similarity gate catches
clearly unrelated questions but not payment-sounding ones such as "PayPal payouts". The
details and the failures are in [EVALUATION.md](docs/EVALUATION.md).

## Layout

| Path | What |
|---|---|
| [`docs/BASELINE.md`](docs/BASELINE.md) | How an operator handles three incidents today, without AI |
| [`scenarios/`](scenarios) | Those incidents reproduced on real PostgreSQL, with what the operator sees |
| [`src/ledgerops_assistant/`](src/ledgerops_assistant) | Corpus, retrieval, routing, providers, evaluation |
| [`eval/questions.jsonl`](eval/questions.jsonl) | 71 labelled questions (draft) |
| [`docs/EVALUATION.md`](docs/EVALUATION.md) | What was measured and what it does not show |
| [`docs/PROVIDERS.md`](docs/PROVIDERS.md) | Model settings: fake, OpenAI API, Ollama, ChatGPT plan |

## Run

```sh
cd assistant
uv sync --locked
uv run --locked python scripts/evaluate_local.py --split dev --output .cache/report.json
uv run --locked python -m ledgerops_assistant --backend memory "Does a timeout release a hold?"
uv run --locked python -m pytest
```

The first two commands download bge-small (about 134 MB) once. Tests use a stand-in
embedder and the `fake` provider, so they run offline and free; `-m model` adds the one
test that runs the real model.

Regenerate the incident snapshots from the repository root, with Docker running:

```sh
uv run --locked python -m pytest -c pyproject.toml -p tests.conftest -q assistant/scenarios/capture.py
```

Limits: answers come from the docs only. Reading live payout state through the API is
the next step, and no answer quality has been measured with a real model yet.

The current PostgreSQL evaluation and local MLflow workflow are in [POSTGRES.md](docs/POSTGRES.md) and [EVALUATION-V1.md](docs/EVALUATION-V1.md). The earlier results above remain historical v0 measurements. The current 60-question set has 40 development and 20 held-out questions; its owner review was deferred.
