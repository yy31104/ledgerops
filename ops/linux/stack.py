"""Build and operate an explicitly owned, loopback-only LedgerOps Linux stack."""

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path
from uuid import uuid4

HERE = Path(__file__).resolve().parent
PYTHON_IMAGE = (
    "python:3.12.15-slim-bookworm@sha256:"
    "6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70"
)
CADDY_IMAGE = (
    "caddy:2.11.7-alpine@sha256:eb4d35b7452d1ab6171fbdbe8d9275b2301dad6dad92115d91e19fb9b24d510f"
)
POSTGRES_IMAGE = "postgres@sha256:5a5a84b19854a9ffaa54082c166ff4ec27473a361e496e5ea167f298f2da9722"

VOLUMES = (
    "api-secrets",
    "worker-secrets",
    "provider-secrets",
    "operator-secrets",
    "bootstrap-secrets",
    "backup-secrets",
    "postgres-secrets",
    "postgres-data",
    "provider-data",
    "objects",
    "backups",
    "caddy-data",
    "reconciler-secrets",
    "operator-scratch",
    "bootstrap-scratch",
    "backup-scratch",
    "reconciler-scratch",
    "evidence-inputs",
)


def owned_labels(labels, project):
    return (
        bool(labels)
        and labels.get("com.docker.compose.project") == project
        and (labels.get("io.ledgerops.scope") == "local-linux-stack")
    )


def run(arguments, *, env=None, capture=False, timeout=300, input_data=None):
    pause = os.environ.get("LEDGEROPS_DOCKER_PAUSE_FILE")
    mutations = {"create", "run", "up", "down", "rm", "start", "stop", "restart"}
    if pause and arguments[0] == "docker" and mutations.intersection(arguments[1:]):
        while Path(pause).exists():
            time.sleep(0.1)
    return subprocess.run(
        arguments,
        env=env,
        check=True,
        timeout=timeout,
        capture_output=capture,
        text=capture or isinstance(input_data, str),
        input=input_data,
    )


