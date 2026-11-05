"""Run the authenticated service using the pinned production WSGI server."""

import json
import os
import sys

from gunicorn.app.base import BaseApplication
from gunicorn.glogging import Logger

from ledgerops.http.application import Application
from ledgerops.http.config import ServiceConfig


class SafeLogger(Logger):
    def warning(self, *args, **kwargs):
        self.error_log.warning('{"event":"http_server_warning"}')

    def error(self, *args, **kwargs):
        self.error_log.error('{"event":"http_server_error"}')

    def exception(self, *args, **kwargs):
        self.error_log.error('{"event":"http_server_exception"}')

    def critical(self, *args, **kwargs):
        self.error_log.critical('{"event":"http_server_critical"}')


def worker_ready(worker):
    print(json.dumps({"event": "http_worker_ready", "pid": os.getpid()}), flush=True)


class Server(BaseApplication):
    def __init__(self, config):
        self.service_config = config
        super().__init__()

    def load_config(self):
        config = self.service_config
        values = {
            "bind": config.bind,
            "workers": config.workers,
            "worker_class": "sync",
            "timeout": 40,
            "graceful_timeout": 45,
            "keepalive": 0,
            "backlog": 64,
            "limit_request_line": 4094,
            "limit_request_fields": 32,
            "limit_request_field_size": 2048,
            "forwarded_allow_ips": ",".join(config.trusted_proxies),
            "secure_scheme_headers": {"X-FORWARDED-PROTO": "https"},
            "proxy_protocol": False,
            "reload": False,
            "preload_app": False,
            "max_requests": 1000,
            "max_requests_jitter": 100,
            "accesslog": None,
            "errorlog": "-",
            "capture_output": False,
            "logger_class": SafeLogger,
            "control_socket_disable": True,
            "post_worker_init": worker_ready,
        }
        for key, value in values.items():
            self.cfg.set(key, value)

    def load(self):
        return Application(self.service_config)


def main():
    try:
        config = ServiceConfig.from_env()
    except (OSError, ValueError, TypeError, RecursionError):
        print(json.dumps({"error": "SERVICE_CONFIGURATION_INVALID"}), file=sys.stderr)
        return 2
    # Configuration is an explicit allowlist; inherited arbitrary Gunicorn CLI
    # settings cannot enable debug/reload/forwarded identity or alter bounds.
    os.environ.pop("GUNICORN_CMD_ARGS", None)
    Server(config).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
