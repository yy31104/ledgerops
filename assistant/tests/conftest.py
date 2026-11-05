import pytest

from ledgerops_assistant.corpus import Corpus, chunk_markdown, load_corpus


@pytest.fixture(scope="session")
def corpus():
    return load_corpus()


@pytest.fixture
def tiny_corpus(tmp_path):
    text = """# Payouts

Intro about payouts.

## Dispatch and recovery

A timeout never releases the hold. Recovery starts with LOOKUP.

## Amounts

LIMIT_EXCEEDED means clearing capacity is already committed.
"""
    chunks = tuple(chunk_markdown("docs/PAYOUTS.md", text))
    return Corpus(root=tmp_path, revision="test", chunks=chunks, file_digests={})
