"""Exercise the Azure Caddyfile against the real production WSGI boundary locally."""

import argparse
import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def request(listen, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", listen, timeout=5)
    try:
        conn.request("GET", "/", headers={"Host": "localhost", **(headers or {})})
        response = conn.getresponse()
        return response.status, response.read().decode()
    finally:
        conn.close()


def ready(process, listen):
    for _ in range(100):
        if process.poll() is not None:
            raise RuntimeError("server exited before it was ready")
        try:
            request(listen)
            return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("server startup timed out")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--caddy", default="caddy")
    parser.add_argument("--break-forwarded-proto", action="store_true")
    parser.add_argument("--serve", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.serve:
        from ledgerops.http.config import ServiceConfig
        from ledgerops.service import Server

        Server(
            ServiceConfig(
                dsn="host=127.0.0.1 port=1 dbname=unused connect_timeout=1",
                hosts=("localhost",),
                bind=f"127.0.0.1:{args.serve}",
                workers=1,
            )
        ).run()
        return

    api_port, proxy_port = port(), port()
    source = (Path(__file__).parent / "modules/container_apps/Caddyfile").read_text()
    config = source.replace("http://:8081 {", f"http://:{proxy_port} {{\n\tbind 127.0.0.1").replace(
        "127.0.0.1:8080", f"127.0.0.1:{api_port}"
    )
    if args.break_forwarded_proto:
        config = config.replace("header_up X-Forwarded-Proto https", "")

    with tempfile.TemporaryDirectory(prefix="ledgerops-azure-proxy-") as tmp:
        config_path = Path(tmp) / "Caddyfile"
        config_path.write_text(config)
        processes = []
        with (Path(tmp) / "servers.log").open("w+") as log:
            try:
                for command, listen in [
                    ([sys.executable, __file__, "--serve", str(api_port)], api_port),
                    (
                        [args.caddy, "run", "--config", str(config_path), "--adapter", "caddyfile"],
                        proxy_port,
                    ),
                ]:
                    process = subprocess.Popen(
                        command,
                        stdout=log,
                        stderr=log,
                        env={**os.environ, "XDG_DATA_HOME": tmp, "XDG_CONFIG_HOME": tmp},
                    )
                    processes.append(process)
                    ready(process, listen)
                direct, body = request(api_port)
                assert direct == 400 and "HTTPS_REQUIRED" in body, (direct, body)
                for headers, expected in [
                    ({}, 200),
                    ({"Origin": "https://localhost"}, 200),
                    ({"X-Forwarded-Proto": "http"}, 200),
                    ({"Host": "untrusted.example"}, 400),
                    ({"Origin": "https://untrusted.example"}, 403),
                ]:
                    status, body = request(proxy_port, headers)
                    assert status == expected, (headers, status, body)
                    assert body, (headers, status, "empty response")
                print(json.dumps({"checks_passed": 6, "direct_http": direct, "proxied_https": 200}))
            except BaseException:
                log.flush()
                print(Path(log.name).read_text(), file=sys.stderr)
                raise
            finally:
                for process in reversed(processes):
                    process.terminate()
                for process in reversed(processes):
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()


if __name__ == "__main__":
    main()
