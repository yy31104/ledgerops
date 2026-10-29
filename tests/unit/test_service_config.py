"""Production config never silently downgrades or invents an identity/provider policy."""

import json

import pytest

from ledgerops.http.config import ServiceConfig

BASE = {
    "LEDGEROPS_SERVICE_DSN": (
        "host=localhost dbname=ledger user=ledger_service password=never-print-me"
    ),
    "LEDGEROPS_TLS_TERMINATED": "true",
}


def test_safe_defaults_disable_all_financial_writes_and_hide_credentials():
    config = ServiceConfig.from_env(BASE)
    assert config.production and config.restore_mode and not config.writes_enabled
    assert config.workers == 2 and config.bind == "127.0.0.1:8080"
    assert config.payout_policies == {} and "never-print-me" not in repr(config)


@pytest.mark.parametrize(
    "overrides",
    [
        {"LEDGEROPS_TLS_TERMINATED": "false"},
        {"LEDGEROPS_SERVICE_WORKERS": "0"},
        {"LEDGEROPS_SERVICE_WORKERS": "8"},
        {"LEDGEROPS_SERVICE_HOSTS": "*"},
        {"LEDGEROPS_SERVICE_BIND": "0.0.0.0:8080", "LEDGEROPS_ENV": "development"},
        {"LEDGEROPS_TRUSTED_PROXIES": "*"},
        {"LEDGEROPS_RESTORE_MODE": "0"},
        {"LEDGEROPS_SERVICE_DSN": ""},
        {"LEDGEROPS_SERVICE_BIND": "127.0.0.1:99999"},
    ],
)
def test_invalid_or_downgraded_configuration_is_rejected(overrides):
    with pytest.raises(ValueError):
        ServiceConfig.from_env({**BASE, **overrides})


def test_policy_file_is_owner_only_and_explicit_version(tmp_path):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({"version": 1, "workspaces": []}))
    path.chmod(0o644)
    with pytest.raises(ValueError):
        ServiceConfig.from_env({**BASE, "LEDGEROPS_PAYOUT_POLICY_FILE": str(path)})
    path.chmod(0o600)
    assert (
        ServiceConfig.from_env({**BASE, "LEDGEROPS_PAYOUT_POLICY_FILE": str(path)}).payout_policies
        == {}
    )
    path.write_text(json.dumps({"version": True, "workspaces": []}))
    with pytest.raises(ValueError):
        ServiceConfig.from_env({**BASE, "LEDGEROPS_PAYOUT_POLICY_FILE": str(path)})


@pytest.mark.parametrize(
    "missing",
    [
        "LEDGEROPS_OPERATOR_DSN",
        "LEDGEROPS_OPERATOR_OBJECTS",
        "LEDGEROPS_OPERATOR_NAMESPACE",
        "LEDGEROPS_OPERATOR_KEYS_FILE",
    ],
)
def test_operator_capability_never_starts_with_partial_configuration(tmp_path, missing):
    values = {
        "LEDGEROPS_OPERATOR_DSN": BASE["LEDGEROPS_SERVICE_DSN"],
        "LEDGEROPS_OPERATOR_OBJECTS": str(tmp_path),
        "LEDGEROPS_OPERATOR_NAMESPACE": "10000000-0000-0000-0000-000000000001",
        "LEDGEROPS_OPERATOR_KEYS_FILE": str(tmp_path / "keys.json"),
    }
    del values[missing]
    with pytest.raises(ValueError, match="COMPLETE_OPERATOR_CONFIGURATION_REQUIRED"):
        ServiceConfig.from_env({**BASE, **values})
