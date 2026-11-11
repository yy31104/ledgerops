"""Measurement contracts; no holdout queries or production configuration changes."""

import copy
import json
from collections import Counter

import numpy as np
import pytest
from tokenizers import Tokenizer, models, pre_tokenizers

from ledgerops_assistant import benchmark as bench
from ledgerops_assistant.corpus import Chunk, Corpus
from ledgerops_assistant.embeddings import BgeSmallEmbedder, HashingEmbedder
from ledgerops_assistant.postgres import PostgresRetriever, index_corpus


def test_generator_reproducibility_and_empirical_lengths(corpus):
    before = corpus.digest
    first = bench.generate_corpus(corpus, 358, 20261111)
    assert first.digest == bench.generate_corpus(corpus, 358, 20261111).digest
    assert first.digest != bench.generate_corpus(corpus, 358, 20261112).digest
    assert len(first.chunks) == len({c.chunk_id for c in first.chunks}) == 358
    assert Counter(len(c.text.split()) for c in first.chunks) == Counter(
        {
            length: count * 2
            for length, count in Counter(len(c.text.split()) for c in corpus.chunks).items()
        }
    )
    assert first.section_ids().isdisjoint(corpus.section_ids())
    assert all(c.path.startswith("__benchmark__/") for c in first.chunks)
    assert corpus.digest == before


@pytest.mark.parametrize("size", [0, -1, 1.5, True])
def test_generator_rejects_invalid_size(corpus, size):
    with pytest.raises(ValueError, match="positive integer"):
        bench.generate_corpus(corpus, size, 1)


@pytest.mark.parametrize("values", [[], [-1], [float("nan")], [float("inf")]])
def test_percentiles_reject_invalid_samples(values):
    with pytest.raises(ValueError, match="nonempty finite nonnegative"):
        bench.percentiles(values)


def test_percentiles_interpolate_instead_of_rounding_samples():
    assert bench.percentiles([4, 1, 3, 2]) == pytest.approx({"p50": 2.5, "p95": 3.85, "p99": 3.97})
    assert bench.percentiles([7]) == {"p50": 7, "p95": 7, "p99": 7}


def minimal_result():
    return {
        "schema_version": bench.SCHEMA_VERSION,
        "environment": dict.fromkeys(bench.ENV_FIELDS, "test"),
        "embedder_kind": "hashing",
        "quality_signal": False,
        "status": "completed",
        "smoke": True,
        "measurements": {"test": True},
        "queries": {},
    }


@pytest.mark.parametrize("field", bench.ENV_FIELDS)
def test_missing_environment_rejected_before_writing(tmp_path, field):
    result = minimal_result()
    del result["environment"][field]
    output = tmp_path / "missing.json"
    with pytest.raises(ValueError, match="missing environment"):
        bench.write_result(output, result)
    assert not output.exists()


def test_hashing_cannot_be_marked_as_quality_result(tmp_path):
    result = minimal_result()
    result["quality_signal"] = True
    with pytest.raises(ValueError, match="stand-in"):
        bench.write_result(tmp_path / "misleading.json", result)


def test_raw_samples_and_minimum_query_count_are_checked():
    result = minimal_result()
    result["queries"] = {"method": {"samples_ms": [1, 2], "latency_ms": {}}}
    with pytest.raises(ValueError, match="percentiles disagree"):
        bench.validate_result(result)
    result["smoke"] = False
    with pytest.raises(ValueError, match="insufficient query samples"):
        bench.validate_result(result)


def test_cache_checks_content_and_identity(tmp_path, tiny_corpus):
    encoder = HashingEmbedder(384)
    path, cold = bench._vectors(tiny_corpus, encoder, tmp_path)
    expected = np.load(path)
    assert cold["cache_hit"] is False
    assert bench._vectors(tiny_corpus, encoder, tmp_path)[1]["cache_hit"] is True
    record_path = path.with_suffix(".json")
    record = json.loads(record_path.read_text())
    record["embedder"] = "wrong-model"
    record_path.write_text(json.dumps(record))
    assert bench._vectors(tiny_corpus, encoder, tmp_path)[1]["cache_hit"] is False
    path.write_bytes(b"corrupt")
    assert bench._vectors(tiny_corpus, encoder, tmp_path)[1]["cache_hit"] is False
    np.testing.assert_array_equal(np.load(path), expected)


