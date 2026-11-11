"""Isolated, measurement-only retrieval benchmarks. Never used by the service.

Only the existing v1 development questions are queried. Synthetic snapshots and
vectors live in a dedicated cache, never in the operating corpus or a baseline.
"""

import argparse
import cProfile
import gc
import hashlib
import io
import json
import math
import os
import platform
import pstats
import random
import re
import resource
import secrets
import statistics
import subprocess
import sys
import time
import tracemalloc
from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from uuid import uuid4

import numpy as np
import psycopg

from ledgerops_assistant.context import ContextRetriever
from ledgerops_assistant.corpus import Chunk, Corpus, chunk_markdown, load_corpus
from ledgerops_assistant.embeddings import BgeSmallEmbedder, HashingEmbedder
from ledgerops_assistant.evaluation import score_ranking
from ledgerops_assistant.postgres import PostgresRetriever, _vector, corpus_key, index_corpus, setup
from ledgerops_assistant.retrieval import (
    BM25Retriever,
    Hit,
    HybridRetriever,
    VectorRetriever,
    rank_sections,
    tokenize,
)

GENERATOR_VERSION = "sentence-mix-v1"
SCHEMA_VERSION = 1
ROOT = Path(__file__).resolve().parents[3]
ASSISTANT = ROOT / "assistant"
IMAGE = "pgvector/pgvector@sha256:c29f30688a9ad961691bdf0ec1650717cc132a5814619d80f80463d6e4c42e47"
METHODS = (
    "memory_bm25",
    "memory_vector",
    "postgres_fts",
    "postgres_exact",
    "hnsw_40",
    "hnsw_100",
    "hnsw_200",
    "hybrid_10",
)
ENV_FIELDS = (
    "cpu",
    "logical_cores",
    "physical_cores",
    "memory_bytes",
    "os",
    "python",
    "numpy",
    "onnxruntime",
    "postgresql",
    "pgvector",
    "threads",
    "postgres_in_docker",
)


