"""Shared runtime: the schema block and the compiled agent graph.

Both are expensive to build, so they are created once at startup and reused for every
request - exactly what the CLI does per process.

The agent graph is SYNCHRONOUS and a turn can take tens of seconds, so `run_turn` is always
executed in a worker thread. Never call `graph.invoke` directly from an async endpoint: it
would block the event loop and serialise every other request behind it.
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Callable

from app.config import settings
from app.db.connection import ping
from app.db.introspect import build_schema_text, build_value_hints
from app.graph.build import build_fast_graph, build_graph
from app.llm import get_usage_report, start_usage_tracking
from app.tracing import callbacks as tracing_callbacks
from app.observability import get_logger

log = get_logger()

RECURSION_LIMIT = 60


def _log_usage_table() -> None:
    """Log this turn's LLM usage, one row per agent. Never raises."""
    try:
        rows = get_usage_report()
        if not rows:
            return  # chitchat/clarify with no model call, or a turn that failed before any call
        log.info("LLM usage this turn:")
        log.info(
            "  %-13s %-26s %5s %8s %8s %8s",
            "Agent", "Model", "Calls", "Input", "Output", "Total",
        )
        t_calls = t_in = t_out = 0
        for r in rows:
            log.info(
                "  %-13s %-26s %5d %8d %8d %8d",
                r["agent"], r["model"], r["calls"], r["input"], r["output"],
                r["input"] + r["output"],
            )
            t_calls += r["calls"]
            t_in += r["input"]
            t_out += r["output"]
        log.info(
            "  %-13s %-26s %5d %8d %8d %8d",
            "TOTAL", "", t_calls, t_in, t_out, t_in + t_out,
        )
    except Exception:  # noqa: BLE001 - accounting must never break a turn
        pass


