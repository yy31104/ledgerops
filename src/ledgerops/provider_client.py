"""Bounded, single-attempt client for the loopback synthetic provider only."""

import http.client
import socket
import threading
from dataclasses import dataclass
from urllib.parse import urlsplit

from ledgerops.provider_protocol import MAX_RESPONSE_BYTES


class TransportError(ConnectionError):
    pass


@dataclass(frozen=True, slots=True)
class TransportResponse:
    http_status: int
    body: bytes

    def __post_init__(self):
        if type(self.http_status) is not int or not 100 <= self.http_status <= 599:
            raise ValueError("invalid HTTP response status")
        if type(self.body) is not bytes or len(self.body) > MAX_RESPONSE_BYTES:
            raise ValueError("invalid bounded response body")


class ProviderHTTPClient:
    """No redirects, ambient proxies or internal retries; caller owns durable retry."""

    def __init__(self, url, token, *, timeout=5.0):
        parsed = urlsplit(url)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.port is None
            or parsed.username
            or parsed.password
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("synthetic provider requires an explicit loopback HTTP endpoint")
        if (
            type(token) is not str
            or not 32 <= len(token) <= 512
            or any(not 33 <= ord(c) <= 126 for c in token)
        ):
            raise ValueError("invalid provider command credential")
        if type(timeout) not in (int, float) or not 0 < timeout <= 30:
            raise ValueError("provider timeout must be finite and at most30seconds")
        self._host, self._port, self._token, self._timeout = (
            parsed.hostname,
            parsed.port,
            token,
            timeout,
        )

    def _request(self, method, key, body=None, suffix=""):
        # Typed retained keys are interpolated into a path, never into a URL/host.
        parts = key.split(".") if type(key) is str else []
        if (
            len(parts) != 4
            or parts[0] != "v1"
            or len(key) > 128
            or any(c not in "0123456789abcdef-v." for c in key)
        ):
            raise ValueError("invalid retained provider key")
        if body is not None and (type(body) is not bytes or not 1 <= len(body) <= 32768):
            raise ValueError("invalid retained provider request bytes")
        conn = http.client.HTTPConnection(self._host, self._port, timeout=self._timeout)
        expired = threading.Event()
        owned_socket = []

        def expire():
            expired.set()
            if owned_socket:
                try:
                    owned_socket[0].shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

        # The socket's timeout alone only bounds inactivity. Closing this exact
        # owned socket also bounds slow headers/body bytes that keep arriving.
        deadline = threading.Timer(self._timeout, expire)
        deadline.start()
        response = None
        try:
            conn.connect()
            owned_socket.append(conn.sock)
            if expired.is_set():
                raise TimeoutError("provider transport deadline expired")
            conn.request(
                method,
                f"/v1/executions/{key}{suffix}",
                body=body,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Content-Type": "application/json",
                    "Connection": "close",
                },
            )
            response = conn.getresponse()
            data = response.read(MAX_RESPONSE_BYTES + 1)
            if expired.is_set():
                raise TimeoutError("provider transport deadline expired")
            if len(data) > MAX_RESPONSE_BYTES:
                raise TransportError("provider response exceeds bounded transport limit")
            return TransportResponse(response.status, data)
        except TimeoutError:
            raise
        except (OSError, http.client.HTTPException) as exc:
            if expired.is_set():
                raise TimeoutError("provider transport deadline expired") from exc
            raise TransportError("provider transport failed; retain original identity") from exc
        finally:
            deadline.cancel()
            deadline.join()
            if response is not None:
                response.close()
            conn.close()

    def put(self, key, request_bytes):
        return self._request("PUT", key, request_bytes)

    def lookup(self, key):
        return self._request("GET", key)

    def close(self, key, request_bytes):
        return self._request("POST", key, request_bytes, "/close-if-absent")
