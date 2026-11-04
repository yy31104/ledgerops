"""Actual local Linux/TLS acceptance for an explicitly seeded disposable stack."""

import argparse
import http.client
import json
import ssl
import stat
import subprocess
import tempfile
import time
from pathlib import Path
from uuid import uuid4

from stack import FailureDiagnostics, Stack, diagnostic_phase, protected_write, run


class AcceptanceFailure(AssertionError):
    """An internal, fixed acceptance code suitable for a public result."""


def require(condition, code):
    if not condition:
        raise AcceptanceFailure(code)


def eventually(call, predicate, code, *, seconds=20):
    deadline = time.monotonic() + seconds
    while True:
        try:
            value = call()
            if predicate(value):
                return value
        except (OSError, http.client.HTTPException):
            pass
        require(time.monotonic() < deadline, code)
        time.sleep(0.05)  # Wait for a bounded observable readiness/result predicate.


class Acceptance:
    def __init__(self, stack, directory):
        self.stack, self.directory, self.checks = stack, Path(directory), []
        self.memory_peaks = {}
        self.directory.mkdir(parents=True, exist_ok=True)
        self.credentials_file, self.ca_file = (
            self.directory / "credentials.json",
            self.directory / "ca.crt",
        )
        diagnostic_phase("startup.copy_bootstrap_credentials")
        stack.copy_volume_file("bootstrap-secrets", "acceptance.json", self.credentials_file)
        diagnostic_phase("startup.wait_proxy_ca")

        def ca_exists():
            try:
                stack.compose(
                    "exec",
                    "--no-TTY",
                    "proxy",
                    "sh",
                    "-c",
                    "test -s /data/caddy/pki/authorities/local/root.crt",
                    capture=True,
                    timeout=5,
                )
                return True
            except subprocess.CalledProcessError:
                return False

        eventually(ca_exists, bool, "PROXY_CA_NOT_READY")
        diagnostic_phase("startup.copy_proxy_ca")
        stack.copy_volume_file("caddy-data", "caddy/pki/authorities/local/root.crt", self.ca_file)
        self.credentials = json.loads(self.credentials_file.read_bytes())
        self.ssl_context = ssl.create_default_context(cafile=str(self.ca_file))
        self.port = int(stack.state["environment"]["LEDGEROPS_HTTPS_PORT"])
        self.operator_port = int(stack.state["environment"]["LEDGEROPS_OPERATOR_PORT"])
        for name, operator in (("api", False), ("operator", True)):
            diagnostic_phase("startup.wait_" + name)

            def live(op=operator):
                try:
                    return self.request("/health/live", operator=op)[0] == 200
                except ValueError:
                    return False  # A transient non-JSON upstream response is not ready.

            eventually(live, bool, name.upper() + "_NOT_LIVE")

    def passed(self, name, started=None):
        value = {"check": name, "passed": True}
        if started is not None:
            value["elapsed_seconds"] = round(time.monotonic() - started, 3)
        self.checks.append(value)

    def request(self, path, *, token=None, payload=None, key=None, operator=False, method=None):
        conn = http.client.HTTPSConnection(
            "localhost",
            self.operator_port if operator else self.port,
            context=self.ssl_context,
            timeout=10,
        )
        headers = {"Connection": "close"}
        if token:
            headers["Authorization"] = "Bearer " + token
        if key:
            headers["Idempotency-Key"] = key
        raw = None if payload is None else json.dumps(payload).encode()
        if raw is not None:
            headers["Content-Type"] = "application/json"
        try:
            conn.request(method or ("GET" if raw is None else "POST"), path, raw, headers)
            response = conn.getresponse()
            content = response.read(1048577)
            require(len(content) <= 1048576, "BOUNDED_HTTP_RESPONSE")
            if response.status >= 500 and not content:
                return response.status, {"code": "UPSTREAM_UNAVAILABLE"}
            return response.status, json.loads(content)
        finally:
            conn.close()

    def info(self, service):
        identifier = self.stack.compose(
            "ps", "--all", "--quiet", service, capture=True
        ).stdout.strip()
        require(bool(identifier), "OWNED_SERVICE_CONTAINER_EXISTS")
        return json.loads(run(["docker", "inspect", identifier], capture=True).stdout)[0]

    def exec(self, service, code):
        result = self.stack.compose("exec", "--no-TTY", service, "python", "-c", code, capture=True)
        return json.loads(result.stdout)

    def runtime(self):
        code = """import json,os,platform,sys,ssl,sqlite3,psycopg,importlib.metadata
+from pathlib import Path
+status=Path('/proc/self/status').read_text()
+profile=Path('/run/ledgerops/profile.json').stat()
+blocked=False
+try: Path('/opt/forbidden-write').write_text('x')
+except OSError: blocked=True
+print(json.dumps({'uid':os.getuid(),'gid':os.getgid(),'system':platform.system(),
+ 'python':platform.python_version(),'psycopg':psycopg.__version__,
+ 'gunicorn':importlib.metadata.version('gunicorn'),'ssl':ssl.OPENSSL_VERSION,
+ 'profile_uid':profile.st_uid,'profile_mode':profile.st_mode & 0o777,
+ 'readonly':blocked,'caps_zero':'CapEff:\\t0000000000000000' in status,
+ 'no_new_privs':'NoNewPrivs:\\t1' in status,
+ 'installed':str(Path(__import__('ledgerops').__file__)),
+ 'host_source_absent':not any(Path(p).exists() for p in ('/build','/workspace','/tests','/lab'))}))
+""".replace("\n+", "\n")
        facts = self.exec("api", code)
        require(
            facts["uid"] == facts["gid"] == facts["profile_uid"] == 10001, "NONROOT_OWNED_SECRET"
        )
        require(facts["profile_mode"] == 0o600, "PRIVATE_PROFILE_0600")
        require(
            facts["system"] == "Linux" and facts["python"] == "3.12.15",
            "ACTUAL_PINNED_LINUX_PYTHON",
        )
        require(
            facts["readonly"] and facts["caps_zero"] and facts["no_new_privs"],
            "RUNTIME_KERNEL_RESTRICTIONS",
        )
        require(
            facts["host_source_absent"] and "site-packages" in facts["installed"],
            "WHEEL_WITHOUT_HOST_SOURCE",
        )
        for name in ("api", "operator", "provider", "postgres", "proxy"):
            info = self.info(name)
            require(info["HostConfig"]["ReadonlyRootfs"], "ALL_LONG_RUNNING_ROOTFS_READONLY")
            require(
                info["HostConfig"]["PidsLimit"] > 0 and info["HostConfig"]["Memory"] > 0,
                "FINITE_RUNTIME_RESOURCES",
            )
            require("ALL" in info["HostConfig"]["CapDrop"], "DROP_ALL_CAPABILITIES")
            require(
                not any(mount["Type"] == "bind" for mount in info["Mounts"]),
                "NO_HOST_REPOSITORY_MOUNTS",
            )
            require(
                not any(
                    "DSN=" in item or "TOKEN=" in item or "HMAC_KEY=" in item
                    for item in info["Config"]["Env"]
                ),
                "NO_DOCKER_ENV_SECRETS",
            )
        postgres = self.info("postgres")
        require(
            postgres["Config"]["User"] == "999:999" and not postgres["HostConfig"]["PortBindings"],
            "NONROOT_UNPUBLISHED_DATABASE",
        )
        self.passed("linux_nonroot_wheel_secrets_caps_readonly_resources")
        return facts

    def safe_defaults(self):
        customer = self.credentials["tokens"]["customer"]
        require(self.request("/health/live")[0] == 200, "DEFAULT_LIVE")
        require(self.request("/health/ready")[0] == 503, "DEFAULT_RESTORE_NOT_READY")
        status, identity = self.request("/v1/me", token=customer)
        require(
            status == 200 and not identity["operator_controls_available"],
            "PUBLIC_API_NO_OPERATOR_CREDENTIAL",
        )
        status, operator_identity = self.request("/v1/me", token=customer, operator=True)
        require(
            status == 200 and operator_identity["operator_controls_available"],
            "SEPARATE_PRIVATE_OPERATOR_ENDPOINT",
        )
        require(
            self.request("/v1/purchases", token=customer, payload={}, key="restore-block")[0]
            == 503,
            "PHYSICAL_DEFAULT_BLOCKS_FINANCIAL_WRITES",
        )
        checker = self.credentials["tokens"]["checker"]
        status, _ = self.request(
            "/v1/operator/actions/" + str(uuid4()), token=checker, operator=True
        )
        require(status == 404, "READONLY_OBJECT_STORE_CONSTRUCTOR_AND_PRIVATE_ROLE")
        conn = http.client.HTTPSConnection("localhost", self.port, timeout=5)
        try:
            try:
                conn.request("GET", "/health/live")
                raise AssertionError("UNTRUSTED_LOCAL_CA_MUST_FAIL")
            except ssl.SSLCertVerificationError:
                pass
        finally:
            conn.close()
        self.passed("explicit_ca_verified_tls_restore_defaults_separate_operator")

    def business(self):
        self.stack.compose("up", "--detach", "--force-recreate", "api", "operator", live=True)
        eventually(
            lambda: self.request("/health/ready"), lambda value: value[0] == 200, "LIVE_API_READY"
        )
        ids, tokens = self.credentials, self.credentials["tokens"]
        purchase = {
            "operation_id": str(uuid4()),
            "customer_account_id": ids["customer_account"],
            "merchant_account_id": ids["merchant_account"],
            "amount_minor": "80",
            "currency": "PLN",
        }
        key = "linux-purchase-" + uuid4().hex
        original = self.request(
            "/v1/purchases", token=tokens["customer"], payload=purchase, key=key
        )
        replay = self.request("/v1/purchases", token=tokens["customer"], payload=purchase, key=key)
        require(original[0] == 201 and replay == original, "TLS_PURCHASE_REPLAY_SAME_RESULT")
        require(
            self.request(
                "/v1/purchases",
                token=tokens["customer"],
                payload={**purchase, "amount_minor": "79"},
                key=key,
            )[0]
            == 409,
            "TLS_IDENTITY_CHANGED_CONTENT_CONFLICT",
        )
        payout = {
            "operation_id": str(uuid4()),
            "merchant_account_id": ids["merchant_account"],
            "beneficiary_id": ids["beneficiary_id"],
            "provider_namespace": ids["namespace"],
            "principal_minor": "60",
            "fee_minor": "2",
            "currency": "PLN",
            "contract_version": 1,
        }
        require(
            self.request(
                "/v1/payouts",
                token=tokens["merchant"],
                payload=payout,
                key="linux-payout-" + uuid4().hex,
            )[0]
            == 202,
            "TLS_PAYOUT_ACCEPTED",
        )
        status, accounts = self.request("/v1/accounts", token=tokens["merchant"])
        account = accounts["accounts"][0]
        require(
            status == 200
            and (account["booked"], account["reserved"], account["available"])
            == ("80", "62", "18"),
            "ACTUAL_HOLD62_BEFORE_DISPATCH",
        )
        self.stack.compose("up", "--detach", "worker", live=True)
        result = eventually(
            lambda: self.request("/v1/payouts/" + payout["operation_id"], token=tokens["merchant"]),
            lambda value: value[0] == 200 and value[1]["knowledge"] == "SUCCEEDED",
            "ACTUAL_PROVIDER_WORKER_TERMINAL",
            seconds=30,
        )
        require(result[1]["reservation_state"] == "CONSUMED", "HOLD_CONSUMED")
        status, accounts = self.request("/v1/accounts", token=tokens["merchant"])
        account = accounts["accounts"][0]
        require(
            (account["booked"], account["reserved"], account["available"]) == ("18", "0", "18"),
            "ACTUAL_SETTLED62_BALANCE18",
        )
        command = (
            "import json,sqlite3; "
            "c=sqlite3.connect('file:/var/lib/ledgerops-provider/provider.sqlite3?mode=ro',"
            "uri=True); "
            "print(json.dumps({'executions':"
            "c.execute('SELECT count(*) FROM executions').fetchone()[0],"
            "'events':c.execute('SELECT count(*) FROM events').fetchone()[0]}))"
        )
        require(
            self.exec("provider", command) == {"executions": 1, "events": 1},
            "ONE_INDEPENDENT_PROVIDER_EFFECT",
        )
        namespaces = [
            self.exec(name, "import json,os; print(json.dumps(os.readlink('/proc/self/ns/net')))")
            for name in ("provider", "worker")
        ]
        require(namespaces[0] == namespaces[1], "WORKER_PROVIDER_SHARE_LOOPBACK_NAMESPACE")
        self.stack.compose("stop", "worker")
        self.stack.compose("up", "--detach", "--force-recreate", "provider", "worker", live=True)
        eventually(
            lambda: self.exec("provider", command),
            lambda value: value == {"executions": 1, "events": 1},
            "PROVIDER_STATE_SURVIVES_RESTART",
        )
        require(
            self.request("/v1/payouts/" + payout["operation_id"], token=tokens["merchant"])[1][
                "knowledge"
            ]
            == "SUCCEEDED",
            "TERMINAL_RESULT_SURVIVES_RESTART",
        )
        inspected = self.stack.compose(
            "run", "--rm", "bootstrap", "bootstrap", "inspect", capture=True
        )
        inspection = json.loads(inspected.stdout.splitlines()[-1])
        require(inspection["report"]["healthy"], "INDEPENDENT_FINANCIAL_EXPORT_A_B")
        self.passed(
            "tls_purchase_replay_conflict_hold_worker_loopback_one_effect_restart_independent_fold"
        )

    def database_outage(self):
        self.stack.compose("stop", "postgres")
        require(self.request("/health/live")[0] == 200, "DB_OUTAGE_LIVE_REMAINS200")
        require(self.request("/health/ready")[0] == 503, "DB_OUTAGE_NOT_READY")
        require(self.info("api")["State"]["Running"], "DB_OUTAGE_NO_LIVENESS_RESTART_LOOP")
        self.stack.compose("start", "postgres")
        eventually(
            lambda: self.request("/health/ready"),
            lambda value: value[0] == 200,
            "DB_RECOVERY_READY",
        )
        self.passed("database_outage_live_ready_separation_and_recovery")

    def capacity(self):
        self.memory_peaks["worker"] = int(
            run(
                ["docker", "exec", self.info("worker")["Id"], "cat", "/sys/fs/cgroup/memory.peak"],
                capture=True,
            ).stdout
        )
        self.stack.compose("stop", "worker")
        ids = self.credentials
        payout = {
            "operation_id": str(uuid4()),
            "merchant_account_id": ids["merchant_account"],
            "beneficiary_id": ids["beneficiary_id"],
            "provider_namespace": ids["namespace"],
            "principal_minor": "10",
            "fee_minor": "2",
            "currency": "PLN",
            "contract_version": 1,
        }
        require(
            self.request(
                "/v1/payouts",
                token=ids["tokens"]["merchant"],
                payload=payout,
                key="linux-capacity-" + uuid4().hex,
            )[0]
            == 202,
            "EXPLICIT_SECOND_SYNTHETIC_PAYOUT",
        )
        fixture = Path(__file__).with_name("acceptance_case.py").read_text()
        checked = self.stack.compose(
            "run",
            "--rm",
            "--no-TTY",
            "--entrypoint",
            "python",
            "capacity",
            "-",
            payout["operation_id"],
            capture=True,
            input_data=fixture,
        )
        proof = json.loads(checked.stdout.splitlines()[-1])
        require(
            proof["passed"]
            and proof["quarantine_rows"] == 129
            and proof["source_bytes"] > 16 * 1024 * 1024
            and proof["verification_complete"]
            and proof["scratch_cleaned"],
            "ACTUAL_LINUX_DISK_BACKED_STREAM_CAPACITY",
        )
        relative = Path(proof["export_path"]).relative_to("/var/lib/ledgerops-inputs")
        # This consumer has no DB/network/private-key capability and cannot mutate
        # source bytes. A tiny owned tmpfs exercises actual SQLite ENOSPC handling.
        failed = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--read-only",
                "--memory",
                "512m",
                "--cpus",
                "1",
                "--pids-limit",
                "64",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges:true",
                "--tmpfs",
                "/tiny:rw,size=4m,uid=10001,gid=10001,mode=0700",
                "--mount",
                "type=volume,src=" + self.stack.project + "_evidence-inputs,dst=/inputs,readonly",
                "--entrypoint",
                "python",
                self.stack.state["environment"]["LEDGEROPS_IMAGE"],
                "-m",
                "ledgerops.stream_verification",
                "/inputs/" + str(relative),
                "--scratch-root",
                "/tiny",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        require(failed.returncode == 2, "ACTUAL_ENOSPC_FAILS_CLOSED")
        error = json.loads(failed.stderr.splitlines()[-1])
        require(error["error"] == "SCRATCH_RESOURCE_LIMIT", "BOUNDED_SCRATCH_FAILURE_CODE")
        proof["scratch_full_code"] = error["error"]
        self.passed("stream10_129_diagnostics_over16MiB_disk_scratch_and_actual_ENOSPC")
        return proof

    def maintenance(self):
        fixture = Path(__file__).with_name("acceptance_maintenance.py").read_text()
        checked = self.stack.compose(
            "run",
            "--rm",
            "--no-TTY",
            "--entrypoint",
            "python",
            "bootstrap",
            "-",
            capture=True,
            input_data=fixture,
        )
        proof = json.loads(checked.stdout.splitlines()[-1])
        require(
            proof["passed"]
            and proof["quarantine_rows"] == 129
            and proof["source_bytes"] > 16 * 1024 * 1024
            and proof["before_booked"] == "19"
            and proof["after_booked"] == "18"
            and proof["hold_history_unchanged"]
            and proof["stable_identity_replays"]
            and proof["resume_report_version"] == 4
            and proof["verification_complete"]
            and proof["capture_context"] == "EXCLUSIVE_WORKSPACE_BARRIER_READ_COMMITTED_V1",
            "ACTUAL_LARGE_SOURCE_MAINTENANCE_AND_RESUME",
        )
        self.passed("large_source_freeze_preview_repair_resume_v4_and_stable_replays")
        return proof

    def default_reentry(self):
        self.stack.up()
        eventually(
            lambda: self.request("/health/ready"),
            lambda value: value[0] == 503,
            "SAFE_REENTRY_RESTORE_NOT_READY",
        )
        require(not self.info("worker")["State"]["Running"], "SAFE_REENTRY_STOPS_PRIOR_LIVE_WORKER")
        require(
            self.request(
                "/v1/purchases",
                token=self.credentials["tokens"]["customer"],
                payload={},
                key="safe-reentry",
            )[0]
            == 503,
            "SAFE_REENTRY_BLOCKS_FINANCIAL_WRITES",
        )
        require(
            not self.info("operator")["State"]["Running"], "SAFE_REENTRY_STOPS_OMITTED_OPERATOR"
        )
        status, _ = self.request(
            "/v1/operator/actions",
            operator=True,
            token=self.credentials["tokens"]["checker"],
            payload={},
        )
        require(status in {502, 503}, "SAFE_REENTRY_HAS_NO_PRIVATE_FINANCIAL_WRITE_PATH")
        self.passed("safe_default_reentry_closes_prior_live_worker_operator_and_writes")

    def admin_runtime(self, *, require_backup):
        image = self.stack.state["environment"]["LEDGEROPS_ADMIN_IMAGE"]
        code = (
            "import json,os,sqlite3,ssl,psycopg,platform; "
            "print(json.dumps({'uid':os.getuid(),'python':platform.python_version(),"
            "'sqlite':sqlite3.sqlite_version,'ssl':ssl.OPENSSL_VERSION}))"
        )
        result = json.loads(
            run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "--read-only",
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges:true",
                    "--entrypoint",
                    "python",
                    image,
                    "-c",
                    code,
                ],
                capture=True,
            ).stdout
        )
        require(
            result["uid"] == 10001 and result["python"] == "3.12.15", "ADMIN_NONROOT_PYTHON_ABI"
        )
        version = run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--entrypoint",
                "pg_dump",
                image,
                "--version",
            ],
            capture=True,
        ).stdout
        require("18.6" in version, "NATIVE_PINNED_PG_DUMP")
        if require_backup:
            output = "/var/lib/ledgerops-backups/acceptance-" + uuid4().hex
            self.stack.compose(
                "run",
                "--rm",
                "backup",
                "backup",
                "backup",
                output,
                "--objects",
                "/var/lib/ledgerops-objects",
                capture=True,
            )
            inspected = self.stack.compose(
                "run", "--rm", "backup", "backup", "inspect", output, capture=True
            )
            require(
                json.loads(inspected.stdout.splitlines()[-1])["backup_version"] == 1,
                "ACTUAL_NATIVE_BACKUP_INTEGRITY",
            )
            database = "ledgerops_restore_linux_" + uuid4().hex[:12]
            restored = self.stack.compose(
                "run",
                "--rm",
                "backup",
                "backup",
                "--database",
                "postgres",
                "restore",
                output,
                "--database",
                database,
                "--objects",
                "/var/lib/ledgerops-backups/restored-objects-" + uuid4().hex,
                "--restore-id",
                str(uuid4()),
                "--admin-actor",
                self.credentials["admin_actor"],
                "--reason",
                "explicit disposable Linux native restore acceptance",
                capture=True,
            )
            restore = json.loads(restored.stdout.splitlines()[-1])
            require(
                restore["runtime_connect_enabled"] is False
                and restore["external_dispatch_enabled"] is False,
                "RESTORED_ACCESS_CLOSED",
            )
            history = self.stack.compose(
                "run",
                "--rm",
                "backup",
                "backup",
                "--database",
                database,
                "history",
                "--provider-url",
                "http://127.0.0.1:8765",
                "--namespace",
                self.credentials["namespace"],
                "--keys-file",
                "/run/ledgerops/provider-keys.json",
                "--history-file",
                "/var/lib/ledgerops-backups/history-" + uuid4().hex + ".json",
                capture=True,
            )
            recovered = json.loads(history.stdout.splitlines()[-1])
            require(recovered["status"] == "RECOVERED", "RESTORED_AUTHENTICATED_HISTORY_RECOVERED")
            # The old bearer enters only stdin of this explicit administrator test,
            # never Docker Env/argv/logs. Report only the independent result.
            code = """import json,sys,psycopg
from pathlib import Path
from psycopg.conninfo import make_conninfo
from ledgerops.http.config import bounded_file
from ledgerops.authentication import authenticate,AuthenticationRequired
from ledgerops.backup import _closed_runtime
from ledgerops.stream_export import export_stream
from ledgerops.stream_verification import verify_stream
import tempfile
values=json.load(sys.stdin)
profile=json.loads(bounded_file(Path('/run/ledgerops/profile.json')))
dsn=make_conninfo(profile['environment']['LEDGEROPS_BACKUP_ADMIN_DSN'],dbname=values['database'])
with psycopg.connect(dsn,autocommit=True) as conn:
 _closed_runtime(conn)
 invalid=False
 try: authenticate(conn,values['old_bearer'])
 except AuthenticationRequired: invalid=True
 modes=conn.execute('SELECT DISTINCT mode FROM ledger.workspace').fetchall()
 provenance=json.loads(Path('/opt/ledgerops-provenance.json').read_bytes())
 with tempfile.TemporaryDirectory(
  prefix='ledgerops-restore-inspect-',dir='/var/lib/ledgerops-scratch'
 ) as root:
  path=export_stream(conn,values['workspace'],root,
                     code_revision=provenance['revision'],dirty=provenance['dirty'])
  report=verify_stream(path,scratch_root='/var/lib/ledgerops-scratch')
 print(json.dumps({'old_bearer_invalid':invalid,'modes':[r[0] for r in modes],
                   'independent_healthy':report['healthy'],
                   'verification_complete':report['verification_complete']}))
"""
            checked = self.stack.compose(
                "run",
                "--rm",
                "--no-TTY",
                "--entrypoint",
                "python",
                "backup",
                "-c",
                code,
                capture=True,
                input_data=json.dumps(
                    {
                        "database": database,
                        "old_bearer": self.credentials["tokens"]["customer"],
                        "workspace": self.credentials["workspace_id"],
                    }
                ),
            )
            proof = json.loads(checked.stdout.splitlines()[-1])
            require(
                proof
                == {
                    "old_bearer_invalid": True,
                    "modes": ["RESTORE"],
                    "independent_healthy": True,
                    "verification_complete": True,
                },
                "RESTORED_IDENTITY_MODE_AND_A_B",
            )
            result["restore"] = {
                "observed_restore_seconds": restore["observed_restore_seconds"],
                "runtime_connect_enabled": False,
                "external_dispatch_enabled": False,
                "history_status": recovered["status"],
                **proof,
            }
        self.passed(
            "admin_python_abi_native_pgtools"
            + ("_backup_restore_history_old_identity" if require_backup else "_preliminary_only")
        )
        return result

    def secrecy_and_shutdown(self):
        state_file = self.directory / "administrator-state.json"
        self.stack.copy_volume_file("bootstrap-secrets", "state.json", state_file)
        administrator = json.loads(state_file.read_bytes())
        logs = self.stack.compose("logs", "--no-color", capture=True).stdout
        secrets = [
            *administrator["passwords"].values(),
            administrator["provider_hmac"],
            administrator["provider_token"],
            *self.credentials["tokens"].values(),
        ]
        require(all(secret not in logs for secret in secrets), "NO_BEARER_PASSWORD_HMAC_IN_LOGS")
        require("password=" not in logs and "postgresql://" not in logs, "NO_DSN_IN_LOGS")
        for path in (self.credentials_file, self.ca_file, state_file):
            require(stat.S_IMODE(path.stat().st_mode) == 0o600, "LOCAL_COPIES_OWNER_ONLY")
        started = time.monotonic()
        self.stack.compose(
            "stop", "--timeout", "50", "api", "operator", "worker", "provider", "proxy"
        )
        require(time.monotonic() - started <= 55, "BOUNDED_GRACEFUL_SHUTDOWN")
        for name in ("api", "operator", "worker", "provider", "proxy"):
            require(not self.info(name)["State"]["Running"], "NO_OWNED_COMPONENT_LEFT_RUNNING")
        self.passed("secret_redaction_and_bounded_shutdown", started)
        state_file.unlink()

    def cleanup_secrets(self):
        for path in (self.credentials_file, self.directory / "administrator-state.json"):
            path.unlink(missing_ok=True)


