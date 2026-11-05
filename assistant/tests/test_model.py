"""Runs the real bge-small model. Deselected by default; run with ``-m model``."""

import numpy as np
import pytest

pytestmark = pytest.mark.model


def test_bge_small_puts_paraphrases_closer_than_unrelated_text():
    from ledgerops_assistant.embeddings import BgeSmallEmbedder

    embedder = BgeSmallEmbedder()
    passages = embedder.embed_passages(
        ["A timeout never releases the reserved money.", "Pierogi are boiled dumplings."]
    )
    query = embedder.embed_query("Does a timeout free the held funds?")
    assert passages.shape == (2, 384)
    assert np.allclose(np.linalg.norm(passages, axis=1), 1.0, atol=1e-5)
    assert query @ passages[0] > query @ passages[1] + 0.1
