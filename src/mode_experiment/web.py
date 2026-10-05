"""Local web adapter for the manual experiment; no web dependencies in the runner."""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import secrets
import shutil
import tempfile
from collections import deque
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from mode_experiment.authentication import credential_resolver
from mode_experiment.execution import Backend
from mode_experiment.runner import Limits, Runner
from mode_experiment.tracing import Output, Trace
from tau_ai.openai_codex import OpenAICodexConfig, OpenAICodexProvider
from tau_coding.codex_model_store import cached_codex_model_catalog
from tau_coding.credentials import FileCredentialStore
from tau_coding.provider_catalog import builtin_provider_entry


def pretty(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False)


def pretty_json(text: str) -> str:
    try:
        return pretty(json.loads(text))
    except ValueError:
        return text


def pretty_embedded(text: str) -> str:
    """Expand a JSON result embedded in harness feedback without losing its surrounding text."""
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, end = decoder.raw_decode(text[index:])
        except ValueError:
            continue
        return text[:index] + pretty(value) + text[index + end :]
    return text


def compact(text: str, limit: int = 150) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"


def display_record(text: str) -> dict[str, Any]:
    """Interpret already-redacted diagnostics, without touching transport bytes."""
    for prefix in ("[assistant: completed text] ", "[assistant: final answer] "):
        if text.startswith(prefix):
            return {"kind": "assistant", "summary": "Assistant", "body": text[len(prefix) :]}
    lines = text.splitlines()
    heading = lines[0] if lines else "Event"
    body = pretty_embedded(text) if heading.startswith("[harness: feedback]") else text
    summary = heading
    if heading.startswith(">>> request"):
        headers, separator, payload = text.partition("\n\n")
        body = headers + (separator + pretty_json(payload) if separator else "")
        summary = heading.split(" [", 1)[0] + " · " + (lines[1] if len(lines) > 1 else "")
    elif heading.startswith("<<< response"):
        summary = heading.split(" [", 1)[0] + " · " + (lines[1] if len(lines) > 1 else "")
    elif heading.startswith("<<< body"):
        body = heading + "\n" + pretty_json("\n".join(lines[1:]))
    elif heading.startswith("<<< SSE"):
        data = "\n".join(
            line[5:].removeprefix(" ") for line in lines[1:] if line.startswith("data:")
        )
        fields = [line for line in lines[1:] if not line.startswith("data:")]
        body = "\n".join([heading, *fields, "data:\n" + pretty_json(data)])
        try:
            event = json.loads(data)
            if isinstance(event, dict):
                label = str(event.get("type", "SSE event"))
                detail = event.get("delta", event.get("name", ""))
                response = event.get("response")
                if isinstance(response, dict):
                    detail = response.get("status", detail)
                summary = heading.split(" [", 1)[0] + " · " + label
                if detail:
                    summary += " · " + compact(str(detail), 70)
        except ValueError:
            pass
    elif heading.startswith("[harness: result] "):
        payload = text.removeprefix("[harness: result] ")
        body = pretty_json(payload)
        try:
            result = json.loads(payload)
            summary = (
                "Tool result · "
                + str(result.get("operation", ""))
                + (" · success" if result.get("success") else " · failed")
            )
        except (ValueError, AttributeError):
            pass
    return {"kind": "trace", "summary": compact(summary), "body": body}


class Journal:
    """Bounded ordered replay, shared by tabs connected to this one local session."""

    def __init__(self, capacity: int = 2000) -> None:
        self.records: deque[dict[str, Any]] = deque(maxlen=capacity)
        self.number = 0
        self.changed = asyncio.Event()

    def append(self, record: dict[str, Any]) -> None:
        self.number += 1
        self.records.append({"id": self.number, **record})
        self.changed.set()

    async def stream(self, after: int) -> AsyncIterator[str]:
        while True:
            self.changed.clear()
            if self.records and after < self.records[0]["id"] - 1:
                after = self.records[0]["id"] - 1
                yield (
                    "data: "
                    + json.dumps(
                        {
                            "kind": "trace",
                            "summary": "Older events expired from server memory",
                            "body": (
                                "Only the latest 2000 records are replayed. "
                                "Use --trace-file for a full trace."
                            ),
                        }
                    )
                    + "\n\n"
                )
            pending = [record for record in self.records if record["id"] > after]
            for record in pending:
                after = record["id"]
                yield f"id: {after}\ndata: {json.dumps(record, ensure_ascii=False)}\n\n"
            try:
                await asyncio.wait_for(self.changed.wait(), timeout=15)
            except TimeoutError:
                yield ": keepalive\n\n"