def digest_file(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


def percentiles(values):
    """Linear interpolation at (n-1)*p, retaining unrounded samples."""
    values = sorted(float(x) for x in values)
    if not values or any(not math.isfinite(x) or x < 0 for x in values):
        raise ValueError("expected nonempty finite nonnegative samples")

    def at(p):
        position = (len(values) - 1) * p
        lo, hi = math.floor(position), math.ceil(position)
        return values[lo] + (values[hi] - values[lo]) * (position - lo)

    return {"p50": at(0.50), "p95": at(0.95), "p99": at(0.99)}


def generate_corpus(source, size, seed):
    """Shuffle the empirical word lengths; fill them with sampled sentence tokens.

    Sentence boundaries are discarded only in synthetic data. Exact output size
    and word-count targets are independent of the production chunker's behavior.
    """
    if type(size) is not int or size <= 0:
        raise ValueError("size must be a positive integer")
    rng = random.Random(seed)
    lengths = [len(c.text.split()) for c in source.chunks]
    bank = [
        s.split()
        for c in source.chunks
        for s in re.split(r"(?<=[.!?])\s+|\n+", c.body)
        if s.split()
    ]
    if not lengths or not bank:
        raise ValueError("source must contain text")
    targets = lengths * (size // len(lengths)) + rng.sample(lengths, size % len(lengths))
    rng.shuffle(targets)
    chunks = []
    for i, target in enumerate(targets):
        words = []
        while len(words) < target - 1:
            words.extend(rng.choice(bank))
        path = f"__benchmark__/seed-{seed}/document-{i:06}.md"
        section = path + "#synthetic"
        chunks.append(
            Chunk(
                section + "/0",
                section,
                path,
                "Synthetic",
                "Synthetic",
                "Synthetic\n\n" + " ".join(words[: target - 1]),
            )
        )
    return Corpus(source.root, f"{GENERATOR_VERSION}:{seed}:{size}", tuple(chunks), {})


def rss_peak_bytes():
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(raw if sys.platform == "darwin" else raw * 1024)


def measured(function):
    """Non-latency phase timing includes allocation tracing; report that explicitly."""
    gc.collect()
    tracemalloc.start()
    started = time.perf_counter()
    value = function()
    seconds = time.perf_counter() - started
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return value, {
        "seconds": seconds,
        "python_current_bytes": current,
        "python_peak_bytes": peak,
        "rss_process_peak_bytes": rss_peak_bytes(),
        "instrumentation": "perf_counter + tracemalloc; RSS lifetime high-water",
    }


def profile(function):
    profiler = cProfile.Profile()
    profiler.runcall(function)
    output = io.StringIO()
    pstats.Stats(profiler, stream=output).strip_dirs().sort_stats("cumulative").print_stats(15)
    # Strip directory names; retain the profiler's unedited top-functions table.
    return {"sort": "cumulative", "limit": 15, "raw_text": output.getvalue()}


def _system_value(*command, fallback="unknown"):
    try:
        return subprocess.check_output(command, text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        return fallback


def environment(dsn=None, in_docker=False):
    if sys.platform == "darwin":
        cpu = _system_value("sysctl", "-n", "machdep.cpu.brand_string")
        physical = int(_system_value("sysctl", "-n", "hw.physicalcpu", fallback="0"))
        memory = int(_system_value("sysctl", "-n", "hw.memsize", fallback="0"))
    else:
        lines = Path("/proc/cpuinfo").read_text().splitlines()
        cpu = next(
            (s.split(":", 1)[1].strip() for s in lines if s.startswith("model name")),
            platform.machine(),
        )
        processors = [
            dict(
                (key.strip(), value.strip())
                for line in block.splitlines()
                if ":" in line
                for key, value in [line.split(":", 1)]
            )
            for block in "\n".join(lines).split("\n\n")
        ]
        cores = {
            (row.get("physical id", "").strip(), row.get("core id", "").strip())
            for row in processors
            if "core id" in row
        }
        physical = len(cores) if cores else "unknown"
        memory = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    result = {
        "cpu": cpu,
        "logical_cores": os.cpu_count(),
        "physical_cores": physical,
        "memory_bytes": memory,
        "os": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "python": platform.python_version(),
        "numpy": metadata.version("numpy"),
        "onnxruntime": metadata.version("onnxruntime"),
        "postgresql": "not run",
        "pgvector": "not run",
        "postgres_in_docker": in_docker,
        "threads": {
            "onnx_intra_op": 4,
            **{
                name: os.environ.get(name, "unset")
                for name in [
                    "OMP_NUM_THREADS",
                    "OPENBLAS_NUM_THREADS",
                    "MKL_NUM_THREADS",
                    "VECLIB_MAXIMUM_THREADS",
                ]
            },
        },
    }
    if dsn:
        with psycopg.connect(dsn) as conn:
            result["postgresql"] = conn.execute("SELECT version()").fetchone()[0]
            result["pgvector"] = conn.execute(
                "SELECT extversion FROM pg_extension WHERE extname='vector'"
            ).fetchone()[0]
            result["postgres_settings"] = {
                key: conn.execute("SELECT current_setting(%s)", (key,)).fetchone()[0]
                for key in [
                    "shared_buffers",
                    "work_mem",
                    "maintenance_work_mem",
                    "max_parallel_workers_per_gather",
                ]
            }
        if in_docker:
            info = json.loads(
                _system_value("docker", "info", "--format", "{{json .}}", fallback="{}")
            )
            result["docker"] = {
                key: info.get(key)
                for key in ["OperatingSystem", "Architecture", "NCPU", "MemTotal", "ServerVersion"]
            }
            result["docker_vm"] = platform.system() == "Darwin"
    return result


def validate_result(result):
    if result.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("invalid result schema version")
    if any(key not in result.get("environment", {}) for key in ENV_FIELDS):
        raise ValueError("missing environment field")
    if result.get("quality_signal") and result.get("embedder_kind") != "bge":
        raise ValueError("stand-in embeddings cannot establish retrieval quality")
    if result.get("status") != "completed" or not result.get("measurements"):
        raise ValueError("incomplete benchmark")
    for name, row in result.get("queries", {}).items():
        if not result["smoke"] and len(row["samples_ms"]) < 200:
            raise ValueError(f"insufficient query samples: {name}")
        if row["latency_ms"] != percentiles(row["samples_ms"]):
            raise ValueError("percentiles disagree with raw samples")
    json.dumps(result, allow_nan=False)


def write_result(path, result):
    validate_result(result)
    write_json(path, result)


def _save_corpus(path, corpus):
    path.write_text("".join(json.dumps(asdict(c)) + "\n" for c in corpus.chunks))


def _load_corpus(path, root):
    return Corpus(
        Path(root),
        GENERATOR_VERSION,
        tuple(Chunk(**json.loads(s)) for s in path.read_text().splitlines()),
        {},
    )


def _dev_questions(root):
    path = Path(root) / "assistant/eval/v1-draft/questions.jsonl"
    return [
        q
        for q in (json.loads(line) for line in path.read_text().splitlines())
        if q["split"] == "dev" and q["expected_route"] == "docs"
    ]


def _encoder(kind):
    return BgeSmallEmbedder() if kind == "bge" else HashingEmbedder(384)


def _vectors(corpus, encoder, cache):
    key = hashlib.sha256((corpus.digest + "\0" + encoder.name).encode()).hexdigest()
    path, manifest = cache / (key + ".npy"), cache / (key + ".json")
    hit = False
    if path.exists() and manifest.exists():
        record = json.loads(manifest.read_text())
        hit = (
            record.get("sha256") == digest_file(path)
            and record.get("corpus_digest") == corpus.digest
            and record.get("embedder") == encoder.name
            and record.get("shape") == [len(corpus.chunks), 384]
        )
    if hit:
        array, loading = measured(lambda: np.load(path))
        measurement = {
            "cache_hit": True,
            "seconds": None,
            "chunks_per_second": None,
            "cache_load": loading,
        }
    else:
        array, measurement = measured(
            lambda: encoder.embed_passages([c.text for c in corpus.chunks])
        )
        measurement.update(
            cache_hit=False, chunks_per_second=len(corpus.chunks) / measurement["seconds"]
        )
        np.save(path, array)
        write_json(
            manifest,
            {
                "sha256": digest_file(path),
                "corpus_digest": corpus.digest,
                "embedder": encoder.name,
                "shape": list(array.shape),
            },
        )
    if array.shape != (len(corpus.chunks), 384) or not np.isfinite(array).all():
        raise ValueError("invalid vector cache")
    if not np.allclose(np.linalg.norm(array, axis=1), 1, atol=1e-4):
        raise ValueError("vector cache is not normalized")
    return path, measurement


class CachedPassages:
    """Load fixed passage vectors; encode queries normally, without a query cache."""

    def __init__(self, name, matrix, delegate=None):
        self.name, self.matrix, self.delegate = name, matrix, delegate

    def embed_passages(self, texts):
        if len(texts) != len(self.matrix):
            raise ValueError("wrong vector row count")
        return self.matrix

    def embed_query(self, text):
        return self.delegate.embed_query(text)


class BenchmarkHNSW(PostgresRetriever):
    """Production vector SQL with benchmark-only ef and explicit ANN forcing."""

    def __init__(self, dsn, key, encoder, ef, *, force_index=False):
        super().__init__(dsn, key, encoder, method="vector")
        self.ef, self.force_index = ef, force_index

    def search(self, query, k=10):
        encoded = _vector(self.embedder.embed_query(query))
        with psycopg.connect(self.dsn, connect_timeout=3) as conn:
            conn.execute("SET TRANSACTION READ ONLY")
            conn.execute("SET LOCAL statement_timeout = '5s'")
            conn.execute("SELECT set_config('hnsw.ef_search',%s,true)", (str(self.ef),))
            conn.execute("SET LOCAL hnsw.iterative_scan = 'strict_order'")
            if self.force_index:
                conn.execute("SET LOCAL enable_seqscan = off")
            rows = conn.execute(
                "SELECT chunk_id,section_id,path,title,heading,body,1-(embedding <=> %s::vector) "
                "FROM assistant_rag.chunks WHERE corpus_id=%s "
                "ORDER BY embedding <=> %s::vector LIMIT %s",
                (encoded, self.corpus_id, encoded, k),
            ).fetchall()
        return [Hit(Chunk(*row[:6]), float(row[6])) for row in rows]


def _retriever(name, corpus, encoder, matrix, dsn):
    cached = CachedPassages(encoder.name, matrix, encoder)
    if name == "memory_bm25":
        return BM25Retriever(corpus)
    if name == "memory_vector":
        return VectorRetriever(corpus, cached)
    key = corpus_key(corpus, encoder)
    fts = PostgresRetriever(dsn, key, encoder, method="fts")
    if name == "postgres_fts":
        return fts
    if name == "postgres_exact":
        return PostgresRetriever(dsn, key, encoder, method="exact")
    if name.startswith("hnsw_"):
        return BenchmarkHNSW(dsn, key, encoder, int(name.split("_")[1]))
    if name == "hybrid_10":
        return HybridRetriever(fts, PostgresRetriever(dsn, key, encoder, method="vector"))
    raise ValueError("unknown method")


def _reset_owned_database(dsn):
    # Only called for the disposable container created below, never a supplied DSN.
    with psycopg.connect(dsn) as conn:
        conn.execute("TRUNCATE assistant_rag.chunks, assistant_rag.corpora")


def _explain(dsn, corpus, encoder, query, *, fts=False, ef=100, force_index=False):
    key = corpus_key(corpus, encoder)
    with psycopg.connect(dsn) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        conn.execute("SET LOCAL hnsw.iterative_scan = 'strict_order'")
        conn.execute("SELECT set_config('hnsw.ef_search',%s,true)", (str(ef),))
        if force_index:
            conn.execute("SET LOCAL enable_seqscan = off")
        if fts:
            terms = list(dict.fromkeys(tokenize(query)))[:128]
            return conn.execute(
                "EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) "
                "SELECT chunk_id,section_id,path,title,heading,body,ts_rank_cd(search,q,32) score "
                "FROM assistant_rag.chunks,websearch_to_tsquery('english',%s) q "
                "WHERE corpus_id=%s AND search @@ q ORDER BY score DESC,chunk_id LIMIT 10",
                (" OR ".join(terms), key),
            ).fetchone()[0]
        encoded = _vector(encoder.embed_query(query))
        return conn.execute(
            "EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) "
            "SELECT chunk_id,section_id,path,title,heading,body,1-(embedding <=> %s::vector) "
            "FROM assistant_rag.chunks WHERE corpus_id=%s "
            "ORDER BY embedding <=> %s::vector LIMIT 10",
            (encoded, key, encoded),
        ).fetchone()[0]


def _token_lengths(corpus, encoder):
    if not isinstance(encoder, BgeSmallEmbedder):
        return {"status": "not measured with stand-in embeddings"}
    from tokenizers import Tokenizer

    tokenizer = Tokenizer.from_str(encoder.tokenizer.to_str())
    tokenizer.no_truncation()
    tokenizer.no_padding()
    sizes = [len(tokenizer.encode(c.text).ids) for c in corpus.chunks]
    return {
        "status": "measured",
        "includes_special_tokens": True,
        "n": len(sizes),
        "percentiles": percentiles(sizes),
        "mean": statistics.mean(sizes),
        "max": max(sizes),
        "histogram": dict(sorted(Counter(sizes).items())),
        "over_512_count": sum(n > 512 for n in sizes),
        "over_512_first_20": [
            {"chunk_id": c.chunk_id, "tokens": n}
            for c, n in zip(corpus.chunks, sizes, strict=True)
            if n > 512
        ][:20],
    }


def _worker(config, stage):
    cache = Path(config["cache"])
    root = Path(config["root"])
    source = load_corpus(root)
    corpus_path = cache / "synthetic.jsonl"
    if stage == "generate":
        # Small discarded warmup, then measured generation and separate rechunking.
        generate_corpus(source, min(20, config["scale"]), config["seed"])
        corpus, generation = measured(
            lambda: generate_corpus(source, config["scale"], config["seed"])
        )
        for chunk in corpus.chunks[:20]:
            chunk_markdown(chunk.path, "# Synthetic\n\n" + chunk.body)
        chunk_count, chunking = measured(
            lambda: sum(
                len(chunk_markdown(c.path, "# Synthetic\n\n" + c.body)) for c in corpus.chunks
            )
        )
        _save_corpus(corpus_path, corpus)
        return {
            "digest": corpus.digest,
            "chunks": len(corpus.chunks),
            "generation": {
                **generation,
                "chunks_per_second": len(corpus.chunks) / generation["seconds"],
            },
            "chunking": {
                **chunking,
                "input_documents": len(corpus.chunks),
                "output_chunks": chunk_count,
                "chunks_per_second": chunk_count / chunking["seconds"],
                "documents_per_second": len(corpus.chunks) / chunking["seconds"],
            },
            "word_counts": {
                "source": percentiles([len(c.text.split()) for c in source.chunks]),
                "synthetic": percentiles([len(c.text.split()) for c in corpus.chunks]),
            },
        }
    corpus = _load_corpus(corpus_path, root)
    if stage == "embed":
        encoder, initialization = measured(lambda: _encoder(config["embedder"]))
        encoder.embed_passages([c.text for c in corpus.chunks[:16]])
        vectors, synthetic = _vectors(corpus, encoder, cache)
        real_vectors, real = _vectors(source, encoder, cache.parent / "real-vectors")
        write_json(
            cache / "vectors.json",
            {"name": encoder.name, "synthetic": str(vectors), "real": str(real_vectors)},
        )
        return {
            "name": encoder.name,
            "initialization": initialization,
            "synthetic": synthetic,
            "real": real,
            "tokens_synthetic": _token_lengths(corpus, encoder),
            "tokens_real": _token_lengths(source, encoder),
            "batch_profile": profile(
                lambda: encoder.embed_passages([c.text for c in corpus.chunks[:16]])
            ),
            "rss_process_peak_bytes": rss_peak_bytes(),
        }
    vector_info = json.loads((cache / "vectors.json").read_text())
    matrix = np.load(vector_info["synthetic"])
    dsn = os.environ.get("LEDGEROPS_BENCHMARK_DSN")
    if stage == "ingest":
        setup(dsn)
        warm = Corpus(root, "benchmark-warmup", corpus.chunks[:20], {})
        index_corpus(dsn, warm, CachedPassages(vector_info["name"], matrix[:20]))
        _reset_owned_database(dsn)
        with psycopg.connect(dsn) as conn:
            conn.execute("DROP INDEX assistant_rag.chunks_search_idx")
            conn.execute("DROP INDEX assistant_rag.chunks_embedding_idx")
        _, measurement = measured(
            lambda: index_corpus(dsn, corpus, CachedPassages(vector_info["name"], matrix))
        )
        return {
            "write": measurement,
            "rows": len(corpus.chunks),
            "database_warmup_rows": len(warm.chunks),
            "secondary_indexes_during_write": False,
            "environment": environment(dsn, True),
        }
    if stage.startswith("index_"):
        kind = stage.split("_")[1]
        statement = (
            "CREATE INDEX chunks_search_idx ON assistant_rag.chunks USING gin(search)"
            if kind == "gin"
            else "CREATE INDEX chunks_embedding_idx ON assistant_rag.chunks "
            "USING hnsw(embedding vector_cosine_ops) WITH (m=16,ef_construction=64)"
        )

        def build():
            with psycopg.connect(dsn) as conn:
                conn.execute(statement)

        _, measurement = measured(build)
        with psycopg.connect(dsn) as conn:
            conn.execute("ANALYZE assistant_rag.chunks")
            sizes = {
                key: conn.execute("SELECT pg_relation_size(%s)", (relation,)).fetchone()[0]
                for key, relation in {
                    "table_main_fork": "assistant_rag.chunks",
                    kind: "assistant_rag.chunks_search_idx"
                    if kind == "gin"
                    else "assistant_rag.chunks_embedding_idx",
                }.items()
            }
            sizes["table_total_with_toast_and_indexes"] = conn.execute(
                "SELECT pg_total_relation_size('assistant_rag.chunks')"
            ).fetchone()[0]
        return {"build": measurement, "sizes_bytes": sizes}
    encoder, model_initialization = measured(lambda: _encoder(config["embedder"]))
    questions = _dev_questions(root)
    queries = [questions[i % len(questions)]["question"] for i in range(config["queries"])]
    random.Random(config["seed"]).shuffle(queries)
    if stage.startswith("query:"):
        name = stage.split(":", 1)[1]
        retriever, initialization = measured(lambda: _retriever(name, corpus, encoder, matrix, dsn))
        for query in queries[: config["warmup"]]:
            retriever.search(query, 10)
        samples = []
        for query in queries:
            started = time.perf_counter()
            retriever.search(query, 10)
            samples.append((time.perf_counter() - started) * 1000)
        _, memory = measured(lambda: retriever.search(queries[0], 10))
        result = {
            "samples_ms": samples,
            "latency_ms": percentiles(samples),
            "single_client_qps": len(samples) * 1000 / sum(samples),
            "warmup_queries": config["warmup"],
            "query_encoding_included": name != "memory_bm25" and name != "postgres_fts",
            "initialization": initialization,
            "model_initialization": model_initialization,
            "one_query_memory_probe": memory,
            "rss_process_peak_bytes": rss_peak_bytes(),
            "latency_instrumentation": "perf_counter only",
        }
        if name == "memory_bm25":
            result["profile"] = profile(lambda: retriever.search(queries[0], 10))
        if name == "postgres_fts" or name.startswith("hnsw_"):
            result["explain"] = _explain(
                dsn,
                corpus,
                encoder,
                queries[0],
                fts=name == "postgres_fts",
                ef=int(name.split("_")[1]) if name.startswith("hnsw_") else 100,
            )
        return result
    if stage == "quality":
        gc.collect()
        tracemalloc.start()
        quality_started = time.perf_counter()
        exact = PostgresRetriever(dsn, corpus_key(corpus, encoder), encoder, method="exact")
        ann = {
            str(ef): BenchmarkHNSW(dsn, exact.corpus_id, encoder, ef, force_index=True)
            for ef in (40, 100, 200)
        }
        recalls = {ef: [] for ef in ann}
        ties = 0
        for query in queries:
            gold = exact.search(query, 11)
            wanted = {h.chunk.chunk_id for h in gold[:10]}
            ties += len(gold) > 10 and abs(gold[9].score - gold[10].score) < 1e-6
            for ef, retriever in ann.items():
                found = {h.chunk.chunk_id for h in retriever.search(query, 10)}
                recalls[ef].append(len(wanted & found) / len(wanted))
        ann_plans = {
            ef: _explain(dsn, corpus, encoder, queries[0], ef=int(ef), force_index=True)
            for ef in ann
        }
        quality = {}
        for population, data, vectors in [
            ("real_only", source, np.load(vector_info["real"])),
            (
                "real_plus_distractors",
                Corpus(root, "benchmark-mixed", source.chunks + corpus.chunks, {}),
                np.concatenate([np.load(vector_info["real"]), matrix]),
            ),
        ]:
            _reset_owned_database(dsn)
            index_corpus(dsn, data, CachedPassages(encoder.name, vectors))
            with psycopg.connect(dsn) as conn:
                conn.execute("ANALYZE assistant_rag.chunks")
            quality[population] = {}
            for name in METHODS:
                retriever = _retriever(name, data, encoder, vectors, dsn)
                values = {
                    q["id"]: score_ranking(
                        rank_sections(retriever.search(q["question"], 30)), q["relevant_sections"]
                    )["recall@5"]
                    for q in questions
                }
                quality[population][name] = {
                    "recall@5": statistics.mean(values.values()),
                    "per_question": values,
                }
        hybrid = _retriever("hybrid_10", data, encoder, vectors, dsn)
        hits = hybrid.search(queries[0], 30)

        class FixedHits:
            name = "fixed-ranking-for-selection-profile"

            def search(self, query, k):
                return hits[:k]

        context = ContextRetriever(FixedHits(), data)
        context_profile = profile(lambda: context.context(queries[0], 5))
        current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        return {
            "hnsw_recall_at_10": {
                ef: {"mean": statistics.mean(v), "per_query": v} for ef, v in recalls.items()
            },
            "ann_force_index": True,
            "ann_explain": ann_plans,
            "boundary_ties_within_1e-6": ties,
            "dev_recall": quality,
            "dev_questions": len(questions),
            "dev_file_sha256": digest_file(root / "assistant/eval/v1-draft/questions.jsonl"),
            "context_profile": context_profile,
            "context_profile_excludes_retrieval": True,
            "model_initialization": model_initialization,
            "measurement": {
                "seconds": time.perf_counter() - quality_started,
                "python_current_bytes": current,
                "python_peak_bytes": peak,
                "instrumentation": "perf_counter + tracemalloc",
            },
            "rss_process_peak_bytes": rss_peak_bytes(),
        }
    raise ValueError("unknown worker stage")


@contextmanager
def disposable_postgres():
    """No DSN option: all destructive setup is confined to this owned container."""
    name, password = "ledgerops-perf-" + uuid4().hex[:12], secrets.token_urlsafe(24)
    subprocess.run(
        [
            "docker",
            "run",
            "--detach",
            "--name",
            name,
            "--publish",
            "127.0.0.1::5432",
            "--env",
            f"POSTGRES_PASSWORD={password}",
            IMAGE,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    try:
        info = json.loads(subprocess.check_output(["docker", "inspect", name], text=True))[0]
        port = info["NetworkSettings"]["Ports"]["5432/tcp"][0]["HostPort"]
        dsn = f"host=127.0.0.1 port={port} user=postgres password={password} dbname=postgres"
        for _ in range(80):
            try:
                with psycopg.connect(dsn, connect_timeout=1):
                    break
            except psycopg.OperationalError:
                time.sleep(0.25)
        else:
            raise RuntimeError("disposable benchmark database unavailable")
        yield dsn
    finally:
        subprocess.run(["docker", "rm", "--force", name], check=True, capture_output=True)


def run_benchmark(
    *,
    scale,
    seed,
    output,
    cache,
    kind="bge",
    queries=200,
    warmup=16,
    smoke=False,
    postgres=True,
    allow_large=False,
    root=ROOT,
):
    if scale == 100000 and not allow_large:
        raise ValueError("100k requires --allow-large")
    if not smoke and (scale not in (1000, 10000, 100000) or queries < 200):
        raise ValueError("real runs require a supported scale and at least 200 queries")
    if kind not in ("bge", "hashing"):
        raise ValueError("unknown embedder kind")
    if not smoke and (kind != "bge" or not postgres):
        raise ValueError("real experiments require BGE and disposable PostgreSQL")
    if queries < 1 or not 1 <= warmup <= queries:
        raise ValueError("invalid query/warmup counts")
    root, cache, output = Path(root).resolve(), Path(cache).resolve(), Path(output)
    corpus = load_corpus(root)
    cache.mkdir(parents=True, exist_ok=True)
    (cache.parent / "real-vectors").mkdir(parents=True, exist_ok=True)
    config = {
        "root": str(root),
        "cache": str(cache),
        "scale": scale,
        "seed": seed,
        "embedder": kind,
        "queries": queries,
        "warmup": warmup,
    }
    configuration = cache / "configuration.json"
    write_json(configuration, config)
    result = {
        "schema_version": SCHEMA_VERSION,
        "status": "started",
        "smoke": smoke,
        "quality_signal": kind == "bge",
        "embedder_kind": kind,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "generator": {"version": GENERATOR_VERSION, "seed": seed, "scale": scale},
        "source": {
            "revision": corpus.revision,
            "digest": corpus.digest,
            "files": len(corpus.file_digests),
            "chunks": len(corpus.chunks),
        },
        "code_sha256": {
            "src/ledgerops_assistant/" + filename: digest_file(
                root / "assistant/src/ledgerops_assistant" / filename
            )
            for filename in [
                "benchmark.py",
                "corpus.py",
                "embeddings.py",
                "retrieval.py",
                "postgres.py",
                "context.py",
                "evaluation.py",
            ]
        },
        "environment": environment(),
        "measurements": {},
        "queries": {},
        "worker_wall_seconds": {},
        "memory_method": (
            "Fresh subprocess per stage/method; RSS is that worker's lifetime peak "
            "including inputs, warmup and profile. Latency tracing is off; memory is a "
            "separate query probe. Other phase timings include tracemalloc overhead. "
            "PostgreSQL server memory is not Python client RSS."
        ),
    }
    started = time.perf_counter()

    def stage(name, dsn=None):
        target = cache / (name.replace(":", "-") + ".json")
        env = {**os.environ, "PYTHONPATH": str(root / "assistant/src")}
        if dsn:
            env["LEDGEROPS_BENCHMARK_DSN"] = dsn
        before = time.perf_counter()
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "ledgerops_assistant.benchmark",
                "--worker",
                name,
                "--configuration",
                str(configuration),
                "--output",
                str(target),
            ],
            env=env,
            capture_output=True,
            text=True,
        )
        if completed.returncode:
            (cache / "worker-failure.txt").write_text(completed.stderr)
            raise RuntimeError(f"benchmark stage {name} failed; private diagnostic saved in cache")
        result["worker_wall_seconds"][name] = time.perf_counter() - before
        print(f"Completed {name}: {result['worker_wall_seconds'][name]:.2f}s", flush=True)
        return json.loads(target.read_text())

    result["measurements"]["generation_and_chunking"] = stage("generate")
    result["measurements"]["encoding"] = stage("embed")
    for method in ("memory_bm25", "memory_vector"):
        result["queries"][method] = stage("query:" + method)
    if postgres:
        with disposable_postgres() as dsn:
            result["measurements"]["postgres_ingest"] = stage("ingest", dsn)
            result["environment"] = result["measurements"]["postgres_ingest"].pop("environment")
            for kind_name in ("gin", "hnsw"):
                result["measurements"]["index_" + kind_name] = stage("index_" + kind_name, dsn)
            for method in METHODS[2:]:
                result["queries"][method] = stage("query:" + method, dsn)
            result["quality"] = stage("quality", dsn)
    else:
        result["quality"] = {"status": "not run: memory smoke only"}
    result.update(status="completed", elapsed_seconds=time.perf_counter() - started)
    write_result(output, result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scale", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=20261111)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cache", type=Path, default=ASSISTANT / ".cache/perf")
    parser.add_argument("--allow-large", action="store_true")
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--memory-only", action="store_true")
    parser.add_argument("--worker", help=argparse.SUPPRESS)
    parser.add_argument("--configuration", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.worker:
        config = json.loads(args.configuration.read_text())
        write_json(args.output, _worker(config, args.worker))
        return 0
    run_benchmark(
        scale=args.scale,
        seed=args.seed,
        output=args.output,
        cache=args.cache
        / f"{GENERATOR_VERSION}-{args.scale}-{args.seed}-{'hashing' if args.smoke else 'bge'}",
        kind="hashing" if args.smoke else "bge",
        smoke=args.smoke,
        queries=4 if args.smoke else 200,
        warmup=1 if args.smoke else 16,
        postgres=not args.memory_only,
        allow_large=args.allow_large,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
