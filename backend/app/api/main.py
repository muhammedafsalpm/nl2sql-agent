"""FastAPI application exposing the natural-language-to-SQL assistant over HTTP.

Run with:
    uvicorn app.api.main:app --host 0.0.0.0 --port 8000

Docs at /docs. The agent graph, prompts, guards and DB layer are reused unchanged; the
terminal CLI (`python main.py`) is unaffected.

The API is STATELESS: there is no server-side session store. For a follow-up question the
client replays the previous turns in `history`, so any number of API instances can serve any
request.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse

from app.api.runtime import runtime
from pydantic import ValidationError

from app.api import export as export_svc
from app.api import export_formats as fmt
from app.api.schemas import (
    ChartSpec,
    ChatRequest,
    ChatResponse,
    DataPayload,
    ExportRequest,
    HealthResponse,
    InfoResponse,
    QueryStep,
    ReadyResponse,
)
from app.config import settings
from app.graph.nodes.finalize import _entity_names
from app.observability import get_logger, setup_logging
from app.tracing import flush as tracing_flush

API_KEY = os.getenv("API_KEY", "").strip()
CORS_ORIGINS = [
    o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",") if o.strip()
]
# How many previous exchanges reach the graph. Governed by ONE setting (HISTORY_TURNS) rather than
# a constant here: the value that used to live at this line said 6 while the agents actually saw 3,
# because format_history() capped tighter downstream and the smaller number always won.


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Show the colour agent trace in the server console by default; set LOG_CONSOLE=false
    # to keep the terminal to uvicorn's HTTP lines only. The file log is written either way.
    setup_logging(
        console=os.getenv("LOG_CONSOLE", "true").strip().lower() not in ("0", "false", "no")
    )
    if API_KEY:
        # The frontend only sends X-API-Key when its own VITE_API_KEY (baked in at build time)
        # matches this value - nothing else reminds you to update+rebuild it, so every request
        # would start failing with 401 with no obvious cause. Warn once, loudly, at startup.
        get_logger().warning(
            "API_KEY is set - the frontend must have a matching VITE_API_KEY in frontend/.env "
            "(rebuilt after any change), or every request will fail with 401."
        )
    # Startup work is blocking (DB + introspection), so keep it off the event loop.
    await asyncio.to_thread(runtime.startup)
    yield
    # Langfuse batches trace sends on a background thread, so without this the last few traces
    # are lost when the server stops. No-op when tracing is disabled, and never raises.
    tracing_flush()


app = FastAPI(
    title=f"{settings.app_name} API",
    description=(
        "Natural-language questions over a SQL Server database. Read-only (SELECT only): "
        "every generated query passes a deterministic SELECT-only validator before execution."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """No-op when API_KEY is unset (local dev); enforced as soon as it is set."""
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing X-API-Key."
        )


# ── Optional voice layer (Speech-to-Text + Text-to-Speech) ──────────────────────
# Additive: it WRAPS the chat flow rather than changing it - speech becomes text, that text goes
# through the same /api/chat endpoints below, and the answer text is spoken back. No node in the
# agent graph is aware voice exists.
#
# Registered here, after require_api_key, because it reuses that same dependency: voice inherits
# the app's auth instead of inventing its own.
#
# The try/except is deliberate. An OPTIONAL feature must never be the reason the whole API fails
# to start - that would be worse than not having the feature. If anything in the voice package
# cannot import, every chat/export/health route still works exactly as before and voice is simply
# absent, which is the same thing the frontend sees when /api/voice/status reports unavailable.
try:
    from app.voice import router as _voice_router

    app.include_router(_voice_router, dependencies=[Depends(require_api_key)])
except Exception as _voice_exc:  # noqa: BLE001
    get_logger().info("voice: routes not registered (%s)", _voice_exc)


def _require_ready() -> None:
    if not runtime.ready:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Service not ready: {runtime.db_message}",
        )


def _history(req: ChatRequest) -> list[dict[str, str]]:
    """Client-supplied turns, in the shape the agent graph expects."""
    turns = req.history[-settings.history_turns:] if settings.history_turns > 0 else []
    return [{"user": t.user, "assistant": t.assistant} for t in turns]


def _chart(result: dict) -> ChartSpec | None:
    """The graph emits {} when nothing could honestly be plotted."""
    spec = result.get("chart") or None
    if not spec:
        return None
    try:
        return ChartSpec(**spec)
    except ValidationError:
        get_logger().warning("chart: dropped a malformed spec: %s", spec)
        return None


def _export_token(result: dict) -> str | None:
    """Sign the SQL this answer was built from, so the client can ask for it as a file.

    Uses the LAST step, matching the convention `_build_response` already follows for `data`.
    Computed OUTSIDE the include_sql branch on purpose: the SQL drawer is off by default, but
    export must still work — and the token is opaque, so it leaks no internals.
    """
    steps = (result.get("steps") or []) + (result.get("rejected_steps") or [])
    if not steps:
        return None  # chitchat / clarify / a turn that retrieved nothing: nothing to export
    return export_svc.make_token(steps[-1].get("sql", ""))


def _build_response(req: ChatRequest, result: dict, elapsed: float) -> ChatResponse:
    # Include steps the Verifier set aside: on an unverified turn they ARE the data the answer was
    # written from, so hiding them would make the SQL view disagree with the answer.
    steps_raw = (result.get("steps") or []) + (result.get("rejected_steps") or [])
    resp = ChatResponse(
        answer=result.get("answer") or "(no answer produced)",
        route=result.get("route", "sql"),
        needs_analysis=bool(result.get("needs_analysis", False)),
        # Non-sql routes never run verification, so they are reported as verified.
        verified=bool(result.get("verify_ok", True)),
        mode=req.mode,
        latency_seconds=round(elapsed, 2),
        chart=_chart(result),
        export_token=_export_token(result),
    )
    if req.include_sql:
        resp.steps = [
            QueryStep(
                sub_question=s.get("sub_question", ""),
                sql=s.get("sql", ""),
                row_count=len(s.get("rows") or []),
                truncated=bool(s.get("truncated")),
            )
            for s in steps_raw
        ]
        if steps_raw:
            last = steps_raw[-1]
            resp.data = DataPayload(
                columns=list(last.get("columns") or []),
                rows=[list(r) for r in (last.get("rows") or [])],
                truncated=bool(last.get("truncated")),
            )
    return resp


# Graph step -> what the user is told. Deliberately plain: it names the ACTIVITY, never a table,
# column, query or agent name. Steps not listed here (internal checks) emit nothing, so the
# previous message simply stays on screen.
STAGE_LABELS = {
    "orchestrator": "Understanding your question",
    "planner": "Planning the steps",
    "sql_author": "Preparing the query",
    "validator": "Checking the query is safe",
    "executor": "Fetching your data",
    "analysis": "Analysing the results",
    "verifier": "Verifying the numbers",
    "synthesize": "Writing the answer",
    "chart": "Building the chart",
}


# What the user is shown when a turn fails. The raw provider/driver text still goes to the log,
# where it is useful — but it must never reach a business user: it names models, token counts and
# internal parameters, and reads as a crash rather than an answer. Observed in production:
# "Error code: 400 - {'error': {'message': "This model's maximum context length is 128000
# tokens..." rendered straight into the chat bubble.
#
# The three recognised cases are the ones a user can actually act on, so each says what to do
# differently rather than only apologising.
_TOO_MUCH_DATA = (
    "context_length_exceeded", "maximum context length", "context length",
    "reduce the length of the messages", "too many tokens", "exceed the configured limit",
    # An oversized request is ALSO reported as HTTP 429 rate_limit_exceeded — "Request too large
    # for <model> ... on tokens per min (TPM): Limit 200000, Requested 290757. The input or output
    # tokens must be reduced in order to run successfully." (an OpenAI error, observed in practice)
    # It must be classified here, not as a rate limit: waiting changes nothing, the request will
    # fail identically every time, and only a narrower question gets through.
    "request too large", "must be reduced",
)
_TOOK_TOO_LONG = ("timeout", "timed out", "deadline exceeded")
# Genuine throttling only — checked AFTER _TOO_MUCH_DATA so an oversized request never lands here.
# A real TPM exhaustion says "Rate limit reached ... Please try again in 1.2s", which retrying does
# resolve.
_TOO_BUSY = ("rate limit reached", "rate_limit", "429", "overloaded", "server_error", "503")


def _friendly_error(exc: Exception) -> str:
    text = str(exc).lower()
    if any(m in text for m in _TOO_MUCH_DATA):
        return (
            "That question brought back more information than I can work through in one go. "
            "Could you narrow it down a little — for example a particular record, category or date "
            "range, or a count rather than the full list?"
        )
    if any(m in text for m in _TOOK_TOO_LONG):
        return (
            "That one took longer than I could wait for, so I stopped rather than leave you "
            "hanging. Please try again — a more specific question usually comes back quickly."
        )
    if any(m in text for m in _TOO_BUSY):
        return (
            "I'm dealing with a lot of requests just now and couldn't get to that one. Please "
            "give it a few seconds and try again."
        )
    return (
        "Sorry — something went wrong on my side while I was working on that, so I couldn't "
        "finish it. Please try again in a moment; if it keeps happening, rephrasing the question "
        "slightly often gets through."
    )


# ── Chat ────────────────────────────────────────────────────────────────────────

@app.post("/api/chat", response_model=ChatResponse, dependencies=[Depends(require_api_key)])
async def chat(req: ChatRequest) -> ChatResponse:
    """Ask a question. Send previous turns in `history` for follow-ups."""
    _require_ready()
    try:
        result, elapsed = await runtime.run_turn(req.question, _history(req), req.mode)
    except Exception as exc:  # noqa: BLE001
        get_logger().exception("API chat failed")
        raise HTTPException(status_code=500, detail=_friendly_error(exc)) from exc
    return _build_response(req, result, elapsed)


@app.post("/api/chat/stream", dependencies=[Depends(require_api_key)])
async def chat_stream(req: ChatRequest) -> StreamingResponse:
    """Same as /api/chat but streams progress as Server-Sent Events.

    The agent graph is not token-streaming, so this emits coarse progress events while the
    turn runs and then the final answer - enough to keep a UI responsive during a long turn.
    """
    _require_ready()
    history = _history(req)

    async def events():
        def sse(event: str, payload: dict) -> str:
            return f"event: {event}\ndata: {json.dumps(payload)}\n\n"

        yield sse("status", {"stage": "Understanding your question", "seconds": 0})

        # The graph runs in a worker thread and reports each completed step here. A plain queue
        # bridges the two: the thread puts step names in, this async loop takes them out.
        steps: asyncio.Queue[str] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def on_node(name: str) -> None:
            loop.call_soon_threadsafe(steps.put_nowait, name)

        task = asyncio.create_task(runtime.run_turn(req.question, history, req.mode, on_node))

        # Report real progress as it happens; the timer is only a fallback so the connection
        # stays open and the user still sees movement between slow steps.
        started = asyncio.get_running_loop().time()
        while not task.done():
            try:
                node = await asyncio.wait_for(steps.get(), timeout=2.0)
                label = STAGE_LABELS.get(node)
                if label:
                    yield sse("status", {
                        "stage": label,
                        "seconds": int(asyncio.get_running_loop().time() - started),
                    })
            except asyncio.TimeoutError:
                if not task.done():
                    yield sse("status", {
                        "seconds": int(asyncio.get_running_loop().time() - started),
                    })

        try:
            result, elapsed = task.result()
        except Exception as exc:  # noqa: BLE001
            get_logger().exception("API stream failed")
            yield sse("error", {"message": _friendly_error(exc)})
            return

        if req.include_sql:
            for s in result.get("steps") or []:
                yield sse("sql", {"sql": s.get("sql", "")})

        chart = _chart(result)
        if chart is not None:
            yield sse("chart", chart.model_dump())

        yield sse(
            "done",
            {
                "answer": result.get("answer") or "",
                "route": result.get("route", "sql"),
                "verified": bool(result.get("verify_ok", True)),
                "mode": req.mode,
                "latency_seconds": round(elapsed, 2),
                "chart": chart.model_dump() if chart is not None else None,
                # None on chitchat/clarify, so the client can show an Export control purely on
                # "did this turn produce data?" without special-casing any route.
                "export_token": _export_token(result),
            },
        )

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Export ──────────────────────────────────────────────────────────────────────

_FILENAME_SAFE = re.compile(r"[^a-z0-9]+")
# File-name prefix from APP_NAME ("Sales Insights" -> "sales-insights"), so downloads are
# recognisable without the code naming any product.
_FILE_PREFIX = _FILENAME_SAFE.sub("-", settings.app_name.lower()).strip("-")[:30] or "export"


def _filename(question: str, ext: str) -> str:
    """A readable, filesystem-safe name derived from the question."""
    stem = _FILENAME_SAFE.sub("-", (question or "export").lower()).strip("-")[:50] or "export"
    return f"{_FILE_PREFIX}-{stem}.{ext}"


@app.post("/api/export", dependencies=[Depends(require_api_key)])
async def export(req: ExportRequest):
    """Download the data behind an answer, in full.

    This does NOT re-run the agent graph and makes NO model call: it re-executes the exact SQL the
    answer was built from and formats the rows. `MAX_ROWS` is deliberately not applied — that cap
    exists to keep rows out of the LLM prompt, which is irrelevant to a file.
    """
    _require_ready()
    try:
        sql = export_svc.read_token(req.token)
    except export_svc.ExportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    content_type, ext = fmt.FORMATS[req.format]
    # pyodbc is synchronous and an export can run for minutes: on the event loop it would stall
    # every other request. Same rule runtime.py follows for the graph.
    try:
        columns, rows, capped = await asyncio.to_thread(export_svc.fetch_all, sql)
    except export_svc.ExportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        get_logger().exception("export failed")
        raise HTTPException(status_code=500, detail=_friendly_error(exc)) from exc

    notes = [
        fmt.cap_note(capped, len(rows), settings.export_max_rows),
        fmt.unverified_note(req.verified),
    ]
    title = (req.question or "Data export").strip()[:120]
    headers = {"Content-Disposition": f'attachment; filename="{_filename(req.question, ext)}"'}

    # Both writers build the file in memory and are CPU-bound, so they go to a thread too.
    try:
        if req.format == "xlsx":
            body = await asyncio.to_thread(fmt.to_xlsx, columns, rows, notes, title)
        else:
            body = await asyncio.to_thread(fmt.to_docx, columns, rows, notes, title, req.answer)
    except ImportError as exc:
        # A missing optional library must name the format that is unavailable, not 500.
        raise HTTPException(
            status_code=501,
            detail=f"{req.format.upper()} export is not available on this server ({exc}).",
        ) from exc
    except Exception as exc:  # noqa: BLE001
        get_logger().exception("export: %s formatting failed", req.format)
        raise HTTPException(
            status_code=500, detail=f"The {req.format.upper()} file could not be generated."
        ) from exc

    return Response(content=body, media_type=content_type, headers=headers)


# ── Health / info ───────────────────────────────────────────────────────────────

@app.get("/api/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Liveness: the process is up. Always 200 while serving."""
    return HealthResponse(status="ok")


