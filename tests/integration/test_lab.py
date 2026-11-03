"""Exercise the teaching UI's real HTTP boundary, state, and fixture lifetime."""

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from lab.server import ASSETS, SOURCES, LabServer, source_excerpt
from lab.session import LabSession
from lab.trace import TracedDatabase
from tests.integration.test_demo import ROOT, _containers

pytestmark = pytest.mark.integration


def call(origin, path="/api/state", body=None, token=None, headers=None):
    request = Request(
        origin + path,
        data=None if body is None else json.dumps(body).encode(),
        headers={
            **(
                {"Content-Type": "application/json", "X-Ledger-Lab": token}
                if body is not None
                else {}
            ),
            **(headers or {}),
        },
    )
    try:
        response = urlopen(request, timeout=15)
    except HTTPError as error:
        response = error
    with response:
        raw = response.read()
        return response.status, json.loads(raw) if raw else None


@pytest.fixture
def http_lab(db, golden, tmp_path):
    with LabServer(LabSession(TracedDatabase(db), golden, tmp_path, "a" * 40)) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            yield server
        finally:
            server.shutdown()
            worker.join(timeout=10)


def _customer(state):
    return next(b["booked"] for b in state["balances"] if b["label"] == "customer")


def test_http_twelve_steps_actual_data_sql_diff_and_reset(http_lab):
    origin = http_lab.origin
    _, state = call(origin)
    assert not state["lab"]["account_opened"] and not state["balances"]
    original_workspace = state["workspace"]["id"]
    token = state["token"]
    for index in range(1, 13):
        code, state = call(origin, "/api/action", {"action": "next"}, token)
        assert code == 200, state
        assert state["guide"]["index"] == index
        event = state["last_event"]
        assert event["guide_step"] == index
        assert event["trace"]
        assert all(t["status"] == "ok" and t["code"] for t in event["trace"])
        assert all(
            t["role"] in {"ledger_app", "ledger_reader", "ledger_maintenance", "ledger_migrator"}
            for t in event["trace"]
        )
        assert event["before_state"] is not None and event["after_state"] is not None
        if event["action"] != "drift":
            assert any(t["sql"] == "BEGIN" for t in event["trace"])
            assert any(t["sql"] == "COMMIT" for t in event["trace"])
        else:
            # This owner-only fault is one autocommit UPDATE. Do not invent
            # explicit protocol commands that the client never issued.
            assert any(t["sql"].startswith("UPDATE") for t in event["trace"])
        if index in {2, 3, 4, 5}:
            assert event["http_status"] == {2: 201, 3: 422, 4: 201, 5: 422}[index]
        if index == 6:
            assert _customer(state) == "120"
        if index == 7:
            assert not state["verification"]["report"]["projection_ok"]
        if index == 10:
            assert state["workspace"]["mode"] == "FROZEN"
            assert str(state["workspace"]["projection_epoch"]) == "2"
            assert _customer(state) == "100" and state["repairs"] and state["repair_rows"]
    assert state["verification"]["report"]["healthy"]
    assert state["workspace"]["mode"] == "NORMAL"
    assert len(state["reversal_attempts"]) == 2 and len(state["reversal_effects"]) == 1
    _, state = call(origin, "/api/action", {"action": "reset"}, token)
    assert state["workspace"]["id"] != original_workspace
    assert state["guide"]["index"] == 0 and not state["balances"]
    _, state = call(origin, "/api/action", {"action": "next"}, token)
    assert state["last_event"]["guide_step"] == 1
    assert state["last_event"]["id"] != state["last_event"]["guide_step"]


def test_http_freeplay_replay_conflict_reversal_and_input_guards(http_lab):
    origin, token = http_lab.origin, http_lab.token
    for body in [
        {"action": "opening"},
        {"action": "purchase", "amount_minor": "80", "merchant": "M2"},
    ]:
        status, bought = call(origin, "/api/action", body, token)
        assert status == 200
    original = bought["last_event"]["request"]
    _, replay = call(origin, "/api/action", {"action": "replay_purchase"}, token)
    assert replay["last_event"]["replayed"] and _customer(replay) == "20"
    assert replay["last_event"]["request"] == original
    status, error = call(
        origin, "/api/action", {"action": "conflict_purchase", "amount_minor": "70"}, token
    )
    assert status == 409
    event = error["state"]["last_event"]
    assert any(t["sql"] == "ROLLBACK" for t in event["trace"])
    assert event["before_state"] == event["after_state"]
    for action, expected in [("reversal", 201), ("duplicate_reversal", 422)]:
        _, state = call(origin, "/api/action", {"action": action}, token)
        assert state["last_event"]["http_status"] == expected
    operation_count = len(state["operations"])
    _, replay = call(origin, "/api/action", {"action": "replay_reversal"}, token)
    assert replay["last_event"]["http_status"] == 422 and replay["last_event"]["replayed"]
    assert len(replay["operations"]) == operation_count
    status, _ = call(origin, "/api/action", {"action": "purchase", "amount_minor": 1.5}, token)
    assert status == 400
    assert call(origin, "/api/action", {"action": "next"}, token)[0] == 409
    assert call(origin, "/api/action", {"action": "reset"}, "bad-token")[0] == 403
    assert call(origin, headers={"Host": "example.com"})[0] == 403
    assert call(origin, headers={"Origin": "https://example.com"})[0] == 403
    assert (
        call(origin, "/api/action", {"action": "sql", "query": "DROP DATABASE x"}, token)[0] == 400
    )