def failure_summary(error, phase):
    return {
        "phase": phase,
        "category": type(error).__name__,
        "code": str(error) if type(error) is AcceptanceFailure else "LOCAL_DIAGNOSTICS_REQUIRED",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--skip-backup-preliminary", action="store_true")
    parser.add_argument("--allow-dirty-preliminary", action="store_true")
    parser.add_argument("--dispose-owned-test-data", action="store_true")
    args = parser.parse_args()
    stack = Stack(args.project, args.state)
    result, acceptance = (
        {"schema_version": 1, "project": args.project, "checks": [], "passed": False},
        None,
    )
    diagnostics = FailureDiagnostics()
    primary = None
    started = time.monotonic()
    try:
        with (
            diagnostics.scope(),
            tempfile.TemporaryDirectory(prefix="ledgerops-linux-private-") as private,
        ):
            acceptance = Acceptance(stack, private)
            diagnostic_phase("runtime.kernel_and_secrets")
            result["runtime"] = acceptance.runtime()
            labels = acceptance.info("api")["Config"]["Labels"]
            result["source_revision"] = labels["org.opencontainers.image.revision"]
            result["source_dirty"] = labels["io.ledgerops.source_dirty"] == "true"
            require(
                not result["source_dirty"]
                or args.allow_dirty_preliminary
                or args.skip_backup_preliminary,
                "CLEAN_COMMITTED_ACCEPTANCE_SOURCE_REQUIRED",
            )
            result["image_ids"] = {
                name: run(
                    [
                        "docker",
                        "image",
                        "inspect",
                        stack.state["environment"][key],
                        "--format",
                        "{{.Id}}",
                    ],
                    capture=True,
                ).stdout.strip()
                for name, key in (
                    ("runtime", "LEDGEROPS_IMAGE"),
                    ("administrator", "LEDGEROPS_ADMIN_IMAGE"),
                    ("proxy", "LEDGEROPS_PROXY_IMAGE"),
                )
            }
            diagnostic_phase("service.safe_defaults")
            acceptance.safe_defaults()
            diagnostic_phase("business.payout_and_replay")
            acceptance.business()
            diagnostic_phase("capacity.complete_source")
            result["capacity"] = acceptance.capacity()
            diagnostic_phase("maintenance.rebuild")
            result["maintenance"] = acceptance.maintenance()
            diagnostic_phase("service.database_outage")
            acceptance.database_outage()
            result["memory_peak_bytes"] = {
                name: acceptance.memory_peaks[name]
                if name in acceptance.memory_peaks
                else int(
                    run(
                        [
                            "docker",
                            "exec",
                            acceptance.info(name)["Id"],
                            "cat",
                            "/sys/fs/cgroup/memory.peak",
                        ],
                        capture=True,
                    ).stdout
                )
                for name in ("api", "operator", "provider", "worker", "postgres", "proxy")
            }
            diagnostic_phase("service.safe_reentry")
            acceptance.default_reentry()
            diagnostic_phase("recovery.native_restore")
            result["administrator_runtime"] = acceptance.admin_runtime(
                require_backup=not args.skip_backup_preliminary
            )
            diagnostic_phase("shutdown.secret_audit")
            acceptance.secrecy_and_shutdown()
            result["checks"] = acceptance.checks
            result["passed"] = True
            acceptance.cleanup_secrets()
    except BaseException as error:
        primary = error
        result["failure"] = failure_summary(error, diagnostics.phase)
    # Keep diagnostics active through cleanup. Constructor failures still have a
    # phase, and a second cleanup error must not replace the original failure.
    with diagnostics.scope():
        cleanups = []
        if acceptance is not None:
            result["checks"] = acceptance.checks
            cleanups.append(("cleanup.local_secrets", acceptance.cleanup_secrets))
        if args.dispose_owned_test_data:
            cleanups.append(("cleanup.owned_stack", stack.dispose))
        for phase, cleanup in cleanups:
            diagnostic_phase(phase)
            try:
                cleanup()
            except BaseException as error:
                result.setdefault("cleanup_failures", []).append(failure_summary(error, phase))
                if primary is None:
                    primary = error
                    result["failure"] = failure_summary(error, phase)
        result["passed"] = primary is None
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
        result["diagnostics"] = diagnostics.report()
        try:
            protected_write(args.output, json.dumps(result, sort_keys=True, indent=2).encode())
        except BaseException as error:
            if primary is None:
                primary = error
    if primary is not None:
        raise primary
    print(
        json.dumps(
            {"event": "linux_acceptance_complete", "passed": True, "checks": len(result["checks"])}
        )
    )


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, AssertionError, subprocess.SubprocessError) as exc:
        print(
            json.dumps(
                {
                    "event": "linux_acceptance_failed",
                    "category": type(exc).__name__,
                    "code": str(exc)
                    if type(exc) is AcceptanceFailure
                    else "LOCAL_DIAGNOSTICS_REQUIRED",
                }
            )
        )
        raise SystemExit(2) from None
