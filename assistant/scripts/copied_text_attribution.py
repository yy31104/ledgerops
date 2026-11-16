# copied-text-attribution-v1
import hashlib
import json
import re
import statistics
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import psycopg

from ledgerops_assistant import benchmark as bench
from ledgerops_assistant.corpus import Corpus, load_corpus
from ledgerops_assistant.postgres import index_corpus, setup


def sentence_evidence(source, relevant_sections):
    evidence = {section: {} for section in sorted(relevant_sections)}
    for chunk in source.chunks:
        if chunk.section_id not in evidence:
            continue
        for unit in re.split(r"(?<=[.!?])\s+|\n+", chunk.body):
            quote = " ".join(unit.split())
            if len(quote.split()) >= 6:
                evidence[chunk.section_id].setdefault(quote, chunk.chunk_id)
    return evidence


def copied_text_scores(hits, relevant_sections, evidence):
    relevant = set(relevant_sections)
    unique = {}
    for hit in hits:
        unique.setdefault(hit.chunk.section_id, hit.chunk)
    selected = list(unique.values())[:5]
    strict = {chunk.section_id for chunk in selected} & relevant
    adjusted = set(strict)
    rows = []
    for chunk in selected:
        copies = []
        if chunk.path.startswith("__benchmark__/"):
            body = " " + " ".join(chunk.body.split()) + " "
            for section in sorted(relevant):
                for quote, origin in evidence[section].items():
                    if " " + quote + " " in body:
                        adjusted.add(section)
                        copies.append(
                            {"section_id": section, "source_chunk_id": origin, "quote": quote}
                        )
                        break
        rows.append(
            {
                "section_id": chunk.section_id,
                "chunk_id": chunk.chunk_id,
                "copied_sentence_evidence": copies,
            }
        )
    return {
        "strict": len(strict) / len(relevant),
        "adjusted": len(adjusted) / len(relevant),
        "strict_sections": sorted(strict),
        "adjusted_sections": sorted(adjusted),
        "top_five": rows,
    }


def cached_matrix(path, corpus, name):
    path = Path(path)
    meta = json.loads(path.with_suffix(".json").read_text())
    assert meta["sha256"] == bench.digest_file(path)
    assert meta["corpus_digest"] == corpus.digest and meta["embedder"] == name
    matrix = np.load(path)
    assert matrix.shape == (len(corpus.chunks), 384)
    return matrix, meta["sha256"]


def main():
    source = load_corpus()
    questions = bench._dev_questions(source.root)
    evidence = sentence_evidence(
        source, {section for q in questions for section in q["relevant_sections"]}
    )
    originals = {
        f"perf/results/{n}-{suffix}.json": bench.digest_file(
            Path(f"perf/results/{n}-{suffix}.json")
        )
        for n in (1000, 10000)
        for suffix in ("cold-encoding", "run1", "run2")
    }
    recorded = json.loads(Path("perf/results/1000-run1.json").read_text())
    assert source.digest == recorded["source"]["digest"]
    assert all(
        bench.digest_file(Path(name)) == digest for name, digest in recorded["code_sha256"].items()
    )
    assert (
        bench.digest_file(Path("eval/v1-draft/questions.jsonl"))
        == (recorded["quality"]["dev_file_sha256"])
    )
    analysis = Path(__file__).read_bytes()
    encoder = bench._encoder("bge")
    info = json.loads(
        Path(".cache/perf/sentence-mix-v1-1000-20261111-bge/vectors.json").read_text()
    )
    real, real_sha = cached_matrix(info["real"], source, encoder.name)
    result = {
        "schema_version": 1,
        "scope": "post_hoc_copied_sentence_attribution",
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "analysis_sha256": hashlib.sha256(analysis).hexdigest(),
        "source_corpus_digest": source.digest,
        "dev_questions_sha256": bench.digest_file(Path("eval/v1-draft/questions.jsonl")),
        "original_six_records_sha256": originals,
        "embedder": encoder.name,
        "vector_cache_sha256": {"real": real_sha},
        "definition": {
            "sentence_units": "generator punctuation-plus-whitespace or newline splitter",
            "minimum_whitespace_words": 6,
            "normalization": "whitespace only",
            "match": "complete unit with token boundaries; synthetic passages only",
            "ranking": "top 5 unique retrieved section IDs from search(k=30)",
            "denominator": "number of original relevant sections",
            "rerank_or_deduplicate_after_attribution": False,
            "strict_and_adjusted_use_same_hits": True,
        },
        "new_non_timed_ranking_pass": True,
        "original_benchmark_rerun": False,
        "independent_hnsw_index_builds": True,
        "populations": {},
    }
    with bench.disposable_postgres() as dsn:
        setup(dsn)
        result["environment"] = bench.environment(dsn, True)
        for scale in (0, 1000, 10000):
            data, matrix = source, real
            if scale:
                cache = Path(f".cache/perf/sentence-mix-v1-{scale}-20261111-bge")
                synthetic = bench._load_corpus(cache / "synthetic.jsonl", source.root)
                old = json.loads(Path(f"perf/results/{scale}-run1.json").read_text())
                assert synthetic.digest == old["measurements"]["generation_and_chunking"]["digest"]
                info = json.loads((cache / "vectors.json").read_text())
                vectors, sha = cached_matrix(info["synthetic"], synthetic, encoder.name)
                result["vector_cache_sha256"][str(scale)] = sha
                data = Corpus(
                    source.root, "copied-text-attribution", source.chunks + synthetic.chunks, {}
                )
                matrix = np.concatenate([real, vectors])
            bench._reset_owned_database(dsn)
            index_corpus(dsn, data, bench.CachedPassages(encoder.name, matrix))
            with psycopg.connect(dsn) as conn:
                conn.execute("ANALYZE assistant_rag.chunks")
            population = {"chunks": len(data.chunks), "digest": data.digest, "methods": {}}
            for name in bench.METHODS:
                retriever = bench._retriever(name, data, encoder, matrix, dsn)
                rows = {
                    q["id"]: copied_text_scores(
                        retriever.search(q["question"], 30), q["relevant_sections"], evidence
                    )
                    for q in questions
                }
                population["methods"][name] = {
                    metric: statistics.mean(row[metric] for row in rows.values())
                    for metric in ("strict", "adjusted")
                }
                population["methods"][name]["per_question"] = rows
                print(
                    scale,
                    name,
                    {k: population["methods"][name][k] for k in ("strict", "adjusted")},
                    flush=True,
                )
            result["populations"][str(scale)] = population
    assert all(bench.digest_file(Path(path)) == sha for path, sha in originals.items())
    bench.write_json(Path("perf/results/copied-text-attribution.json"), result)


if __name__ == "__main__":
    main()
