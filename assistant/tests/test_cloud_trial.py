from decimal import Decimal

import pytest

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
