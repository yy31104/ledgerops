"""Fixed component launcher; private profiles never become Docker env/arguments."""

import json
import os
import re
import sys
from pathlib import Path

from psycopg.conninfo import make_conninfo

from ledgerops.http.config import bounded_file, unique

COMMON = {"LEDGEROPS_RESTORE_MODE"}
ALLOWED = {
    "service": COMMON
    | {
        "LEDGEROPS_SERVICE_DSN",
        "LEDGEROPS_ENV",
        "LEDGEROPS_TLS_TERMINATED",
        "LEDGEROPS_SERVICE_HOSTS",
        "LEDGEROPS_SERVICE_BIND",
        "LEDGEROPS_SERVICE_WORKERS",
        "LEDGEROPS_TRUSTED_PROXIES",
        "LEDGEROPS_API_WRITES_ENABLED",
        "LEDGEROPS_PAYOUT_POLICY_FILE",
    },
    "worker": COMMON
    | {
        "LEDGEROPS_WORKER_DSN",
        "LEDGEROPS_PROVIDER_URL",
        "LEDGEROPS_PROVIDER_TOKEN",
        "LEDGEROPS_PROVIDER_NAMESPACE",
        "LEDGEROPS_PROVIDER_KEYS_FILE",
        "LEDGEROPS_DISPATCH_ENABLED",
        "LEDGEROPS_WORKER_BATCH_SIZE",
        "LEDGEROPS_WORKER_POLL_MS",
        "LEDGEROPS_WORKER_LEASE_SECONDS",
        "LEDGEROPS_PROVIDER_TIMEOUT_MS",
    },
    "provider": {
        "LEDGEROPS_PROVIDER_HMAC_KEY",
        "LEDGEROPS_PROVIDER_TOKEN",
        "LEDGEROPS_PROVIDER_NAMESPACE",
    },
    "bootstrap": {"LEDGEROPS_BOOTSTRAP_STATE_FILE"},
    "backup": {"LEDGEROPS_BACKUP_ADMIN_DSN", "LEDGEROPS_PROVIDER_TOKEN"},
    "reconciler": {"LEDGEROPS_RECON_DSN"},
}
ALLOWED["operator"] = ALLOWED["service"] | {
    "LEDGEROPS_OPERATOR_DSN",
    "LEDGEROPS_OPERATOR_OBJECTS",
    "LEDGEROPS_OPERATOR_NAMESPACE",
    "LEDGEROPS_OPERATOR_KEYS_FILE",
}


def main():
    component, *arguments = sys.argv[1:]
    if component == "init":
        from bootstrap import initialize_files

        initialize_files()
        return 0
    if component not in ALLOWED:
        raise ValueError("UNKNOWN_COMPONENT")
    profile = json.loads(
        bounded_file(Path("/run/ledgerops/profile.json")), object_pairs_hook=unique
    )
    if profile.keys() != {"version", "environment"} or profile["version"] != 1:
        raise ValueError("INVALID_PRIVATE_PROFILE")
    environment = profile["environment"]
    if type(environment) is not dict or not environment.keys() <= ALLOWED[component]:
        raise ValueError("UNKNOWN_PRIVATE_CONFIGURATION")
    if any(type(value) is not str for value in environment.values()):
        raise ValueError("PRIVATE_CONFIGURATION_MUST_BE_TEXT")
    if component == "backup" and arguments[:1] == ["--database"]:
        if (
            len(arguments) < 3
            or re.fullmatch(
                r"(?:postgres|ledgerops|ledgerops_restore_[a-z0-9_]{8,40})", arguments[1]
            )
            is None
        ):
            raise ValueError("KNOWN_ADMINISTRATOR_DATABASE_REQUIRED")
        environment = {
            **environment,
            "LEDGEROPS_BACKUP_ADMIN_DSN": make_conninfo(
                environment["LEDGEROPS_BACKUP_ADMIN_DSN"], dbname=arguments[1]
            ),
        }
        arguments = arguments[2:]
    # Only explicit physical process flags can override safe generated defaults.
    overrides = {
        name: os.environ[name]
        for name in (
            "LEDGEROPS_RESTORE_MODE",
            "LEDGEROPS_API_WRITES_ENABLED",
            "LEDGEROPS_DISPATCH_ENABLED",
        )
        if name in os.environ and name in ALLOWED[component]
    }
    os.environ.clear()
    os.environ.update(
        {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": "/tmp",
            "PYTHONPATH": "/opt",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
        }
    )
    if component in {"operator", "bootstrap", "backup", "reconciler"}:
        os.environ["TMPDIR"] = "/var/lib/ledgerops-scratch"
        import shutil

        if shutil.disk_usage(os.environ["TMPDIR"]).free < 2 * 1024**3:
            raise ValueError("INSUFFICIENT_SCRATCH_FREE_SPACE")
    if Path("/opt/python-compat").is_dir():
        os.environ["LD_LIBRARY_PATH"] = "/usr/local/lib:/opt/python-compat"
    os.environ.update(environment)
    os.environ.update(overrides)
    if component in {"service", "operator"}:
        command = [sys.executable, "-m", "ledgerops.service"]
    elif component == "worker":
        command = [sys.executable, "-m", "ledgerops.worker", *arguments]
    elif component == "provider":
        command = [
            sys.executable,
            "-m",
            "provider_sim",
            "--database",
            "/var/lib/ledgerops-provider/provider.sqlite3",
            "--namespace",
            environment["LEDGEROPS_PROVIDER_NAMESPACE"],
            "--key-id",
            "deploy-v1",
            "--port",
            "8765",
        ]
    elif component == "reconciler":
        command = [sys.executable, "-m", "ledgerops.recon_cli", *arguments]
    elif component == "bootstrap":
        command = [sys.executable, "/opt/ledgerops-ops/bootstrap.py", *arguments]
    else:
        command = [sys.executable, "-m", "ledgerops.backup_cli", *arguments]
    os.execv(command[0], command)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, TypeError, KeyError):
        print('{"event":"launcher_configuration_invalid"}', file=sys.stderr)
        raise SystemExit(2) from None
