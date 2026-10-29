"""Owned actual Gunicorn process and real HTTP requests, never a WSGI test client."""

import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from threading import Event

from psycopg.conninfo import make_conninfo

ROOT = Path(__file__).resolve().parents[2]


class ProcessService:
    def __init__(self, db, directory, **overrides):
        directory.mkdir()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.db = db
        self.stdout = directory / "stdout.log"
        self.stderr = directory / "stderr.log"
        self.outputs = [self.stdout.open("wb"), self.stderr.open("wb")]
        self.dsn = make_conninfo(
            host="127.0.0.1",
            port=db.port,
            dbname=db.name,
            user="ledger_service",
            password=db.service_password,
        )
        env = {
            **os.environ,
            "LEDGEROPS_SERVICE_DSN": self.dsn,
            "LEDGEROPS_ENV": "development",
            "LEDGEROPS_SERVICE_BIND": f"127.0.0.1:{self.port}",
            "LEDGEROPS_SERVICE_WORKERS": "2",
            "LEDGEROPS_SERVICE_HOSTS": "127.0.0.1,localhost",
            "LEDGEROPS_RESTORE_MODE": "false",
            "LEDGEROPS_API_WRITES_ENABLED": "true",
            **overrides,
        }
        self.environment = env
        self.process = subprocess.Popen(
            [sys.executable, "-m", "ledgerops.service"],
            cwd=ROOT,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=self.outputs[0],
            stderr=self.outputs[1],
            start_new_session=True,
        )
        try:
            self._ready()
        except BaseException:
            self.close()
            raise

    def _ready(self):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise AssertionError(self.logs())
            try:
                initialized = [
                    json.loads(line) for line in self.stdout.read_text().splitlines() if line
                ]
                ready = {
                    row["pid"] for row in initialized if row.get("event") == "http_worker_ready"
                }
                if len(ready) == 2 and self.request("GET", "/health/live")[0] == 200:
                    return
            except (OSError, http.client.HTTPException):
                pass
            Event().wait(0.02)
        raise AssertionError("Actual Gunicorn process did not start: " + self.logs())

    def request(self, method, path, body=None, *, token=None, headers=None):
        values = {**(headers or {})}
        if token is not None:
            values["Authorization"] = "Bearer " + token
        if isinstance(body, dict):
            body = json.dumps(body).encode()
            values.setdefault("Content-Type", "application/json")
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=12)
        try:
            conn.request(method, path, body, values)
            response = conn.getresponse()
            return response.status, response.read(), dict(response.getheaders())
        finally:
            conn.close()

    def logs(self):
        return self.stdout.read_text() + self.stderr.read_text()

    def close(self):
        try:
            if self.process.poll() is None:
                self.process.send_signal(signal.SIGTERM)
                try:
                    self.process.wait(timeout=12)
                except subprocess.TimeoutExpired:
                    os.killpg(self.process.pid, signal.SIGKILL)
                    self.process.wait(timeout=5)
                    raise AssertionError("Gunicorn did not stop gracefully") from None
            assert self.dsn not in self.logs() and self.db.service_password not in self.logs()
            with self.db.connect("admin") as conn:
                deadline = time.monotonic() + 3
                while conn.execute(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname=%s "
                    "AND application_name IN ('ledgerops-api','ledgerops-callback',"
                    "'ledgerops-operator')",
                    (self.db.name,),
                ).fetchone() != (0,):
                    assert time.monotonic() < deadline, (
                        "HTTP process left an owned database connection"
                    )
                    Event().wait(0.01)
        finally:
            for stream in self.outputs:
                stream.close()
