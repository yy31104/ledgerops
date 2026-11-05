"""A closed GET-only surface, using the current operator's own credential."""

import hashlib
import json
import math
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlsplit
from uuid import UUID

import httpx

TOOLS = {
    "payout": ("/v1/payouts/{}", "payout:read"),
    "operation": ("/v1/operations/{}", "operations:read"),
    "investigation": ("/v1/investigation/operations/{}", "evidence:read"),
    "accounts": ("/v1/accounts", "accounts:read"),
    "metrics": ("/v1/metrics", "evidence:read"),
}


class AuthenticationRequired(Exception):
    pass


class APIUnavailable(Exception):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class Identity:
    workspace_id: str
    actor_id: str
    client_id: str
    scopes: frozenset[str]

    @property
    def owner_key(self):
        raw = f"{self.workspace_id}:{self.actor_id}:{self.client_id}"
        return hashlib.sha256(raw.encode()).hexdigest()


@dataclass(repr=False)
class ReadOnlyAPI:
    base_url: str
    token: str = field(repr=False)
    transport: object = field(default=None, repr=False)
    timeout: float = 5
    attempts: int = 2
    sleep: object = field(default=time.sleep, repr=False)

    def __post_init__(self):
        url = urlsplit(self.base_url)
        if (
            url.scheme not in ("http", "https")
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.path not in ("", "/")
        ):
            raise ValueError("configure a fixed API origin without credentials, path or query")
        if url.scheme == "http" and url.hostname not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("plaintext API access is restricted to loopback")
        if not re.fullmatch(r"lo1_[A-Za-z0-9_-]{43}", self.token):
            raise AuthenticationRequired("a current LedgerOps token is required")
        if not 1 <= self.attempts <= 3 or not 0 < self.timeout <= 10:
            raise ValueError("invalid HTTP limits")

    def _get(self, path):
        with httpx.Client(
            transport=self.transport, follow_redirects=False, timeout=self.timeout, trust_env=False
        ) as client:
            for attempt in range(self.attempts):
                try:
                    with client.stream(
                        "GET",
                        self.base_url.rstrip("/") + path,
                        headers={"Authorization": "Bearer " + self.token},
                    ) as response:
                        status = response.status_code
                        if status == 401:
                            raise AuthenticationRequired("credential expired or revoked")
                        if status == 429 and attempt + 1 < self.attempts:
                            try:
                                delay = float(response.headers.get("Retry-After", "0.1"))
                                delay = min(max(delay, 0), 2) if math.isfinite(delay) else 0.1
                            except ValueError:
                                delay = 0.1
                            self.sleep(delay)
                            continue
                        if status != 200:
                            label = {
                                403: "forbidden",
                                404: "not_found_or_hidden",
                                429: "rate_limited",
                            }.get(status, "upstream_unavailable")
                            return {"status": label}
                        body = bytearray()
                        for part in response.iter_bytes():
                            body.extend(part)
                            if len(body) > 65536:
                                return {"status": "response_too_large"}
                        try:
                            data = json.loads(body)
                        except (ValueError, UnicodeError):
                            return {"status": "invalid_response"}
                        return (
                            {"status": "ok", "data": data}
                            if isinstance(data, dict)
                            else {"status": "invalid_response"}
                        )
                except (httpx.TimeoutException, httpx.NetworkError):
                    if attempt + 1 == self.attempts:
                        return {"status": "timeout"}
                except httpx.HTTPError:
                    return {"status": "upstream_unavailable"}
        return {"status": "upstream_unavailable"}

    def identity(self) -> Identity:
        result = self._get("/v1/me")
        if result["status"] != "ok":
            raise APIUnavailable(result["status"])
        data = result["data"]
        try:
            ids = [str(UUID(data[k])) for k in ("workspace_id", "actor_id", "client_id")]
            scopes = data["scopes"]
            if not isinstance(scopes, list) or not all(isinstance(s, str) for s in scopes):
                raise ValueError
        except (KeyError, ValueError, TypeError, AttributeError):
            raise AuthenticationRequired("invalid identity response") from None
        return Identity(*ids, frozenset(scopes))

    def read(self, tool, object_id, owner_key):
        if tool not in TOOLS:
            raise ValueError("tool is not in the read-only allowlist")
        route, scope = TOOLS[tool]
        if "{}" in route:
            object_id = str(UUID(object_id))
            route = route.format(object_id)
        elif object_id is not None:
            raise ValueError("tool does not accept an object ID")
        current = self.identity()
        if current.owner_key != owner_key:
            raise AuthenticationRequired("operator identity changed")
        result = self._get(route) if scope in current.scopes else {"status": "forbidden"}
        raw = json.dumps(result, sort_keys=True, separators=(",", ":"))
        return {
            "tool": tool,
            "object_id": object_id,
            "endpoint": route,
            **result,
            "source_id": "api:" + hashlib.sha256((route + raw).encode()).hexdigest(),
            "observed_at": datetime.now(UTC).isoformat(),
            "complete": False,
            "warning": "Caller-scoped point-in-time evidence; recent lists are bounded. "
            "404 can conceal an object. Absence is not proof of no payment.",
        }
