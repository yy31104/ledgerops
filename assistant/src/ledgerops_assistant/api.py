"""Single-process local FastAPI service with bounded SSE investigation and review."""

import asyncio
import json
import threading
from dataclasses import replace
from uuid import UUID

import psycopg
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.staticfiles import StaticFiles

from ledgerops_assistant.agent import Agent
from ledgerops_assistant.drafts import WorkflowConflict
from ledgerops_assistant.providers import get_provider
from ledgerops_assistant.settings import Settings
from ledgerops_assistant.telemetry import RequestLimit, RequestTelemetry, Telemetry
from ledgerops_assistant.tools import APIUnavailable, AuthenticationRequired, ReadOnlyAPI
from ledgerops_assistant.workflow import DraftWorkflow


class Ask(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=8000)
    request_id: UUID
    provider: str = "fake"


class StartDraft(Ask):
    intent: dict


class Resume(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    continue_review: bool
    draft_sha256: str = Field(pattern="^[0-9a-f]{64}$")


def sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def public_result(state):
    return {
        "answer": state["result"],
        "steps": state.get("steps", 0),
        "cost_usd": state.get("cost_usd", 0),
        "cost_kind": "estimated_upper_bound",
        "input_tokens": state.get("input_tokens", 0),
        "output_tokens": state.get("output_tokens", 0),
    }


def public_draft(state):
    return {k: state[k] for k in ("status", "reason", "draft") if k in state}


def create_app(settings=None, *, retriever=None, api_factory=None, telemetry=None):
    settings = settings or Settings.environment()
    if retriever is None:
        from ledgerops_assistant.runtime import retriever_for

        retriever = retriever_for(settings)
    agents = {}
    for name in settings.providers:
        if name not in ("fake", "openai_api", "ollama"):
            raise ValueError("provider is unavailable in the HTTP service")
        limits = (
            settings.limits
            if name == "openai_api"
            else replace(settings.limits, input_usd_per_million=0, output_usd_per_million=0)
        )
        agents[name] = Agent(retriever, get_provider(name), limits=limits)
    workflows = {
        name: DraftWorkflow(settings.database_url, agent) for name, agent in agents.items()
    }
    app = FastAPI(title="LedgerOps operator assistant", docs_url=None, redoc_url=None)
    traces = telemetry or Telemetry()
    app.add_middleware(RequestLimit)
    app.add_middleware(RequestTelemetry, telemetry=traces)
    running = {}
    registry_lock = threading.Lock()
    app.state.telemetry, app.state.running, app.state.agents = traces, running, agents

    @app.exception_handler(AuthenticationRequired)
    async def auth_error(request, error):
        from fastapi.responses import JSONResponse

        return JSONResponse({"detail": "authentication_required"}, 401)

    @app.exception_handler(WorkflowConflict)
    async def workflow_error(request, error):
        from fastapi.responses import JSONResponse

        return JSONResponse({"detail": str(error)}, 409)

    @app.exception_handler(APIUnavailable)
    async def upstream_error(request, error):
        from fastapi.responses import JSONResponse

        return JSONResponse(
            {"detail": error.reason}, 429 if error.reason == "rate_limited" else 503
        )

    @app.exception_handler(psycopg.Error)
    async def database_error(request, error):
        from fastapi.responses import JSONResponse

        return JSONResponse({"detail": "assistant_database_unavailable"}, 503)

    async def authenticate(request):
        raw = request.headers.get("authorization", "")
        if not raw.startswith("Bearer "):
            raise AuthenticationRequired()
        token = raw[7:]
        client = api_factory(token) if api_factory else ReadOnlyAPI(settings.api_origin, token)
        identity = await asyncio.to_thread(client.identity)
        return client, identity

    def selected(name):
        if name not in agents:
            raise HTTPException(400, "provider_not_enabled")
        return agents[name]

    @app.get("/health/live")
    def live():
        return {"status": "live"}

    @app.get("/health/ready")
    def ready():
        if settings.database_url:
            import psycopg

            try:
                with psycopg.connect(settings.database_url, connect_timeout=2) as conn:
                    conn.execute("SELECT 1 FROM assistant_rag.corpora LIMIT 1")
            except psycopg.Error:
                raise HTTPException(503, "assistant_database_unavailable") from None
        return {"status": "ready"}

    @app.get("/v1/demo")
    def demo():
        if not settings.demo_token:
            raise HTTPException(404, "not_available")
        return {"token": settings.demo_token, "notice": "Synthetic demo credential only"}

    @app.get("/v1/session")
    async def session(request: Request):
        _, identity = await authenticate(request)
        return {
            "workspace_id": identity.workspace_id,
            "actor_id": identity.actor_id,
            "scopes": sorted(identity.scopes),
            "providers": list(agents),
            "console_url": settings.console_url,
            "demo": bool(settings.demo_token),
            "embedding": settings.embedder,
        }

    @app.post("/v1/assist")
    async def assist(body: Ask, request: Request):
        client, identity = await authenticate(request)
        agent = selected(body.provider)
        key = (identity.owner_key, str(body.request_id))
        cancel = threading.Event()
        with registry_lock:
            if key in running:
                raise HTTPException(409, "request_already_running")
            if len(running) >= 4:
                raise HTTPException(429, "assistant_busy")
            running[key] = cancel
        queue = asyncio.Queue(maxsize=64)
        loop = asyncio.get_running_loop()

        def progress(event):
            loop.call_soon_threadsafe(queue.put_nowait, event)

        async def execute():
            def charge(amount):
                request.state.cost_usd = getattr(request.state, "cost_usd", 0) + amount

            return await asyncio.to_thread(
                agent.run, body.question, client, cancel=cancel, on_event=progress, on_charge=charge
            )

        task = asyncio.create_task(execute())

        def finished(done):
            with registry_lock:
                running.pop(key, None)
            if not done.cancelled():
                done.exception()  # consume failures even after a browser disconnect

        task.add_done_callback(finished)

        async def stream():
            try:
                yield sse(
                    "started", {"request_id": str(body.request_id), "provider": body.provider}
                )
                while not task.done() or not queue.empty():
                    if await request.is_disconnected():
                        cancel.set()
                        return
                    try:
                        event = await asyncio.wait_for(queue.get(), 0.25)
                        yield sse("step", event)
                    except TimeoutError:
                        yield ": keepalive\n\n"
                state = task.result()
                request.state.cost_usd, request.state.steps = (
                    state.get("cost_usd", 0),
                    state.get("steps", 0),
                )
                result = public_result(state)
                # Prose streams only after schema/citation validation, never before it.
                for start in range(0, len(result["answer"]["text"]), 80):
                    if cancel.is_set():
                        break
                    yield sse("text", {"delta": result["answer"]["text"][start : start + 80]})
                yield sse("result", result)
            except AuthenticationRequired:
                yield sse("error", {"reason": "authentication_required"})
            except Exception:
                yield sse("error", {"reason": "assistant_unavailable"})
            finally:
                cancel.set()

        return StreamingResponse(
            stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"}
        )

    @app.delete("/v1/runs/{request_id}")
    async def cancel_run(request_id: UUID, request: Request):
        _, identity = await authenticate(request)
        with registry_lock:
            event = running.get((identity.owner_key, str(request_id)))
            if event is None:
                raise HTTPException(404, "not_available")
            event.set()
        return {"status": "cancelling"}

    @app.post("/v1/drafts/{thread_id}")
    async def start_draft(thread_id: UUID, body: StartDraft, request: Request):
        client, identity = await authenticate(request)
        selected(body.provider)
        key = (identity.owner_key, str(body.request_id))
        cancel = threading.Event()
        with registry_lock:
            if key in running:
                raise HTTPException(409, "request_already_running")
            if len(running) >= 4:
                raise HTTPException(429, "assistant_busy")
            running[key] = cancel

        async def execute_draft():
            def charge(amount):
                request.state.cost_usd = getattr(request.state, "cost_usd", 0) + amount

            try:
                return await asyncio.to_thread(
                    workflows[body.provider].invoke,
                    str(thread_id),
                    client,
                    question=body.question,
                    request_id=str(body.request_id),
                    intent=body.intent,
                    cancel=cancel,
                    on_charge=charge,
                )
            finally:
                with registry_lock:
                    running.pop(key, None)

        task = asyncio.create_task(execute_draft())
        try:
            state = await asyncio.shield(task)
        except (ValueError, TypeError, AttributeError):
            raise HTTPException(422, "invalid_draft_input") from None
        return public_draft(state)

    @app.get("/v1/drafts/{thread_id}")
    async def read_draft(thread_id: UUID, request: Request, provider: str = "fake"):
        client, _ = await authenticate(request)
        selected(provider)
        return public_draft(
            await asyncio.to_thread(workflows[provider].inspect, str(thread_id), client)
        )

    @app.post("/v1/drafts/{thread_id}/review")
    async def resume(thread_id: UUID, body: Resume, request: Request, provider: str = "fake"):
        client, _ = await authenticate(request)
        selected(provider)
        state = await asyncio.to_thread(
            workflows[provider].invoke,
            str(thread_id),
            client,
            decision={"continue": body.continue_review, "draft_sha256": body.draft_sha256},
        )
        return public_draft(state)

    if settings.web_dist.is_dir():
        app.mount("/assets", StaticFiles(directory=settings.web_dist / "assets"), name="assets")

        @app.get("/")
        def panel():
            return FileResponse(settings.web_dist / "index.html")

    return app
