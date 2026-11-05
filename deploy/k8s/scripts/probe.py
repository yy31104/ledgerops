"""Synthetic black-box client: calls the API over TLS and exports client-side Prometheus metrics.

Stdlib only. Tokens come from the seed output on a PVC; they are never logged or exported.
"""

import http.client
import json
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)
LOCK = threading.Lock()
COUNTS = {}
HIST = {}


def observe(route, code, seconds):
    with LOCK:
        COUNTS[(route, code)] = COUNTS.get((route, code), 0) + 1
        buckets, total, count = HIST.get(route, ([0] * len(BUCKETS), 0.0, 0))
        buckets = [b + (seconds <= le) for b, le in zip(buckets, BUCKETS, strict=True)]
        HIST[route] = (buckets, total + seconds, count + 1)


def render():
    lines = ["# TYPE ledgerops_probe_requests_total counter"]
    with LOCK:
        for (route, code), value in sorted(COUNTS.items()):
            lines.append(f'ledgerops_probe_requests_total{{route="{route}",code="{code}"}} {value}')
        lines.append("# TYPE ledgerops_probe_duration_seconds histogram")
        for route, (buckets, total, count) in sorted(HIST.items()):
            bucket = "ledgerops_probe_duration_seconds_bucket"
            for le, value in zip(BUCKETS, buckets, strict=True):
                lines.append(f'{bucket}{{route="{route}",le="{le}"}} {value}')
            lines.append(f'{bucket}{{route="{route}",le="+Inf"}} {count}')
            lines.append(f'ledgerops_probe_duration_seconds_sum{{route="{route}"}} {total}')
            lines.append(f'ledgerops_probe_duration_seconds_count{{route="{route}"}} {count}')
    return ("\n".join(lines) + "\n").encode()


class Metrics(BaseHTTPRequestHandler):
    def do_GET(self):
        raw = render()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass


def call(context, token, route, path):
    started = time.monotonic()
    try:
        conn = http.client.HTTPSConnection("api.ledgerops.svc", 8443, context=context, timeout=5)
        conn.request("GET", path, headers={"Authorization": "Bearer " + token})
        code = str(conn.getresponse().status)
        conn.close()
    except (OSError, http.client.HTTPException):
        code = "connect_error"
    observe(route, code, time.monotonic() - started)


def main():
    context = ssl.create_default_context(cafile="/run/ca/ca.crt")
    threading.Thread(
        target=ThreadingHTTPServer(("0.0.0.0", 9100), Metrics).serve_forever, daemon=True
    ).start()
    with open("/run/bootstrap/acceptance.json", "rb") as stream:
        state = json.load(stream)
    token = state["tokens"]["customer"]
    while True:
        call(context, token, "me", "/v1/me")
        call(context, token, "accounts", "/v1/accounts")
        time.sleep(0.5)


if __name__ == "__main__":
    main()
