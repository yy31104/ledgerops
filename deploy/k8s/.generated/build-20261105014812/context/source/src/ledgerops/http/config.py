"""Explicit bounded service configuration; no fixture identity or debug fallback."""

import json
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path

from psycopg.conninfo import conninfo_to_dict

from ledgerops.domain import (
    ActorId,
    BeneficiaryId,
    ProviderNamespaceId,
    TrustedPayoutPolicy,
    WorkspaceId,
)
from ledgerops.provider_protocol import ProviderTrust
from ledgerops.worker import _read_keys


class ConfigurationError(ValueError):
    pass


def boolean(env, name, default):
    value = env.get(name, default)
    if value not in ("true", "false"):
        raise ConfigurationError("BOOLEAN_CONFIGURATION_INVALID")
    return value == "true"


def bounded_file(path, maximum=65536):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or not 0 < info.st_size <= maximum
        ):
            raise ConfigurationError("PROTECTED_CONFIGURATION_FILE_REQUIRED")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            raw = stream.read(maximum + 1)
        if len(raw) > maximum:
            raise ConfigurationError("CONFIGURATION_SIZE_LIMIT")
        return raw
    finally:
        os.close(descriptor)


def unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def policies(path):
    if path is None:
        return {}
    value = json.loads(bounded_file(path), object_pairs_hook=unique)
    if (
        type(value) is not dict
        or value.keys() != {"version", "workspaces"}
        or type(value["version"]) is not int
        or value["version"] != 1
    ):
        raise ConfigurationError("PAYOUT_CONFIGURATION_INVALID")
    if type(value["workspaces"]) is not list or len(value["workspaces"]) > 128:
        raise ConfigurationError("PAYOUT_CONFIGURATION_LIMIT")
    result = {}
    for row in value["workspaces"]:
        fields = {
            "workspace_id",
            "provider_namespace",
            "fixed_fee_minor",
            "dispatch_ttl_seconds",
            "beneficiary_owners",
        }
        if (
            type(row) is not dict
            or row.keys() != fields
            or type(row["beneficiary_owners"]) is not dict
        ):
            raise ConfigurationError("PAYOUT_CONFIGURATION_INVALID")
        if len(row["beneficiary_owners"]) > 1000:
            raise ConfigurationError("BENEFICIARY_CONFIGURATION_LIMIT")
        policy = TrustedPayoutPolicy(
            workspace_id=WorkspaceId.parse(row["workspace_id"]),
            provider_namespace=ProviderNamespaceId.parse(row["provider_namespace"]),
            fixed_fee_minor=row["fixed_fee_minor"],
            dispatch_ttl_seconds=row["dispatch_ttl_seconds"],
            beneficiary_owners={
                BeneficiaryId.parse(k): ActorId.parse(v)
                for k, v in row["beneficiary_owners"].items()
            },
        )
        key = str(policy.workspace_id)
        if key in result:
            raise ConfigurationError("DUPLICATE_WORKSPACE_POLICY")
        result[key] = policy
    return result


