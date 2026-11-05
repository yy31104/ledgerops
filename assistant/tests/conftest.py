import json
import secrets
import subprocess
import time
from uuid import uuid4

import psycopg
import pytest

from ledgerops_assistant.corpus import Corpus, chunk_markdown, load_corpus
from ledgerops_assistant.postgres import setup


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


IMAGE = "pgvector/pgvector@sha256:c29f30688a9ad961691bdf0ec1650717cc132a5814619d80f80463d6e4c42e47"


def docker(*args):
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, check=True, timeout=90
    ).stdout.strip()


@pytest.fixture(scope="session")
def pg_dsn():
    """Only creates and removes its own container; never uses a developer database."""
    password = secrets.token_urlsafe(24)
    name = "ledgerops-assistant-test-" + uuid4().hex[:12]
    docker(
        "run",
        "--detach",
        "--name",
        name,
        "--publish",
        "127.0.0.1::5432",
        "--env",
        f"POSTGRES_PASSWORD={password}",
        IMAGE,
    )
    try:
        port = json.loads(docker("inspect", name))[0]["NetworkSettings"]["Ports"]["5432/tcp"][0][
            "HostPort"
        ]
        dsn = f"host=127.0.0.1 port={port} user=postgres password={password} dbname=postgres"
        for _ in range(60):
            try:
                with psycopg.connect(dsn, connect_timeout=1):
                    break
            except psycopg.OperationalError:
                time.sleep(0.25)
        else:
            pytest.fail("disposable PostgreSQL did not become ready")
        setup(dsn)
        yield dsn
    finally:
        docker("rm", "--force", name)
