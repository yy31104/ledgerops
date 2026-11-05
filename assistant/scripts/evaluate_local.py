"""Run retrieval evaluation on disposable PostgreSQL; no account or cloud required."""

import json
import os
import secrets
import subprocess
import sys
import time
from contextlib import contextmanager
from uuid import uuid4

import psycopg

from ledgerops_assistant.evaluation import main

IMAGE = "pgvector/pgvector@sha256:c29f30688a9ad961691bdf0ec1650717cc132a5814619d80f80463d6e4c42e47"


def docker(*args):
    return subprocess.run(
        ["docker", *args], check=True, capture_output=True, text=True, timeout=120
    ).stdout.strip()


@contextmanager
def temporary_database():
    name = "ledgerops-assistant-eval-" + uuid4().hex[:12]
    password = secrets.token_urlsafe(24)
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
        ports = json.loads(docker("inspect", name))[0]["NetworkSettings"]["Ports"]
        port = ports["5432/tcp"][0]["HostPort"]
        dsn = f"host=127.0.0.1 port={port} user=postgres password={password} dbname=postgres"
        for _ in range(60):
            try:
                with psycopg.connect(dsn, connect_timeout=1):
                    break
            except psycopg.OperationalError:
                time.sleep(0.25)
        else:
            raise RuntimeError("disposable evaluation database did not become ready")
        yield dsn
    finally:
        docker("rm", "--force", name)


if __name__ == "__main__":
    with temporary_database() as dsn:
        os.environ["LEDGEROPS_ASSISTANT_DATABASE_URL"] = dsn
        sys.exit(main([*sys.argv[1:], "--backend", "postgres", "--setup-database"]))
