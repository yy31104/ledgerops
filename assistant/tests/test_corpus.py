from ledgerops_assistant.corpus import MAX_WORDS, chunk_markdown, slug


def test_slug_matches_github_anchor_rules():
    assert slug("Dispatch and recovery") == "dispatch-and-recovery"
    assert slug("Authenticated observations and one-time resolution (007)") == (
        "authenticated-observations-and-one-time-resolution-007"
    )
    assert slug("Large inputs: explicit format 10") == "large-inputs-explicit-format-10"


def test_hash_lines_inside_code_fences_are_not_headings():
    text = "# Doc\n\n## Run\n\n```sh\n# Set the DSN privately\nmake run\n```\n\nAfter.\n"
    chunks = chunk_markdown("docs/X.md", text)
    # The title section has no text of its own, so it produces no chunk.
    assert [c.section_id for c in chunks] == ["docs/X.md#run"]
    assert "# Set the DSN privately" in chunks[0].text


def test_long_sections_split_on_paragraphs_and_keep_their_title():
    paragraph = " ".join(["word"] * 100) + "."  # two paragraphs fit in one chunk
    text = "# Doc\n\n## Long\n\n" + "\n\n".join([paragraph] * 4)
    chunks = [c for c in chunk_markdown("docs/X.md", text) if c.heading == "Long"]
    assert len(chunks) == 2
    assert all(c.text.startswith("Doc > Long\n\n") for c in chunks)
    assert [c.chunk_id for c in chunks] == ["docs/X.md#long/0", "docs/X.md#long/1"]


def test_repeated_headings_get_github_suffixes():
    chunks = chunk_markdown("docs/X.md", "# Doc\n\n## Commands\n\na\n\n## Commands\n\nb\n")
    assert [c.section_id for c in chunks] == ["docs/X.md#commands", "docs/X.md#commands-1"]


def test_real_corpus_has_unique_ids_and_bounded_chunks(corpus):
    ids = [c.chunk_id for c in corpus.chunks]
    assert len(ids) == len(set(ids))
    # One sentence longer than the limit is kept whole; nothing else may exceed it much.
    assert max(len(c.body.split()) for c in corpus.chunks) < MAX_WORDS * 1.2
    assert "docs/PAYOUTS.md#dispatch-and-recovery" in corpus.section_ids()


def test_corpus_excludes_the_assistant_docs_that_contain_evaluation_answers(corpus):
    assert not any(path.startswith("assistant/") for path in corpus.file_digests)
