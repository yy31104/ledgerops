"""Real-component fixtures: one disposable cluster, a fresh database per test."""

import json
import os
import secrets
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from ledgerops.db.migrations import apply_migrations

POSTGRES_IMAGE = "postgres@sha256:5a5a84b19854a9ffaa54082c166ff4ec27473a361e496e5ea167f298f2da9722"
ROOT = Path(__file__).resolve().parents[1]


def docker(*args: str) -> str:
    return subprocess.run(
        ["docker", *args], check=True, capture_output=True, text=True, timeout=60
    ).stdout.strip()


@dataclass(frozen=True, repr=False)
class TestDatabase:
    port: int
    admin_password: str
    app_password: str
    name: str
    reader_password: str
    maintenance_password: str

    def connect(self, role: str = "app") -> psycopg.Connection:
        if role not in {"app", "reader", "maintenance", "owner", "admin"}:
            raise ValueError("unknown test connection role")
        conn = psycopg.connect(
            host="127.0.0.1",
            port=self.port,
            dbname=self.name,
            user={
                "app": "ledger_app",
                "reader": "ledger_reader",
                "maintenance": "ledger_maintenance",
            }.get(role, "postgres"),
            password={
                "app": self.app_password,
                "reader": self.reader_password,
                "maintenance": self.maintenance_password,
            }.get(role, self.admin_password),
            autocommit=True,
            connect_timeout=2,
            options="-c timezone=UTC -c statement_timeout=10000 -c lock_timeout=5000",
        )
        if role == "owner":
            conn.execute("SET ROLE ledger_migrator")
        return conn


@pytest.fixture(scope="session")
def postgres_cluster():
    """Never connects to an existing database or deletes a pre-existing container."""
    admin_password = secrets.token_urlsafe(24)
    app_password = secrets.token_urlsafe(24)
    reader_password = secrets.token_urlsafe(24)
    maintenance_password = secrets.token_urlsafe(24)
    container_id = None
    try:
        docker("version", "--format", "{{.Server.Version}}")
        result = subprocess.run(
            [
                "docker",
                "run",
                "--detach",
                "--rm",
                "--name",
                f"ledgerops-r1-tests-{uuid4().hex[:12]}",
                "--label",
                "ledgerops.scope=disposable-test",
                "--publish",
                "127.0.0.1::5432",
                "--env",
                "POSTGRES_PASSWORD",
                "--tmpfs",
                "/var/lib/postgresql:rw,size=512m",
                POSTGRES_IMAGE,
                "-c",
                "max_wal_size=128MB",
            ],
            env={**os.environ, "POSTGRES_PASSWORD": admin_password},
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        container_id = result.stdout.strip()
        bindings = json.loads(
            docker(
                "inspect",
                "--format",
                '{{json (index .NetworkSettings.Ports "5432/tcp")}}',
                container_id,
            )
        )
        cluster = TestDatabase(
            int(bindings[0]["HostPort"]),
            admin_password,
            app_password,
            "postgres",
            reader_password,
            maintenance_password,
        )
        deadline = time.monotonic() + 30
        while True:
            try:
                conn = cluster.connect("admin")
                break
            except psycopg.OperationalError:
                if time.monotonic() >= deadline:
                    pytest.fail("disposable PostgreSQL failed to become ready within 30 seconds")
                time.sleep(0.1)  # readiness polling, not a concurrency-test schedule
        with conn:
            assert conn.execute("SHOW server_version_num").fetchone()[0] == "180006"
            conn.execute("""
                CREATE ROLE ledger_migrator NOLOGIN NOSUPERUSER NOBYPASSRLS
                  NOCREATEDB NOCREATEROLE NOREPLICATION
            """)
            conn.execute(
                sql.SQL("""
                CREATE ROLE ledger_app LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB
                  NOCREATEROLE NOREPLICATION PASSWORD {}
            """).format(sql.Literal(app_password))
            )
            conn.execute(
                sql.SQL("""
                CREATE ROLE ledger_reader LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB
                  NOCREATEROLE NOREPLICATION PASSWORD {}
            """).format(sql.Literal(reader_password))
            )
            conn.execute(
                sql.SQL("""
                CREATE ROLE ledger_maintenance LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB
                  NOCREATEROLE NOREPLICATION PASSWORD {}
            """).format(sql.Literal(maintenance_password))
            )
        yield cluster
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        pytest.fail(
            f"real PostgreSQL tests require Docker and the pinned image: {type(exc).__name__}"
        )
    finally:
        if container_id is not None:
            docker("rm", "--force", container_id)


@pytest.fixture
def db(postgres_cluster):
    cluster = postgres_cluster
    name = f"ledgerops_test_{uuid4().hex}"
    database = TestDatabase(
        cluster.port,
        cluster.admin_password,
        cluster.app_password,
        name,
        cluster.reader_password,
        cluster.maintenance_password,
    )
    with cluster.connect("admin") as admin:
        admin.execute(
            sql.SQL("CREATE DATABASE {} OWNER ledger_migrator").format(sql.Identifier(name))
        )
    try:
        with database.connect("admin") as admin:
            assert apply_migrations(admin) == [
                "001_foundation.sql",
                "002_export_reader.sql",
                "003_projection_maintenance.sql",
                "004_resume_review.sql",
                "005_full_reversal.sql",
            ]
        yield database
    finally:
        with cluster.connect("admin") as admin:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.fixture
def golden():
    return json.loads((ROOT / "fixtures/golden/purchase_80.json").read_text())
