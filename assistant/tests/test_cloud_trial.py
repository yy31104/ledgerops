import json
import socket
import ssl
import urllib.error
from decimal import Decimal

import pytest

from scripts import cloud_trial
from scripts.cloud_trial import cost_record, origin, smoke


@pytest.mark.parametrize(
    "value",
    [
        "http://example.com",
        "https://user:pass@example.com",
        "https://example.com/path",
        "https://example.com?token=x",
    ],
)
def test_only_https_origins(value):
    with pytest.raises(ValueError):
        origin(value)


def test_smoke_is_get_only_and_never_records_token():
    calls = []

    def request(url, token):
        calls.append((url, token))
        if url.endswith("/v1/session") and token is None:
            return 401
        return 404 if url.endswith("/v1/demo") else 200

    result = smoke(
        "https://api.example.com", "https://assistant.example.com", "secret", request=request
    )
    assert result["passed"]
    assert len(calls) == 6
    assert [token for _, token in calls] == ["secret", None, None, None, "secret", None]
    assert "secret" not in str(result)


def test_smoke_records_failure_without_hiding_it():
    result = smoke(
        "https://api.example.com", "https://assistant.example.com", "secret", request=lambda *_: 500
    )
    assert not result["passed"]
    assert all(check["status"] == 500 for check in result["checks"])


@pytest.mark.parametrize(
    ("failure", "category"),
    [
        (urllib.error.URLError(socket.gaierror("private DNS details")), "dns"),
        (urllib.error.URLError(ssl.SSLError("private TLS details")), "tls"),
        (urllib.error.URLError(TimeoutError("private timeout details")), "timeout"),
        (TimeoutError("private timeout details"), "timeout"),
        (urllib.error.URLError("private connection details"), "transport"),
        (ConnectionError("private connection details"), "transport"),
        (RuntimeError("private request details"), "request"),
    ],
)
def test_transport_failure_preserves_partial_evidence(failure, category):
    calls = []

    def request(url, token):
        calls.append((url, token))
        if len(calls) == 3:
            raise failure
        return 200

    result = smoke(
        "https://api.example.com", "https://assistant.example.com", "secret", request=request
    )
    assert not result["passed"] and not result["healthy"] and not result["completed"]
    assert result["expected_checks"] == 6
    assert len(result["checks"]) == len(calls) == 3
    assert all(check["passed"] for check in result["checks"][:2])
    failed = result["checks"][2]
    assert failed["name"] == "assistant_ready"
    assert failed["status"] is None and not failed["passed"]
    assert failed["error_category"] == category
    assert failed["latency_ms"] >= 0
    assert not any(value in json.dumps(result) for value in ("private", "secret", "https://"))


def test_cli_writes_transport_failure_and_exits_nonzero_without_leaks(
    tmp_path, monkeypatch, capsys
):
    output = tmp_path / "evidence.json"
    token = "private-caller-token"
    calls = []

    def request(url, credential):
        calls.append((url, credential))
        if len(calls) == 3:
            raise urllib.error.URLError(TimeoutError(f"{url} Authorization: Bearer {token}"))
        return 200

    monkeypatch.setenv("LEDGEROPS_OPERATOR_TOKEN", token)
    monkeypatch.setattr(
        cloud_trial,
        "smoke",
        lambda api, assistant, caller: smoke(api, assistant, caller, request=request),
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "cloud_trial.py",
            "smoke",
            "--api-origin",
            "https://private-api.example.com",
            "--assistant-origin",
            "https://private-assistant.example.com",
            "--output",
            str(output),
        ],
    )
    with pytest.raises(SystemExit) as stopped:
        cloud_trial.main()
    assert stopped.value.code == 1
    contents = output.read_text()
    result = json.loads(contents)
    assert len(result["checks"]) == 3
    assert result["checks"][2]["error_category"] == "timeout"
    assert not result["healthy"]
    assert not any(value in contents for value in (token, "private-", "Authorization", "https://"))
    with pytest.raises(FileExistsError):
        cloud_trial.main()
    assert len(calls) == 3
    assert output.read_text() == contents
    assert capsys.readouterr() == ("", "")


def test_transport_failure_on_last_check_is_not_completed():
    def request(url, token):
        if url.endswith("/v1/demo"):
            raise TimeoutError("private response details")
        return 401 if url.endswith("/v1/session") and token is None else 200

    result = smoke(
        "https://api.example.com", "https://assistant.example.com", "secret", request=request
    )
    assert len(result["checks"]) == 6
    assert not result["completed"] and not result["healthy"]


def test_cost_evidence_hashes_export_without_copying_it(tmp_path):
    export = tmp_path / "billing.csv"
    export.write_text("account,amount\nprivate-account,1.23\n")
    result = cost_record("1.23", "usd", export, "rg-trial")
    assert Decimal(result["actual_cost"]) == Decimal("1.23")
    assert result["currency"] == "USD"
    assert len(result["source_sha256"]) == 64
    assert "private-account" not in str(result)


@pytest.mark.parametrize("amount", ["-1", "NaN", "Infinity", "invalid"])
def test_cost_rejects_invalid_totals(tmp_path, amount):
    with pytest.raises(ValueError, match="nonnegative"):
        cost_record(amount, "USD", tmp_path / "unused", "rg-trial")
