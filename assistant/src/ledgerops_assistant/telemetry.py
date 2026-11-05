"""OTel request spans record latency and estimated cost, without request contents."""

import sys
import time
from uuid import uuid4

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor


class Telemetry:
    def __init__(self, exporter=None):
        self.provider = TracerProvider()
        self.provider.add_span_processor(
            SimpleSpanProcessor(
                exporter if exporter is not None else ConsoleSpanExporter(out=sys.stderr)
            )
        )
        self.tracer = self.provider.get_tracer("ledgerops.operator-assistant")


class RequestTelemetry:
    def __init__(self, app, telemetry):
        self.app, self.telemetry = app, telemetry

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        started = time.perf_counter()
        state = scope.setdefault("state", {})
        state["request_id"] = str(uuid4())
        status = 500
        with self.telemetry.tracer.start_as_current_span("assistant.http.request") as span:
            span.set_attribute("http.request.method", scope["method"])
            span.set_attribute("request.id", state["request_id"])

            async def traced_send(message):
                nonlocal status
                if message["type"] == "http.response.start":
                    status = message["status"]
                    message = {
                        **message,
                        "headers": [
                            *message.get("headers", []),
                            (b"cache-control", b"no-store"),
                            (b"x-content-type-options", b"nosniff"),
                            (b"referrer-policy", b"no-referrer"),
                            (
                                b"content-security-policy",
                                b"default-src 'self'; script-src 'self'; "
                                b"style-src 'self'; connect-src 'self'; "
                                b"frame-ancestors 'none'; base-uri 'none'",
                            ),
                            (b"x-request-id", state["request_id"].encode()),
                        ],
                    }
                await send(message)

            try:
                await self.app(scope, receive, traced_send)
            finally:
                span.set_attribute("http.response.status_code", status)
                span.set_attribute("request.duration_ms", (time.perf_counter() - started) * 1000)
                span.set_attribute("gen_ai.cost.estimated_usd", state.get("cost_usd", 0.0))
                span.set_attribute("gen_ai.steps", state.get("steps", 0))


class RequestLimit:
    def __init__(self, app, maximum=32768):
        self.app, self.maximum = app, maximum

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        from starlette.responses import JSONResponse

        headers = dict(scope.get("headers", []))
        try:
            declared = int(headers.get(b"content-length", b"0"))
        except ValueError:
            return await JSONResponse({"detail": "invalid_length"}, 400)(scope, receive, send)
        if declared > self.maximum:
            return await JSONResponse({"detail": "request_too_large"}, 413)(scope, receive, send)
        total = 0

        async def limited_receive():
            nonlocal total
            message = await receive()
            total += len(message.get("body", b""))
            if total > self.maximum:
                raise ValueError("request_too_large")
            return message

        try:
            await self.app(scope, limited_receive, send)
        except ValueError as error:
            if str(error) != "request_too_large":
                raise
            await JSONResponse({"detail": "request_too_large"}, 413)(scope, receive, send)