def test_untruncated_lengths_do_not_change_encoder_tokenizer(tmp_path):
    encoder = BgeSmallEmbedder.__new__(BgeSmallEmbedder)
    encoder.tokenizer = Tokenizer(models.WordLevel({"[UNK]": 0, "failure": 1}, "[UNK]"))
    encoder.tokenizer.pre_tokenizer = pre_tokenizers.Whitespace()
    encoder.tokenizer.enable_truncation(max_length=512)
    text = "failure " * 600
    corpus = Corpus(tmp_path, "test", (Chunk("p/0", "p", "p", "p", "p", text),), {})
    before = encoder.tokenizer.to_str()
    assert len(encoder.tokenizer.encode(text).ids) == 512
    stats = bench._token_lengths(corpus, encoder)
    assert stats["max"] == 600
    assert stats["over_512_count"] == 1
    assert encoder.tokenizer.to_str() == before


def test_memory_smoke_outputs_valid_schema(tmp_path):
    result = bench.run_benchmark(
        scale=200,
        seed=20261111,
        output=tmp_path / "smoke.json",
        cache=tmp_path / "cache",
        kind="hashing",
        queries=4,
        warmup=1,
        smoke=True,
        postgres=False,
    )
    assert result["quality_signal"] is False
    assert result["measurements"]["generation_and_chunking"]["chunks"] == 200
    assert set(result["queries"]) == {"memory_bm25", "memory_vector"}
    assert json.loads((tmp_path / "smoke.json").read_text()) == result
    invalid = copy.deepcopy(result)
    invalid["quality_signal"] = True
    with pytest.raises(ValueError, match="stand-in"):
        bench.validate_result(invalid)


@pytest.mark.postgres
def test_disposable_postgres_smoke(tmp_path):
    result = bench.run_benchmark(
        scale=200,
        seed=20261111,
        output=tmp_path / "pg-smoke.json",
        cache=tmp_path / "cache",
        kind="hashing",
        queries=4,
        warmup=1,
        smoke=True,
    )
    assert result["quality_signal"] is False
    assert set(result["queries"]) == set(bench.METHODS)
    assert result["environment"]["postgres_in_docker"] is True
    assert result["quality"]["dev_questions"] == 32
    for name in ("gin", "hnsw"):
        assert result["measurements"]["index_" + name]["sizes_bytes"][name] > 0
    for row in result["quality"]["hnsw_recall_at_10"].values():
        assert 0 <= row["mean"] <= 1


@pytest.mark.postgres
def test_ef100_adapter_matches_production_sql(pg_dsn, corpus):
    encoder = HashingEmbedder(384)
    key = index_corpus(pg_dsn, corpus, encoder)
    production = PostgresRetriever(pg_dsn, key, encoder, method="vector")
    adapter = bench.BenchmarkHNSW(pg_dsn, key, encoder, 100)
    query = "recover dispatch timeout without releasing funds"
    actual, expected = adapter.search(query), production.search(query)
    assert [(h.chunk.chunk_id, h.score) for h in actual] == [
        (h.chunk.chunk_id, h.score) for h in expected
    ]


def test_large_runs_require_opt_in_and_real_quality_prerequisites(tmp_path):
    args = {
        "scale": 100000,
        "seed": 1,
        "output": tmp_path / "out.json",
        "cache": tmp_path / "cache",
    }
    with pytest.raises(ValueError, match="allow-large"):
        bench.run_benchmark(**args)
    args.update(scale=1000, kind="hashing")
    with pytest.raises(ValueError, match="require BGE"):
        bench.run_benchmark(**args)
    assert not (tmp_path / "out.json").exists()
