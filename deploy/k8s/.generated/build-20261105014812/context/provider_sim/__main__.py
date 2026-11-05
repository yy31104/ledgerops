"""Run the synthetic provider with explicit persistent state and environment secrets."""

import argparse
import json
import os
import signal
import sys
import threading

from .server import ProviderHTTPServer, require_token
from .store import ProviderStore


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--key-id", required=True)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--test-control", action="store_true")
    args = parser.parse_args(argv)
    try:
        secret = bytes.fromhex(os.environ.get("LEDGEROPS_PROVIDER_HMAC_KEY", ""))
        if len(secret) < 32:
            raise ValueError("HMAC key must contain at least 32 bytes")
        token = require_token(os.environ.get("LEDGEROPS_PROVIDER_TOKEN"))
        control_token = (
            require_token(os.environ.get("LEDGEROPS_PROVIDER_CONTROL_TOKEN"))
            if args.test_control
            else None
        )
        store = ProviderStore(args.database, args.namespace, args.key_id, secret)
        server = ProviderHTTPServer(store, token=token, port=args.port, control_token=control_token)
    except Exception:
        # Configuration may include paths or credentials. Keep stderr bounded and
        # leave detail diagnosis to explicit local configuration/store inspection.
        print(
            "provider initialization failed: check explicit state/configuration and credentials",
            file=sys.stderr,
        )
        return 2

    stopping = threading.Event()

    def stop(signum, frame):
        if not stopping.is_set():
            stopping.set()
            threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        print(
            json.dumps(
                {
                    "event": "ready",
                    "url": server.url,
                    "pid": os.getpid(),
                    "namespace": args.namespace,
                    "diagnostics": store.diagnostics(),
                }
            ),
            flush=True,
        )
        server.serve_forever(poll_interval=0.1)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
