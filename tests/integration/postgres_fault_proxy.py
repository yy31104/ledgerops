"""Single-connection loopback PostgreSQL proxy for two observed COMMIT faults.

This test fixture forwards protocol frames, not SQL results or fake exceptions.
It records only COMMIT counters, never startup/password/query payloads. Clients
must disable SSL and GSS encryption for the disposable localhost test connection.
Frame layout: https://www.postgresql.org/docs/18/protocol-message-formats.html
"""

import socket
from threading import Event, Lock, Thread
from time import monotonic

_MAX_FRAME_BYTES = 16 * 1024 * 1024


def _receive_exact(sock, count, stopped):
    chunks = bytearray()
    deadline = monotonic() + 20
    while len(chunks) < count:
        if stopped.is_set():
            raise EOFError("PostgreSQL proxy stopped")
        if monotonic() >= deadline:
            raise TimeoutError("PostgreSQL frame read exceeded watchdog")
        try:
            chunk = sock.recv(count - len(chunks))
        except TimeoutError:
            if monotonic() >= deadline:
                raise
            continue
        if not chunk:
            raise EOFError("PostgreSQL peer closed")
        chunks.extend(chunk)
    return bytes(chunks)


def _receive_body(sock, stopped):
    header = _receive_exact(sock, 4, stopped)
    length = int.from_bytes(header, "big")
    if not 4 <= length <= _MAX_FRAME_BYTES:
        raise ValueError("Invalid PostgreSQL frame length")
    return header, _receive_exact(sock, length - 4, stopped)


class PostgresCommitProxy:
    """Forward one connection; fail before forwarding COMMIT or its success frame."""

    def __init__(self, upstream_port, *, fault):
        if fault not in {"before-forward", "after-commit"}:
            raise ValueError("Unknown COMMIT fault")
        self.upstream_port = upstream_port
        self.fault = fault
        self.frontend_commit_seen = Event()
        self.backend_commit_seen = Event()
        self.fault_applied = Event()
        self.finished = Event()
        self.stopped = Event()
        self.shutdown_lock = Lock()
        self.commit_queries_forwarded = 0
        self.commit_completions_forwarded = 0
        self.errors = []
        self.listener = None
        self.client = None
        self.server = None
        self.worker = None
        self.frontend_worker = None

    def __enter__(self):
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.settimeout(5)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.port = self.listener.getsockname()[1]
        self.worker = Thread(target=self._serve, name="postgres-commit-proxy", daemon=True)
        self.worker.start()
        return self

    def _shutdown(self):
        # Only one relay owns socket shutdown. Concurrent close() calls from the
        # frontend/backend cleanup paths can race while the peer is in recv().
        with self.shutdown_lock:
            if self.stopped.is_set():
                return
            self.stopped.set()
            for sock in (self.client, self.server, self.listener):
                if sock is not None:
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    sock.close()

    def _guard(self, action, direction):
        try:
            action()
        except (OSError, EOFError) as exc:
            if not self.stopped.is_set():
                self.errors.append(f"{direction}: {type(exc).__name__}")
        except Exception as exc:
            # Do not include exception contents, which might contain wire data.
            self.errors.append(f"{direction}: {type(exc).__name__}")
        finally:
            self._shutdown()

    def _serve(self):
        try:
            client, _ = self.listener.accept()
            with self.shutdown_lock:
                if self.stopped.is_set():
                    client.close()
                    return
                self.client = client
            # A short read timeout lets each relay observe cooperative shutdown
            # even where closing a socket from another thread doesn't wake recv.
            self.client.settimeout(0.1)
            server = socket.create_connection(("127.0.0.1", self.upstream_port), timeout=5)
            with self.shutdown_lock:
                if self.stopped.is_set():
                    server.close()
                    return
                self.server = server
            self.server.settimeout(0.1)
            # StartupMessage is length-prefixed but has no one-byte message tag.
            header, body = _receive_body(self.client, self.stopped)
            if len(body) < 4 or int.from_bytes(body[:4], "big") >> 16 != 3:
                raise ValueError("Proxy requires unencrypted PostgreSQL protocol version 3")
            self.server.sendall(header + body)
            self.frontend_worker = Thread(
                target=self._guard,
                args=(self._forward_frontend, "frontend"),
                name="postgres-commit-proxy-frontend",
                daemon=True,
            )
            self.frontend_worker.start()
            self._guard(self._forward_backend, "backend")
        except Exception as exc:
            if not self.stopped.is_set():
                self.errors.append(f"startup: {type(exc).__name__}")
        finally:
            self._shutdown()
            if self.frontend_worker is not None:
                self.frontend_worker.join(timeout=5)
                if self.frontend_worker.is_alive():
                    self.errors.append("frontend thread did not stop")
            self.finished.set()

    def _forward_frontend(self):
        while not self.stopped.is_set():
            tag = _receive_exact(self.client, 1, self.stopped)
            header, body = _receive_body(self.client, self.stopped)
            is_commit = tag == b"Q" and body.rstrip(b"\0 \t\n;").upper() == b"COMMIT"
            if is_commit:
                self.frontend_commit_seen.set()
                if self.fault == "before-forward":
                    self.fault_applied.set()
                    return  # _guard shuts both peers; COMMIT never reaches PostgreSQL.
            self.server.sendall(tag + header + body)
            if is_commit:
                self.commit_queries_forwarded += 1

    def _forward_backend(self):
        while not self.stopped.is_set():
            tag = _receive_exact(self.server, 1, self.stopped)
            header, body = _receive_body(self.server, self.stopped)
            is_commit = tag == b"C" and body == b"COMMIT\0"
            if is_commit:
                self.backend_commit_seen.set()
                if self.fault == "after-commit":
                    self.fault_applied.set()
                    # CommandComplete COMMIT and the following ReadyForQuery
                    # never reach the caller, although PostgreSQL committed.
                    return
            self.client.sendall(tag + header + body)
            if is_commit:
                self.commit_completions_forwarded += 1

    def __exit__(self, exc_type, exc, traceback):
        self._shutdown()
        self.worker.join(timeout=12)
        assert not self.worker.is_alive(), "PostgreSQL proxy thread did not stop"
        assert not self.errors, f"PostgreSQL proxy failed: {self.errors}"