class WebOutput(Output):
    """Keep the existing redaction/file path, then render complete records for the browser."""

    def __init__(self, journal: Journal, trace_file: Path | None, limit: int) -> None:
        self.buffer = io.StringIO()
        super().__init__(self.buffer, trace_file, limit)
        self.journal = journal
        self.context_window: int | None = None

    def statistics(self, payload: dict[str, Any]) -> None:
        self.journal.append(
            {
                "kind": "statistics",
                "statistics": {**payload, "context_window": self.context_window},
            }
        )

    def capture(self, text: str) -> str:
        super().emit(text)
        value = self.buffer.getvalue().removesuffix("\n")
        self.buffer.seek(0)
        self.buffer.truncate()
        return value

    def emit(self, text: str) -> None:
        with self.lock:
            self.journal.append(display_record(self.capture(text)))

    def event(self, text: str, *, kind: str, payload: Any) -> None:
        with self.lock:
            # Learn secrets in structured arguments before emitting any display/file text.
            payload = self.redactor.structure(payload)
            safe = self.capture(text)
            if kind == "action":
                summary = (
                    f"Tool call · {payload['operation']} · "
                    f"{compact(pretty(payload['arguments']), 90)}"
                )
            else:
                summary = f"Mode text · {payload['active_mode']} · {compact(payload['text'], 90)}"
            summary = self.redactor.text(summary)
            body = self.redactor.text(
                f"Active mode: {payload['active_mode']}\n\n{payload['text']}"
                if kind == "mode"
                else pretty(payload)
            )
            if len(body) > self.limit:
                body = safe  # use the normal bounded, redacted diagnostic fallback
            self.journal.append({"kind": "trace", "summary": compact(summary), "body": body})

    def user(self, text: str) -> None:
        with self.lock:
            self.journal.append({"kind": "user", "summary": "You", "body": self.capture(text)})


@asynccontextmanager
async def open_runner(args: argparse.Namespace, output: WebOutput) -> AsyncIterator[Runner]:
    store = FileCredentialStore()
    temporary = None
    backend = None
    provider = None
    trace = Trace(output, enabled=not args.no_trace, frame_limit=args.trace_limit)
    try:
        async with trace.client(timeout=60) as client:
            resolve = credential_resolver(store, client, output)
            if args.workspace is None:
                temporary = tempfile.mkdtemp(prefix="tau-mode-experiment-")
                workspace = Path(temporary)
            else:
                workspace = args.workspace.expanduser().resolve(strict=True)
                if not workspace.is_dir():
                    raise RuntimeError("--workspace must be an existing directory")
            output.emit(
                "[harness: WARNING] UNSAFE LOCAL EXECUTION: generated code can read/modify "
                "the host, access host secrets, and use the network. NOT SANDBOXED."
            )
            output.emit(
                f"[harness: startup]\nworkspace: {workspace}\nprotocol: {args.protocol}\n"
                f"model: {args.model}\nreasoning: {args.reasoning}; "
                f"summary: {args.reasoning_summary}\n"
                f"limits per turn: requests={args.max_requests}, actions={args.max_actions}, "
                f"recoveries={args.max_recoveries}, wall={args.wall_time}s; "
                f"action={args.action_timeout}s; output={args.output_limit}\n"
                f"workspace retained: {bool(args.workspace or args.keep_workspace)}\n"
                "Reconstructed application-level trace, not a packet capture. Auth fields are "
                "redacted; prompts/code/workspace data are NOT safe to publish. "
                "Do not submit secrets."
            )
            backend = Backend(
                workspace, timeout=args.action_timeout, output_limit=args.output_limit
            )
            provider = OpenAICodexProvider(
                OpenAICodexConfig(
                    credential_resolver=resolve,
                    reasoning_effort=None if args.reasoning == "default" else args.reasoning,
                    reasoning_summary=args.reasoning_summary,
                ),
                client=client,
            )
            credential = store.get_oauth("openai-codex")
            catalog = cached_codex_model_catalog(
                account_id=credential.account_id if credential else None
            )
            if args.discover_models:
                trace.association = "model discovery"
                catalog = await provider.discover_models()
                output.emit(
                    "[harness: discovered models] " + ", ".join(m.id for m in catalog.models)
                )
            output.context_window = args.context_window
            if output.context_window is None and catalog is not None:
                advertised = catalog.model(args.model)
                if advertised is not None and advertised.limits is not None:
                    output.context_window = advertised.limits.effective_context_window
            if output.context_window is None:
                entry = builtin_provider_entry("openai-codex")
                if entry is not None:
                    metadata = entry.model_metadata.get(args.model)
                    output.context_window = (metadata.context_window if metadata else None) or (
                        entry.context_windows or {}
                    ).get(args.model)
            yield Runner(
                provider,
                backend,
                output,
                protocol=args.protocol,
                model=args.model,
                limits=Limits(
                    args.max_requests, args.max_actions, args.max_recoveries, args.wall_time
                ),
                trace=trace,
                completed=trace.completed,
            )
    finally:
        if provider is not None:
            await provider.aclose()
        if backend is not None:
            await backend.close()
        if temporary is not None:
            if args.keep_workspace:
                print(f"Retained workspace: {temporary}")
            else:
                shutil.rmtree(temporary)