def test_source_allowlist_returns_current_source(http_lab):
    actual = source_excerpt("balance_lock")
    assert "def _lock_balances" in actual["text"] and "FOR UPDATE" in actual["text"]
    assert actual["path"] == "src/ledgerops/db/admission.py"
    assert call(http_lab.origin, "/api/source?key=../../conftest.py")[0] == 404
    for key in SOURCES:
        status, source = call(http_lab.origin, "/api/source?key=" + key)
        assert status == 200 and source["text"]
    for path, (_, content_type) in ASSETS.items():
        with urlopen(http_lab.origin + path, timeout=5) as response:
            assert response.status == 200
            assert response.headers["Content-Type"] == content_type
            assert response.read()
    html = (ROOT / "lab/index.html").read_text()
    javascript = (ROOT / "lab/app.js").read_text()
    ids = set(re.findall(r'\bid="([\w-]+)"', html))
    references = set(re.findall(r'\$\("([\w-]+)"\)', javascript))
    assert references - ids == {"source-container"}  # Created by renderReading.


@pytest.mark.parametrize("exit_method", ["shutdown", "ctrl_c"])
def test_make_lab_http_smoke_and_shutdown_cleanup(tmp_path, exit_method):
    before = _containers()
    temporary = tmp_path / "temporary"
    temporary.mkdir()
    log = tmp_path / "lab.log"
    environment = {
        **os.environ,
        "UV_CACHE_DIR": str(ROOT / ".cache/uv"),
        "UV_OFFLINE": "1",
        "UV_NO_SYNC": "1",
        "UV_PROJECT_ENVIRONMENT": sys.prefix,
        "PYTEST_ADDOPTS": "",
        "TMPDIR": str(temporary),
    }
    with log.open("w") as output:
        process = subprocess.Popen(
            ["make", "lab"],
            cwd=ROOT,
            env=environment,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 70
            origin = None
            while time.monotonic() < deadline and process.poll() is None:
                matches = [
                    line.removeprefix("LEDGEROPS_LAB_URL=").rstrip("/")
                    for line in log.read_text().splitlines()
                    if line.startswith("LEDGEROPS_LAB_URL=")
                ]
                if matches:
                    origin = matches[-1]
                    break
                time.sleep(0.1)
            assert origin, log.read_text()
            _, state = call(origin)
            token = state["token"]
            for _ in range(12):
                status, state = call(origin, "/api/action", {"action": "next"}, token)
                assert status == 200, state
            assert state["verification"]["report"]["healthy"]
            if exit_method == "shutdown":
                assert call(origin, "/api/action", {"action": "shutdown"}, token)[0] == 200
            else:
                os.killpg(process.pid, signal.SIGINT)
            return_code = process.wait(timeout=25)
            if exit_method == "shutdown":
                assert return_code == 0, log.read_text()
            else:
                # Ctrl-C also interrupts make/uv in this foreground process
                # group. Its conventional interrupted exit isn't a DB failure;
                # the resource assertions below are the cleanup evidence.
                assert return_code in {0, -signal.SIGINT, 2, 130}, log.read_text()
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGINT)
                process.wait(timeout=25)
    assert _containers() == before
    assert not list(temporary.rglob("ledgerops-lab-*"))


def test_make_lab_docker_absence_fails_explicitly(tmp_path):
    env = {
        **os.environ,
        "UV_OFFLINE": "1",
        "UV_CACHE_DIR": str(ROOT / ".cache/uv"),
        "UV_NO_SYNC": "1",
        "UV_PROJECT_ENVIRONMENT": sys.prefix,
        "PYTEST_ADDOPTS": "",
        "DOCKER_HOST": f"unix://{tmp_path}/absent.sock",
    }
    for name in ("DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_TLS", "DOCKER_CERT_PATH"):
        env.pop(name, None)
    result = subprocess.run(
        ["make", "lab"], cwd=ROOT, env=env, capture_output=True, text=True, timeout=30
    )
    assert result.returncode != 0 and "require Docker" in result.stdout + result.stderr
    assert "LEDGEROPS_LAB_URL=" not in result.stdout
