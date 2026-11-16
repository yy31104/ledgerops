"""Isolated fake-model Compose acceptance: SSE, restart, same draft, no submission."""

import json
import os
import secrets
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


def main():
    with socket.socket() as check:
        try:
            check.bind(("127.0.0.1", 8088))
        except OSError:
            raise SystemExit(
                "Port 8088 is in use; stop its owner before running acceptance."
            ) from None
    project = "ledgerops-acceptance-" + uuid4().hex[:10]
    token = "lo1_" + secrets.token_urlsafe(32)
    opener = build_opener(ProxyHandler({}))

    def request(path, data=None, *, stream=False):
        raw = None if data is None else json.dumps(data).encode()
        req = Request(
            "http://127.0.0.1:8088" + path,
            data=raw,
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        )
        with opener.open(req, timeout=30) as response:
            body = response.read().decode()
        return body if stream else json.loads(body)

    with tempfile.TemporaryDirectory(prefix="ledgerops-acceptance-") as directory:
        env_file = Path(directory) / ".env"
        env_file.write_text(
            f"ASSISTANT_OWNER_PASSWORD={secrets.token_urlsafe(32)}\n"
            f"ASSISTANT_RUNTIME_PASSWORD={secrets.token_urlsafe(32)}\n"
            f"LEDGEROPS_ASSISTANT_DEMO_TOKEN={token}\n"
        )
        env_file.chmod(0o600)
        command = [
            "docker",
            "compose",
            "-p",
            project,
            "--env-file",
            str(env_file),
            "-f",
            str(ROOT / "compose.yml"),
        ]
        env = {**os.environ, "LEDGEROPS_ASSISTANT_EMBEDDER": "hashing"}

        def compose(*args):
            subprocess.run([*command, *args], check=True, env=env, timeout=600)

        try:
            compose("up", "--build", "-d", "--wait", "--wait-timeout", "240")
            assert request("/health/ready")["status"] == "ready"
            stream = request(
                "/v1/assist",
                {
                    "question": "Does a timeout release a hold?",
                    "request_id": str(uuid4()),
                    "provider": "fake",
                },
                stream=True,
            )
            results = [
                json.loads(frame.split("data: ", 1)[1])
                for frame in stream.split("\n\n")
                if frame.startswith("event: result\n")
            ]
            assert len(results) == 1 and "usage" in results[0] and results[0]["duration_ms"] > 0
            thread = str(uuid4())
            path = "/v1/drafts/" + thread
            draft = request(
                path,
                {
                    "question": "Inspect original terminal evidence before drafting.",
                    "request_id": str(uuid4()),
                    "provider": "fake",
                    "intent": {
                        "payout_id": "40000000-0000-4000-8000-000000000006",
                        "observation_id": "40000000-0000-4000-8000-000000000007",
                        "evidence_run_id": "40000000-0000-4000-8000-000000000008",
                        "reason": "Verify repaired local blocker before console submission.",
                        "ttl_seconds": 900,
                    },
                },
            )
            assert draft["status"] == "awaiting_review", draft.get("reason")
            before = draft["draft"]
            compose("restart", "api")
            for _ in range(60):
                try:
                    if request("/health/ready")["status"] == "ready":
                        break
                except OSError:
                    pass
                time.sleep(0.5)
            else:
                raise RuntimeError("assistant did not recover after restart")
            restored = request(path)
            assert restored["draft"] == before
            decision = {"continue_review": True, "draft_sha256": before["draft_sha256"]}
            handed = request(path + "/review", decision)
            repeated = request(path + "/review", decision)
            assert handed["status"] == repeated["status"] == "handoff_ready"
            assert handed["draft"] == repeated["draft"] == before
            # This synthetic upstream implements GET only. No operator POST is sent.
            print(
                "PASS: SSE usage/latency; restart retained identical draft; "
                "repeat review retained one action ID."
            )
            print(
                "Fake generation and hashing retrieval verify plumbing, not answer quality. "
                "No action submitted."
            )
        finally:
            compose("down", "--volumes", "--remove-orphans")


if __name__ == "__main__":
    main()