class Prompt(BaseModel):
    text: str = Field(min_length=1, max_length=100000)


def create_app(
    args: argparse.Namespace,
    *,
    token: str | None = None,
    runner_context: Callable[..., Any] = open_runner,
) -> FastAPI:
    if not args.allow_unsafe_local:
        raise RuntimeError("Local execution is UNSANDBOXED. Pass --allow-unsafe-local to opt in.")
    if (
        args.trace_file is not None
        and args.trace_file.resolve() == FileCredentialStore().path.resolve()
    ):
        raise RuntimeError("Trace destination must not be the credential store.")
    token = token or secrets.token_urlsafe(32)
    journal = Journal()
    active: asyncio.Task[Any] | None = None
    runner: Runner | None = None
    output: WebOutput | None = None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        nonlocal runner, output
        output = WebOutput(journal, args.trace_file, args.trace_limit)
        output.context_window = args.context_window
        try:
            async with runner_context(args, output) as instance:
                runner = instance
                output.statistics(runner.statistics())
                try:
                    yield
                finally:
                    if active is not None:
                        active.cancel()
                        with suppress(asyncio.CancelledError):
                            await active
        finally:
            output.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]"])

    async def authorize(request: Request) -> None:
        supplied = request.query_params.get("token", "")
        if not secrets.compare_digest(supplied, token):
            raise HTTPException(403, "Use the private URL printed by the server.")
        origin = request.headers.get("origin")
        if origin and urlsplit(origin).netloc != request.headers.get("host"):
            raise HTTPException(403, "Cross-origin access is not allowed.")

    auth = [Depends(authorize)]

    @app.middleware("http")
    async def private_headers(request: Request, call_next: Callable[..., Any]) -> Any:
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; connect-src 'self'; "
            "img-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        return response

    @app.get("/", dependencies=auth, response_class=HTMLResponse)
    async def index() -> str:
        return Path(__file__).with_name("web.html").read_text(encoding="utf-8")

    @app.get("/state", dependencies=auth)
    async def state() -> dict[str, Any]:
        return {
            "protocol": args.protocol,
            "model": args.model,
            "workspace": str(runner.backend.workspace.path) if runner else "",
            "mode": runner.mode if runner else None,
            "busy": active is not None and not active.done(),
            "statistics": {
                **(runner.statistics() if runner else {}),
                "context_window": output.context_window if output else None,
            },
        }

    @app.get("/events", dependencies=auth)
    async def events(request: Request) -> StreamingResponse:
        try:
            after = max(0, int(request.headers.get("last-event-id", "0")))
        except ValueError:
            raise HTTPException(400, "Invalid event cursor") from None
        return StreamingResponse(
            journal.stream(after),
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no"},
        )

    async def turn(text: str) -> None:
        assert runner is not None
        try:
            await runner.turn(text)
        finally:
            journal.append({"kind": "status", "busy": False, "mode": runner.mode})

    @app.post("/prompt", dependencies=auth, status_code=202)
    async def prompt(body: Prompt) -> dict[str, bool]:
        nonlocal active
        if not body.text.strip():
            raise HTTPException(422, "Prompt must not be blank.")
        if active is not None and not active.done():
            raise HTTPException(409, "A turn is already running.")
        assert output is not None
        output.user(body.text)
        journal.append({"kind": "status", "busy": True})
        active = asyncio.create_task(turn(body.text))
        return {"accepted": True}

    @app.post("/cancel", dependencies=auth)
    async def cancel() -> dict[str, bool]:
        cancelled = active is not None and not active.done()
        if cancelled:
            active.cancel()
        return {"cancelled": cancelled}

    return app


def serve(args: argparse.Namespace) -> None:
    if args.port > 65535:
        raise RuntimeError("--port must be between 1 and 65535")
    token = secrets.token_urlsafe(32)
    app = create_app(args, token=token)
    host = "[::1]" if args.host == "::1" else args.host
    print(
        "UNSANDBOXED local agent. Keep this private URL secret; do not expose or tunnel the server."
    )
    print(f"Open http://{host}:{args.port}/?token={token}", flush=True)
    # Access logs would copy the capability token into logs on every request.
    # Persistent browser SSE connections must not hold shutdown open indefinitely.
    uvicorn.run(app, host=args.host, port=args.port, access_log=False, timeout_graceful_shutdown=3)