@app.get("/api/ready", response_model=ReadyResponse)
async def ready() -> ReadyResponse:
    """Readiness: DB reachable, schema loaded, graph compiled."""
    body = ReadyResponse(
        status="ready" if runtime.ready else "not_ready",
        database="ok" if runtime.db_ok else runtime.db_message,
        llm="configured" if settings.active_model else "not configured",
        schema_loaded=bool(runtime.schema),
        schema_chars=len(runtime.schema),
    )
    if not runtime.ready:
        raise HTTPException(status_code=503, detail=body.model_dump())
    return body


def _entities(schema: str, limit: int = 6) -> list[str]:
    """Readable entity names taken from the live schema's table names.

    Lets a client suggest starter questions without hardcoding anything about this database:
    point the app at a different one and the suggestions follow. Uses the SAME selection as the
    chitchat agent (richest real tables first, backups/system tables skipped), so the UI and the
    assistant never suggest different things.
    """
    return _entity_names(schema, limit=limit)


@app.get("/api/info", response_model=InfoResponse, dependencies=[Depends(require_api_key)])
async def info() -> InfoResponse:
    """Non-secret runtime configuration."""
    return InfoResponse(
        app_name=settings.app_name,
        app_description=settings.app_description,
        provider=settings.llm_provider,
        model=settings.active_model,
        fast_model=settings.fast_model,
        fast_model_enabled=settings.fast_model != settings.active_model,
        database=settings.db_name,
        allowed_schemas=list(settings.allowed_schemas),
        entities=_entities(runtime.schema),
        max_rows=settings.max_rows,
        max_queries=settings.max_queries,
    )
