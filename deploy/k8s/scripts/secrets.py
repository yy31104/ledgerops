"""Generate random in-cluster Secrets for the kind demo (mirrors ops/linux/bootstrap.py).

Nothing is written to Git: values go straight to `kubectl apply -f -`. Existing Secrets are kept,
so re-running never rotates database passwords underneath a populated PostgreSQL volume.
"""

import base64
import json
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "ops" / "linux"))
from bootstrap import LOGINS  # noqa: E402  (single source of truth for login roles)

NS = "ledgerops"
HOSTS = "localhost,127.0.0.1,api,api.ledgerops.svc,api.ledgerops.svc.cluster.local"
IDS = (
    "deployment_id",
    "namespace",
    "workspace_id",
    "system_actor",
    "customer_actor",
    "merchant_actor",
    "maker_actor",
    "checker_actor",
    "admin_actor",
    "customer_account",
    "merchant_account",
    "clearing_account",
    "beneficiary_id",
    "customer_client",
    "merchant_client",
    "maker_client",
    "checker_client",
)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def profile(environment):
    return canonical({"version": 1, "environment": environment})


def dsn(state, role):
    password = state["passwords"][role]
    return (
        f"host=postgres port=5432 dbname=ledgerops user={role} "
        f"password={password} connect_timeout=3"
    )


def secret(name, files):
    data = {key: base64.b64encode(value).decode() for key, value in files.items()}
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "type": "Opaque",
        "metadata": {"name": name, "namespace": NS},
        "data": data,
    }


def exists(name):
    result = subprocess.run(
        ["kubectl", "-n", NS, "get", "secret", name], capture_output=True, check=False
    )
    return result.returncode == 0


def openssl(directory, *arguments):
    subprocess.run(["openssl", *arguments], cwd=directory, check=True, capture_output=True)


def certificates():
    """Throwaway 30-day CA + server certificate for in-cluster TLS (never leaves the cluster)."""
    with tempfile.TemporaryDirectory() as directory:
        openssl(
            directory, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "30",
            "-subj", "/CN=ledgerops-kind-ca", "-keyout", "ca.key", "-out", "ca.crt",
        )  # fmt: skip
        openssl(
            directory, "req", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=api.ledgerops.svc",
            "-keyout", "tls.key", "-out", "tls.csr",
        )  # fmt: skip
        Path(directory, "ext").write_text(
            "subjectAltName=DNS:api.ledgerops.svc,DNS:api,DNS:localhost\n"
        )
        openssl(
            directory, "x509", "-req", "-in", "tls.csr", "-CA", "ca.crt", "-CAkey", "ca.key",
            "-CAcreateserial", "-days", "30", "-extfile", "ext", "-out", "tls.crt",
        )  # fmt: skip
        names = ("ca.crt", "tls.crt", "tls.key")
        return {name: Path(directory, name).read_bytes() for name in names}


def main():
    if exists("ledgerops-bootstrap"):
        print('{"event":"k8s_secrets_already_present"}')
        return
    state = {name: str(uuid4()) for name in IDS}
    state["passwords"] = {name: secrets.token_urlsafe(32) for name in (*LOGINS, "postgres")}
    state["provider_hmac"] = secrets.token_hex(32)
    state["provider_token"] = secrets.token_urlsafe(32)
    policy = {
        "version": 1,
        "workspaces": [
            {
                "workspace_id": state["workspace_id"],
                "provider_namespace": state["namespace"],
                "fixed_fee_minor": 2,
                "dispatch_ttl_seconds": 60,
                "beneficiary_owners": {state["beneficiary_id"]: state["merchant_actor"]},
            }
        ],
    }
    api = {
        "LEDGEROPS_ENV": "production",
        "LEDGEROPS_TLS_TERMINATED": "true",
        "LEDGEROPS_SERVICE_HOSTS": HOSTS,
        "LEDGEROPS_SERVICE_BIND": "0.0.0.0:8080",
        "LEDGEROPS_SERVICE_WORKERS": "2",
        # Safe defaults; the Deployment sets the physical override flags explicitly.
        "LEDGEROPS_RESTORE_MODE": "true",
        "LEDGEROPS_API_WRITES_ENABLED": "false",
        "LEDGEROPS_TRUSTED_PROXIES": "127.0.0.1",
        "LEDGEROPS_PAYOUT_POLICY_FILE": "/run/ledgerops/payout-policy.json",
        "LEDGEROPS_SERVICE_DSN": dsn(state, "ledger_service"),
    }
    worker = {
        "LEDGEROPS_WORKER_DSN": dsn(state, "ledger_dispatcher"),
        "LEDGEROPS_PROVIDER_URL": "http://127.0.0.1:8765",
        "LEDGEROPS_PROVIDER_TOKEN": state["provider_token"],
        "LEDGEROPS_PROVIDER_NAMESPACE": state["namespace"],
        "LEDGEROPS_PROVIDER_KEYS_FILE": "/run/ledgerops/provider-keys.json",
        "LEDGEROPS_RESTORE_MODE": "true",
        "LEDGEROPS_DISPATCH_ENABLED": "false",
        "LEDGEROPS_WORKER_BATCH_SIZE": "10",
        "LEDGEROPS_WORKER_POLL_MS": "500",
        "LEDGEROPS_WORKER_LEASE_SECONDS": "30",
        "LEDGEROPS_PROVIDER_TIMEOUT_MS": "5000",
    }
    provider = {
        "LEDGEROPS_PROVIDER_HMAC_KEY": state["provider_hmac"],
        "LEDGEROPS_PROVIDER_TOKEN": state["provider_token"],
        "LEDGEROPS_PROVIDER_NAMESPACE": state["namespace"],
    }
    bootstrap = {"LEDGEROPS_BOOTSTRAP_STATE_FILE": "/run/ledgerops/state.json"}
    keys = canonical({"deploy-v1": state["provider_hmac"]})
    certs = certificates()
    items = [
        secret("ledgerops-postgres", {"password": state["passwords"]["postgres"].encode()}),
        secret(
            "ledgerops-api",
            {"profile.json": profile(api), "payout-policy.json": canonical(policy)},
        ),
        secret("ledgerops-worker", {"profile.json": profile(worker), "provider-keys.json": keys}),
        secret("ledgerops-provider", {"profile.json": profile(provider)}),
        secret("ledgerops-api-tls", {"tls.crt": certs["tls.crt"], "tls.key": certs["tls.key"]}),
        secret("ledgerops-api-ca", {"ca.crt": certs["ca.crt"]}),
        secret("ledgerops-grafana", {"admin-password": secrets.token_urlsafe(24).encode()}),
        # Last: its presence marks the set complete (same rule as ops/linux/bootstrap.py).
        secret(
            "ledgerops-bootstrap",
            {"profile.json": profile(bootstrap), "state.json": canonical(state)},
        ),
    ]
    manifest = canonical({"apiVersion": "v1", "kind": "List", "items": items})
    subprocess.run(["kubectl", "apply", "-f", "-"], input=manifest, check=True, capture_output=True)
    print(json.dumps({"event": "k8s_secrets_created", "count": len(items)}))


if __name__ == "__main__":
    main()