class Runtime:
    def __init__(self) -> None:
        self.schema: str = ""
        self.graph: Any = None        # full pipeline — "deep" mode
        self.fast_graph: Any = None   # short pipeline — "normal" mode
        self.db_ok: bool = False
        self.db_message: str = "not checked"

    @property
    def ready(self) -> bool:
        return bool(self.schema) and self.graph is not None and self.db_ok

    def startup(self) -> None:
        """Check the DB, load the schema, compile the graph. Never raises."""
        log.info(
            "API startup: provider=%s model=%s fast_model=%s (%s)",
            settings.llm_provider,
            settings.active_model,
            settings.fast_model,
            "active for routing"
            if settings.fast_model != settings.active_model
            else "not set - routing uses the main model",
        )
        self.db_ok, self.db_message = ping()
        if not self.db_ok:
            log.error("API startup: database unavailable: %s", self.db_message)
            return

        try:
            schema = build_schema_text()
            hints = build_value_hints()
            self.schema = schema + (("\n\n" + hints) if hints else "")
            log.info("API startup: schema loaded (%d chars)", len(self.schema))
        except Exception as exc:  # noqa: BLE001
            log.exception("API startup: could not load schema")
            self.db_message = f"schema load failed: {exc}"
            return

        try:
            self.graph = build_graph()
            self.fast_graph = build_fast_graph()
            log.info("API startup: agent graphs compiled (full + fast)")
        except Exception:  # noqa: BLE001
            log.exception("API startup: could not build the agent graph")

        self._warm_llm()

    def _warm_llm(self) -> None:
        """Pay the provider client's one-time cost at boot instead of on the first question.

        MEASURED: the first main-tier call in a process took 34.0s, every later one 2.2s. The cost
        is process-wide, not per-client - a second client with different settings came back in
        2.2s - so it is the deferred `from langchain_openai import ChatOpenAI` inside get_llm()
        plus the first TLS/DNS round trip, not anything per-agent. One call per tier covers it.

        Without this the whole 30-odd seconds lands on whoever asks the first question, which
        looks like the app is broken rather than warming up.

        Both tiers are warmed because they are separate clients and the FAST one runs first in
        every turn (the Orchestrator routes before anything else). Warming only the main tier
        would leave the first real question still paying for the fast client.

        Never raises and never blocks readiness: a provider that is down at boot must not stop the
        server starting, since /api/ready reports on the DATABASE and schema. Set LLM_WARMUP=false
        to skip it - useful in tests, where it would be a real network call per process.
        """
        if os.getenv("LLM_WARMUP", "true").strip().lower() in ("0", "false", "no"):
            log.info("API startup: LLM warm-up skipped (LLM_WARMUP=false)")
            return
        from langchain_core.messages import HumanMessage

        from app.llm import get_llm

        probe = [HumanMessage(content="ok")]
        start = time.perf_counter()
        for fast in (True, False):
            tier = "fast" if fast else "main"
            try:
                # Deliberately NOT chat(): that would record the call in the per-turn token
                # accounting and name a Langfuse span after an agent that never ran.
                get_llm(0.0, fast).invoke(probe)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "API startup: %s-tier LLM warm-up failed (%s) - the first question will pay "
                    "the client setup instead", tier, str(exc)[:120],
                )
                return
        log.info("API startup: LLM clients warmed in %.1fs", time.perf_counter() - start)

    def _invoke(
        self,
        question: str,
        history: list[dict[str, str]],
        mode: str,
        on_node: Callable[[str], None] | None = None,
    ) -> dict:
        # Token accounting is per-thread, and this method IS the worker thread that makes every
        # chat() call for this turn — so both starting it and reporting it must happen here, not
        # in run_turn() (a different thread, which would see an empty report).
        start_usage_tracking()
        try:
            # Only the explicit string "normal" takes the short path. Anything else — including an
            # unset or unexpected value from an old client — gets the full pipeline.
            fast = mode == "normal" and self.fast_graph is not None
            graph = self.fast_graph if fast else self.graph
            payload = {
                "question": question,
                "history": history,
                "schema": self.schema,
                "mode": "normal" if fast else "deep",
            }
            config = {"recursion_limit": RECURSION_LIMIT}
            # Optional Langfuse tracing. Added to the config dict that ALL THREE execution paths
            # below already share (invoke, stream, and the stream-unavailable fallback), so one
            # line instruments every path. When tracing is off this list is empty and `config`
            # is exactly what it was before — the graph sees no difference at all.
            handlers = tracing_callbacks()
            if handlers:
                config["callbacks"] = handlers

            if on_node is None:
                return graph.invoke(payload, config=config)

            # Stream so the caller learns which stage is running and can show real progress rather
            # than a timer. Falls back to a plain invoke if streaming is unavailable, because
            # progress reporting must never be the reason a turn fails.
            progressed = False
            try:
                final: dict = {}
                for kind, chunk in graph.stream(
                    payload, config=config, stream_mode=["updates", "values"]
                ):
                    progressed = True
                    if kind == "updates" and isinstance(chunk, dict):
                        for node in chunk:
                            on_node(node)
                    elif kind == "values" and isinstance(chunk, dict):
                        final = chunk
                if final:
                    return final
                log.warning("stream produced no final state - falling back to invoke")
            except Exception as exc:  # noqa: BLE001
                # The fallback exists for a runtime that cannot stream AT ALL. Once chunks have
                # arrived, streaming plainly works — so the failure came from inside the graph, and
                # re-running is pointless: the same prompt fails at the same step. Observed on a
                # context-length failure, the entire turn re-ran (orchestrator, SQL author,
                # validator, and a second 500-row query) and failed again 7s later, doubling the
                # latency, the LLM spend and the database load before the user saw anything.
                if progressed:
                    raise
                log.warning("stream unavailable (%s) - falling back to invoke", exc)
            return graph.invoke(payload, config=config)
        finally:
            # `finally`, so the table still appears for a turn that raised — seeing what a failed
            # turn spent is exactly when the numbers are most useful.
            _log_usage_table()

    async def run_turn(
        self,
        question: str,
        history: list[dict[str, str]],
        mode: str = "deep",
        on_node: Callable[[str], None] | None = None,
    ) -> tuple[dict, float]:
        """Run one agent turn in a worker thread. Returns (result, elapsed_seconds).

        `on_node` is called from the worker thread as each graph step completes, so it must be
        thread-safe (the SSE endpoint hands it a queue).
        """
        start = time.perf_counter()
        result = await asyncio.to_thread(self._invoke, question, history, mode, on_node)
        elapsed = time.perf_counter() - start
        log.info("API turn done in %.2fs (%s mode)", elapsed, mode)
        return result, elapsed


runtime = Runtime()