@dataclass(frozen=True, repr=False)
class ServiceConfig:
    dsn: str = field(repr=False)
    hosts: tuple
    production: bool = True
    restore_mode: bool = True
    writes_enabled: bool = False
    bind: str = "127.0.0.1:8080"
    workers: int = 2
    trusted_proxies: tuple = ("127.0.0.1", "::1")
    payout_policies: dict = field(default_factory=dict, repr=False)

    callback_dsn: str | None = field(default=None, repr=False)
    callback_trust: ProviderTrust | None = field(default=None, repr=False)

    operator_dsn: str | None = field(default=None, repr=False)
    operator_objects: Path | None = field(default=None, repr=False)
    operator_trust: ProviderTrust | None = field(default=None, repr=False)

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        dsn = env.get("LEDGEROPS_SERVICE_DSN", "")
        if not 1 <= len(dsn) <= 16384:
            raise ConfigurationError("SERVICE_DATABASE_CONFIGURATION_REQUIRED")
        conninfo_to_dict(dsn)
        environment = env.get("LEDGEROPS_ENV", "production")
        if environment not in ("production", "development"):
            raise ConfigurationError("ENVIRONMENT_INVALID")
        production = environment == "production"
        if production and not boolean(env, "LEDGEROPS_TLS_TERMINATED", "false"):
            raise ConfigurationError("PRODUCTION_TLS_TERMINATION_REQUIRED")
        hosts = tuple(env.get("LEDGEROPS_SERVICE_HOSTS", "localhost,127.0.0.1").split(","))
        if not 1 <= len(hosts) <= 16 or any(
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", h) for h in hosts
        ):
            raise ConfigurationError("EXPLICIT_HOSTS_REQUIRED")
        bind = env.get("LEDGEROPS_SERVICE_BIND", "127.0.0.1:8080")
        if (
            not re.fullmatch(r"(?:127\.0\.0\.1|0\.0\.0\.0):[0-9]{1,5}", bind)
            or not 1 <= int(bind.rsplit(":", 1)[1]) <= 65535
        ):
            raise ConfigurationError("SERVICE_BIND_INVALID")
        if not production and not bind.startswith("127.0.0.1:"):
            raise ConfigurationError("PLAINTEXT_DEVELOPMENT_IS_LOOPBACK_ONLY")
        count = env.get("LEDGEROPS_SERVICE_WORKERS", "2")
        if not re.fullmatch("[1-4]", count):
            raise ConfigurationError("WORKER_COUNT_INVALID")
        proxies = tuple(env.get("LEDGEROPS_TRUSTED_PROXIES", "127.0.0.1,::1").split(","))
        import ipaddress

        if not 1 <= len(proxies) <= 16:
            raise ConfigurationError("TRUSTED_PROXY_LIMIT")
        for proxy in proxies:
            ipaddress.ip_address(proxy)
        names = (
            "LEDGEROPS_CALLBACK_DSN",
            "LEDGEROPS_CALLBACK_NAMESPACE",
            "LEDGEROPS_CALLBACK_KEYS_FILE",
        )
        callback_dsn = None
        callback_trust = None
        if any(env.get(name) for name in names):
            if not all(env.get(name) for name in names):
                raise ConfigurationError("COMPLETE_CALLBACK_CONFIGURATION_REQUIRED")
            callback_dsn = env[names[0]]
            if not 1 <= len(callback_dsn) <= 16384:
                raise ConfigurationError("CALLBACK_DATABASE_CONFIGURATION_INVALID")
            conninfo_to_dict(callback_dsn)
            callback_trust = ProviderTrust(
                ProviderNamespaceId.parse(env[names[1]]), _read_keys(Path(env[names[2]]))
            )
        operator_names = (
            "LEDGEROPS_OPERATOR_DSN",
            "LEDGEROPS_OPERATOR_OBJECTS",
            "LEDGEROPS_OPERATOR_NAMESPACE",
            "LEDGEROPS_OPERATOR_KEYS_FILE",
        )
        operator_dsn = operator_objects = operator_trust = None
        if any(env.get(name) for name in operator_names):
            if not all(env.get(name) for name in operator_names):
                raise ConfigurationError("COMPLETE_OPERATOR_CONFIGURATION_REQUIRED")
            operator_dsn = env[operator_names[0]]
            if not 1 <= len(operator_dsn) <= 16384:
                raise ConfigurationError("OPERATOR_CONFIGURATION_INVALID")
            conninfo_to_dict(operator_dsn)
            operator_objects = Path(env[operator_names[1]])
            if not operator_objects.is_dir() or operator_objects.is_symlink():
                raise ConfigurationError("EXISTING_OPERATOR_OBJECT_STORE_REQUIRED")
            operator_trust = ProviderTrust(
                ProviderNamespaceId.parse(env[operator_names[2]]),
                _read_keys(Path(env[operator_names[3]])),
            )
        return cls(
            dsn,
            hosts,
            production,
            boolean(env, "LEDGEROPS_RESTORE_MODE", "true"),
            boolean(env, "LEDGEROPS_API_WRITES_ENABLED", "false"),
            bind,
            int(count),
            proxies,
            policies(
                Path(env["LEDGEROPS_PAYOUT_POLICY_FILE"])
                if env.get("LEDGEROPS_PAYOUT_POLICY_FILE")
                else None
            ),
            callback_dsn,
            callback_trust,
            operator_dsn,
            operator_objects,
            operator_trust,
        )