def protected_write(path, raw):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def _copy_context_files(source, operations, destination):
    for name in ("pyproject.toml", "uv.lock", "README.md"):
        target = destination / "source" / name
        target.parent.mkdir(exist_ok=True)
        shutil.copyfile(source / name, target)
    shutil.copytree(
        source / "src",
        destination / "source" / "src",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    shutil.copytree(
        source / "provider_sim",
        destination / "provider_sim",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    (destination / "scripts").mkdir()
    for name in ("launcher.py", "bootstrap.py"):
        shutil.copyfile(operations / name, destination / "scripts" / name)
    for name in ("requirements-build.txt", "requirements-runtime.txt", "Dockerfile", "Caddyfile"):
        shutil.copyfile(operations / name, destination / name)


def prepare_context(source, destination, *, allow_dirty=False):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    revision = run(["git", "-C", str(source), "rev-parse", "HEAD"], capture=True).stdout.strip()
    dirty = (
        bool(
            run(
                ["git", "-C", str(source), "status", "--porcelain", "--untracked-files=normal"],
                capture=True,
            ).stdout.strip()
        )
        or HERE != source / "ops" / "linux"
    )
    if dirty and not allow_dirty:
        raise ValueError("CLEAN_COMMITTED_SOURCE_AND_OPS_REQUIRED_OR_EXPLICIT_ALLOW_DIRTY")
    destination.mkdir(parents=True, exist_ok=False)
    if dirty:
        _copy_context_files(source, HERE, destination)
    else:
        # Read the immutable commit rather than racing a later worktree edit.
        # This is the source authority; no source-file digest inventory is created.
        selected = [
            "src",
            "provider_sim",
            "pyproject.toml",
            "uv.lock",
            "README.md",
            "ops/linux/launcher.py",
            "ops/linux/bootstrap.py",
            "ops/linux/Dockerfile",
            "ops/linux/Caddyfile",
            "ops/linux/requirements-build.txt",
            "ops/linux/requirements-runtime.txt",
        ]
        raw = subprocess.run(
            ["git", "-C", str(source), "archive", revision, *selected],
            check=True,
            capture_output=True,
            timeout=30,
        ).stdout
        with tempfile.TemporaryDirectory(prefix="ledgerops-committed-build-") as temporary:
            with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
                archive.extractall(temporary, filter="data")
            committed = Path(temporary)
            _copy_context_files(committed, committed / "ops/linux", destination)
    provenance = {
        "schema_version": 1,
        "revision": revision,
        "dirty": dirty,
        "base_images": {"python": PYTHON_IMAGE, "postgres": POSTGRES_IMAGE, "proxy": CADDY_IMAGE},
    }
    raw = json.dumps(provenance, sort_keys=True, separators=(",", ":")).encode()
    (destination / "source-provenance.json").write_bytes(raw)
    return provenance, hashlib.sha256(raw).hexdigest()


class Stack:
    def __init__(self, project, state_file):
        if re.fullmatch(r"ledgerops-[a-z0-9][a-z0-9-]{0,40}", project) is None:
            raise ValueError("OWNED_LEDGEROPS_PROJECT_NAME_REQUIRED")
        self.project, self.state_file = project, Path(state_file).absolute()
        self.state = json.loads(self.state_file.read_bytes()) if self.state_file.exists() else None
        if self.state is not None and self.state["project"] != project:
            raise ValueError("STACK_STATE_PROJECT_MISMATCH")

    def environment(self, *, live=False):
        if self.state is None:
            raise ValueError("BUILD_OR_EXPLICIT_INIT_STATE_REQUIRED")
        return {
            **os.environ,
            "COMPOSE_PROFILES": "admin,operator,dispatch,acceptance",
            **self.state["environment"],
            "LEDGEROPS_RESTORE_MODE": "false" if live else "true",
            "LEDGEROPS_API_WRITES_ENABLED": "true" if live else "false",
            "LEDGEROPS_DISPATCH_ENABLED": "true" if live else "false",
        }

    def compose(self, *arguments, live=False, capture=False, timeout=300, input_data=None):
        return run(
            [
                "docker",
                "compose",
                "--project-name",
                self.project,
                "--file",
                str(HERE / "compose.yaml"),
                *arguments,
            ],
            env=self.environment(live=live),
            capture=capture,
            timeout=timeout,
            input_data=input_data,
        )

    def new_state(self, image, admin_image, proxy_image, *, https_port=8443, operator_port=9443):
        if self.state is not None:
            raise ValueError("EXISTING_STACK_STATE_IS_IMMUTABLE")
        # Choose an unoccupied private /24; record it so restarts retain proxy trust.
        lines = run(
            ["docker", "network", "ls", "--format", "json"], capture=True
        ).stdout.splitlines()
        network_ids = [json.loads(line)["ID"] for line in lines]
        used = (
            run(["docker", "network", "inspect", *network_ids], capture=True).stdout
            if network_ids
            else ""
        )
        octet = next(number for number in range(80, 240) if f"172.30.{number}." not in used)
        self.state = {
            "schema_version": 1,
            "project": self.project,
            "environment": {
                "LEDGEROPS_IMAGE": image,
                "LEDGEROPS_ADMIN_IMAGE": admin_image,
                "LEDGEROPS_PROXY_IMAGE": proxy_image,
                "LEDGEROPS_SUBNET": f"172.30.{octet}.0/24",
                "LEDGEROPS_DYNAMIC_RANGE": f"172.30.{octet}.128/25",
                "LEDGEROPS_PROXY_IP": f"172.30.{octet}.2",
                "LEDGEROPS_HTTPS_PORT": str(https_port),
                "LEDGEROPS_OPERATOR_PORT": str(operator_port),
            },
        }
        protected_write(self.state_file, json.dumps(self.state, sort_keys=True).encode())

    def copy_volume_file(self, volume_suffix, source, destination):
        volume = self.project + "_" + volume_suffix
        info = json.loads(run(["docker", "volume", "inspect", volume], capture=True).stdout)[0]
        if info["Labels"].get("com.docker.compose.project") != self.project:
            raise ValueError("OWNED_VOLUME_REQUIRED")
        name = self.project + "-copy-" + uuid4().hex[:8]
        container = None
        try:
            container = run(
                [
                    "docker",
                    "create",
                    "--name",
                    name,
                    "--network",
                    "none",
                    "--entrypoint",
                    "python",
                    "--read-only",
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges:true",
                    "--log-driver",
                    "none",
                    "--mount",
                    f"type=volume,src={volume},dst=/source,readonly",
                    self.state["environment"]["LEDGEROPS_IMAGE"],
                    "-c",
                    "import time; time.sleep(30)",
                ],
                capture=True,
            ).stdout.strip()
            run(["docker", "start", container], capture=True)
            destination = Path(destination)
            if destination.exists() or destination.is_symlink():
                raise ValueError("NEW_OUTPUT_PATH_REQUIRED")
            destination.parent.mkdir(parents=True, exist_ok=True)
            run(["docker", "cp", f"{container}:/source/{source}", str(destination)], capture=True)
            os.chmod(destination, 0o600)
        finally:
            if container is not None:
                run(["docker", "rm", "--force", container], capture=True)

    def assert_owned_resources(self, *, volumes=False):
        identifiers = run(
            [
                "docker",
                "ps",
                "--all",
                "--filter",
                "label=com.docker.compose.project=" + self.project,
                "--format",
                "{{.ID}}",
            ],
            capture=True,
        ).stdout.splitlines()
        for identifier in identifiers:
            info = json.loads(run(["docker", "inspect", identifier], capture=True).stdout)[0]
            if not owned_labels(info["Config"].get("Labels"), self.project):
                raise ValueError("UNEXPECTED_CONTAINER_SCOPE")
        existing_networks = set(
            run(
                ["docker", "network", "ls", "--format", "{{.Name}}"],
                capture=True,
            ).stdout.splitlines()
        )
        for suffix in ("private", "ingress"):
            name = self.project + "_" + suffix
            if name in existing_networks:
                info = json.loads(run(["docker", "network", "inspect", name], capture=True).stdout)[
                    0
                ]
                if not owned_labels(info.get("Labels"), self.project):
                    raise ValueError("UNEXPECTED_NETWORK_SCOPE")
        if volumes:
            existing_volumes = set(
                run(
                    ["docker", "volume", "ls", "--format", "{{.Name}}"],
                    capture=True,
                ).stdout.splitlines()
            )
            for suffix in VOLUMES:
                name = self.project + "_" + suffix
                if name in existing_volumes:
                    info = json.loads(
                        run(["docker", "volume", "inspect", name], capture=True).stdout
                    )[0]
                    if not owned_labels(info.get("Labels"), self.project):
                        raise ValueError("UNEXPECTED_VOLUME_SCOPE")

    def up(self, *, live=False, operator=False):
        self.assert_owned_resources(volumes=True)
        # A safe restart also closes a worker left running by an earlier explicit
        # live session; merely omitting it from `up` would leave dispatch enabled.
        if not live:
            self.compose("stop", "--timeout", "50", "worker")
        if not operator:
            self.compose("stop", "--timeout", "50", "operator")
        services = ["postgres", "provider", "api", "proxy"]
        if operator:
            services.append("operator")
        if live:
            services.append("worker")
        self.compose("up", "--detach", *services, live=live)

    def down(self):
        self.assert_owned_resources()
        self.compose("down", "--remove-orphans")

    def dispose(self):
        # Validate every expected named volume even when there are no containers.
        # A project-prefix name alone never authorizes deleting pre-existing data.
        self.assert_owned_resources(volumes=True)
        self.compose("down", "--volumes", "--remove-orphans")


def build(args):
    context = Path(args.context).absolute()
    provenance, descriptor_hash = prepare_context(
        args.source, context, allow_dirty=args.allow_dirty
    )
    suffix = provenance["revision"][:12] + (
        "-dirty-" + uuid4().hex[:8] if provenance["dirty"] else ""
    )
    image, admin, proxy = (
        "ledgerops-local:" + suffix,
        "ledgerops-local-admin:" + suffix,
        "ledgerops-local-proxy:" + suffix,
    )
    for target, tag in (("runtime", image), ("administrator", admin), ("proxy", proxy)):
        run(
            [
                "docker",
                "build",
                "--target",
                target,
                "--tag",
                tag,
                "--build-arg",
                "SOURCE_REVISION=" + provenance["revision"],
                "--build-arg",
                "SOURCE_DIRTY=" + str(provenance["dirty"]).lower(),
                str(context),
            ],
            timeout=900,
        )
    stack = Stack(args.project, args.state)
    stack.new_state(
        image, admin, proxy, https_port=args.https_port, operator_port=args.operator_port
    )
    print(
        json.dumps(
            {
                "event": "linux_artifacts_built",
                "image": image,
                "admin_image": admin,
                "revision": provenance["revision"],
                "dirty": provenance["dirty"],
                "context_descriptor_sha256": descriptor_hash,
            }
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--state", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("build")
    command.add_argument("--source", type=Path, required=True)
    command.add_argument("--context", type=Path, required=True)
    command.add_argument("--allow-dirty", action="store_true")
    command.add_argument("--https-port", type=int, default=8443)
    command.add_argument("--operator-port", type=int, default=9443)
    commands.add_parser("init")
    commands.add_parser("seed-synthetic")
    command = commands.add_parser("up")
    command.add_argument("--synthetic-live", action="store_true")
    command.add_argument("--operator", action="store_true")
    for name in ("status", "down", "dispose-owned-test-data", "inspect"):
        commands.add_parser(name)
    command = commands.add_parser("credentials")
    command.add_argument("--output", type=Path, required=True)
    command = commands.add_parser("reconcile")
    command.add_argument("arguments", nargs=argparse.REMAINDER)
    command = commands.add_parser("backup")
    command.add_argument("--database", default="ledgerops")
    command.add_argument("arguments", nargs=argparse.REMAINDER)
    command = commands.add_parser("ca")
    command.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "build":
        build(args)
        return
    stack = Stack(args.project, args.state)
    if args.command in {"init", "up", "seed-synthetic"}:
        stack.assert_owned_resources(volumes=True)
    if args.command == "init":
        stack.compose("stop", "--timeout", "50", "api", "operator", "worker")
        stack.compose("run", "--rm", "--no-deps", "init")
        stack.compose("up", "--detach", "--wait", "postgres")
        stack.compose("run", "--rm", "bootstrap", "bootstrap", "provision")
    elif args.command == "seed-synthetic":
        stack.compose("run", "--rm", "bootstrap", "bootstrap", "seed")
    elif args.command == "up":
        stack.up(live=args.synthetic_live, operator=args.operator)
    elif args.command == "status":
        stack.compose("ps", "--all")
    elif args.command == "down":
        stack.down()
    elif args.command == "dispose-owned-test-data":
        stack.dispose()
    elif args.command == "inspect":
        stack.compose("run", "--rm", "bootstrap", "bootstrap", "inspect")
    elif args.command == "reconcile":
        stack.compose("run", "--rm", "reconciler", "reconciler", *args.arguments)
    elif args.command == "backup":
        stack.compose(
            "run", "--rm", "backup", "backup", "--database", args.database, *args.arguments
        )
    elif args.command == "credentials":
        stack.copy_volume_file("bootstrap-secrets", "acceptance.json", args.output)
    else:
        stack.copy_volume_file("caddy-data", "caddy/pki/authorities/local/root.crt", args.output)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(json.dumps({"event": "owned_stack_command_failed", "category": type(exc).__name__}))
        raise SystemExit(2) from None
