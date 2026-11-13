"""Configurable LLM client shared by all agents.

Provider is chosen by LLM_PROVIDER in .env:
  - "local" (default) -> Qwen3 (or any Ollama model) via Ollama
  - "open" / "openai"  -> OpenAI (OPENAI_MODEL, e.g. gpt-5-mini) via OPENAI_API_KEY

`fast=True` uses the optional small routing model (OLLAMA_FAST_MODEL / OPENAI_FAST_MODEL) when set;
otherwise it falls back to the main model, so enabling it is purely opt-in.

Nothing else in the app changes — every agent calls chat()/get_llm() here, so switching the
provider or model tier is a pure config change.
"""
from __future__ import annotations

import threading
import time
from functools import lru_cache

from langchain_core.messages import HumanMessage, SystemMessage

from app.config import settings
from app.observability import get_logger


def _is_openai() -> bool:
    return settings.llm_provider.lower() in ("open", "openai")


# ── Logging: which agent ran on which model ─────────────────────────────────────
# Every node passes its prompt constant from app.graph.prompts unchanged, so the calling
# agent can be recognised by object identity here. This keeps the model trace in one place:
# no node, prompt or graph code has to change.
_PROMPT_LABELS: dict[int, str] | None = None


def _agent_label(system: str) -> str:
    global _PROMPT_LABELS
    if _PROMPT_LABELS is None:
        try:
            from app.graph import prompts as p  # deferred: avoids any import cycle

            _PROMPT_LABELS = {
                id(p.ORCHESTRATOR_SYSTEM): "orchestrator",
                id(p.PLANNER_SYSTEM): "planner",
                id(p.SQL_AUTHOR_SYSTEM): "sql_author",
                id(p.ANALYSIS_SYSTEM): "analysis",
                id(p.SYNTHESIZE_SYSTEM): "synthesize",
                id(p.VERIFIER_SYSTEM): "verifier",
                id(p.CHITCHAT_SYSTEM): "chitchat",
            }
        except Exception:  # noqa: BLE001 - logging must never break a request
            _PROMPT_LABELS = {}
    return _PROMPT_LABELS.get(id(system), "agent")


# ── Per-agent reasoning tuning (MAIN TIER ONLY) ─────────────────────────────────
# (reasoning_effort, verbosity) per agent, keyed on the label _agent_label() already derives from
# the prompt constant's identity. Living here means no node, no prompt and no graph code changes.
#
# ⚠ NEVER sent on a fast-tier call. The fast model is not a reasoning model and rejects both
# parameters, so a single shared setting would break every routing call.
#
# The pairing is deliberately INVERSE, because in this pipeline the hardest-thinking agents emit
# the SHORTEST output:
#   sql_author  high/low     - one SELECT. A wrong query costs a whole lap (validator, executor,
#                              analysis, verifier) and then repeats, so effort here SAVES time.
#   verifier    high/low     - the accuracy gate; emits {"ok":..,"feedback":..} under 400 chars.
#                              Low verbosity is also correctness: it fails CLOSED, so a malformed
#                              verdict is read as a rejection and burns a retry for nothing.
#   analysis    medium/medium- reasons, but over at most MAX_ROWS rows, and the verifier re-checks
#                              its arithmetic. Its output is injected into BOTH the verifier and
#                              synthesizer prompts, so every extra sentence is paid three times.
#   synthesize  low/medium   - instruction-following over already-verified data; it derives
#                              nothing. Its prompt fights verbosity in a dozen places.
#   orchestrator low/low   - DEEP MODE ONLY; normal mode still routes on the fast tier and so
#                            sends neither parameter. It classifies one short message into a
#                            fixed JSON object, so there is nothing to reason about at
#                            length - it is here because the cheap model was measurably
#                            worse at that classification, not because it needs depth. Low
#                            verbosity because the reply IS that JSON and nothing else.
# Agents absent from this map (planner, chitchat) are hardcoded fast=True and so never reach
# a reasoning model at all.
_AGENT_TUNING: dict[str, tuple[str, str]] = {
    "sql_author": ("high", "low"),
    "verifier": ("high", "low"),
    "analysis": ("medium", "medium"),
    "synthesize": ("low", "medium"),
    "orchestrator": ("low", "low"),
}


def _tuning(system: str, fast: bool) -> tuple[str, str]:
    """(effort, verbosity) for this call. ("", "") means send neither parameter."""
    if fast or not _is_openai():
        return "", ""          # fast tier / Ollama: never send these
    effort, verbosity = _AGENT_TUNING.get(_agent_label(system), ("", ""))
    # A global setting overrides the table, so the whole feature can be forced or examined from
    # .env without editing code.
    return settings.reasoning_effort or effort, settings.reasoning_verbosity or verbosity


def active_model_for(fast: bool) -> str:
    """The model name a call with this tier will actually use."""
    return settings.fast_model if fast else settings.active_model


def _log_call(system: str, fast: bool, elapsed: float) -> None:
    try:
        model = active_model_for(fast)
        # Only label it "fast" when a distinct fast model is really configured.
        tier = " (fast)" if fast and model != settings.active_model else ""
        eff, verb = _tuning(system, fast)
        tune = f" [effort={eff} verbosity={verb}]" if (eff or verb) else ""
        get_logger().info(
            "llm: %s -> %s%s%s in %.1fs", _agent_label(system), model, tier, tune, elapsed
        )
    except Exception:  # noqa: BLE001
        pass


# ── Per-turn token accounting ───────────────────────────────────────────────────
# Every agent already funnels through chat(), so recording usage here needs no change to any
# node, to AgentState, or to the graph. The counts come from the provider's own response
# (`usage_metadata`), never estimated locally.
#
# Thread-local, because a turn already runs entirely inside one worker thread (see
# runtime.py's asyncio.to_thread). That gives one isolated accumulator per turn with no locking.
# The corollary matters: whoever starts tracking must also read the report FROM THE SAME THREAD.
# Reading it from the event loop after `await asyncio.to_thread(...)` returns would silently see
# an empty list, because that is a different thread with its own thread-local storage.
_usage = threading.local()


def start_usage_tracking() -> None:
    """Begin (or restart) accounting for the current thread.

    Clearing is required, not merely tidy: asyncio's default executor REUSES worker threads, so
    without this a turn would inherit the previous turn's counts on the same thread.
    """
    _usage.calls = []


def _record_usage(system: str, fast: bool, resp) -> None:
    """Append one call's usage. Never raises — accounting must not break a request."""
    try:
        calls = getattr(_usage, "calls", None)
        if calls is None:
            return  # tracking not started on this thread (CLI, eval, a one-off script)
        # Ollama does not always populate usage_metadata, so treat it as best-effort: a call that
        # reports nothing contributes 0 rather than breaking the table.
        usage = getattr(resp, "usage_metadata", None) or {}
        calls.append(
            {
                "agent": _agent_label(system),
                "model": active_model_for(fast),
                "input": usage.get("input_tokens") or 0,
                "output": usage.get("output_tokens") or 0,
            }
        )
    except Exception:  # noqa: BLE001
        pass


def get_usage_report() -> list[dict]:
    """One aggregated row per agent for the current thread's turn, in first-call order.

    Only agents that actually ran appear — the deterministic nodes (validator, executor, chart,
    clarify) never call an LLM, so they are absent by construction rather than shown as zero.
    """
    by_agent: dict[str, dict] = {}
    for call in getattr(_usage, "calls", []) or []:
        row = by_agent.setdefault(
            call["agent"],
            {"agent": call["agent"], "model": call["model"], "calls": 0, "input": 0, "output": 0},
        )
        row["calls"] += 1
        row["input"] += call["input"]
        row["output"] += call["output"]
        # The SQL Author and Synthesizer switch tier by mode, and a retry can land on a different
        # model than the first attempt. Show that instead of implying one model handled them all.
        if call["model"] not in row["model"].split(", "):
            row["model"] += ", " + call["model"]
    return list(by_agent.values())


@lru_cache(maxsize=64)
def get_llm(
    temperature: float | None = 0.0,
    fast: bool = False,
    effort: str = "",
    verbosity: str = "",
):
    """Return a cached chat model for the configured provider.

    temperature=None builds the client without a temperature override (fallback for OpenAI models
    that only accept their default temperature). fast=True selects the small routing model.

    `effort`/`verbosity` are part of the CACHE KEY, not just arguments: agents ask for different
    values, and with the key limited to (temperature, fast) the first caller's client would be
    handed to every other agent for the rest of the process.
    """
    if _is_openai():
        from langchain_openai import ChatOpenAI

        model = settings.fast_model if fast else settings.openai_model
        # Without a timeout a stalled provider request holds its worker thread forever, and each
        # thread also holds a DB connection. A handful of those and the service stops responding
        # while /api/health still reports OK.
        kwargs: dict = {
            "model": model,
            "api_key": settings.openai_api_key,
            "timeout": settings.llm_timeout,
            "max_retries": settings.llm_max_retries,
        }
        if temperature is not None:
            kwargs["temperature"] = temperature
        # Only ever set for main-tier calls — _tuning() returns "" for the fast tier.
        if effort:
            kwargs["reasoning_effort"] = effort
        if verbosity:
            kwargs["verbosity"] = verbosity
        return ChatOpenAI(**kwargs)

    # Default: local Ollama
    from langchain_ollama import ChatOllama

    model = settings.fast_model if fast else settings.llm_model
    # ChatOllama has no `timeout` field — passing one is silently dropped by pydantic, so the
    # timeout has to go to the underlying httpx client instead.
    kwargs = {
        "model": model,
        "base_url": settings.ollama_base_url,
        "num_ctx": settings.ollama_num_ctx,
        "client_kwargs": {"timeout": settings.llm_timeout},
    }
    if temperature is not None:
        kwargs["temperature"] = temperature
    return ChatOllama(**kwargs)


def chat(system: str, user: str, temperature: float = 0.0, fast: bool = False) -> str:
    """One-shot system+user call, returns the raw text content."""
    messages = [SystemMessage(content=system), HumanMessage(content=user)]
    # Names this call's span after the agent making it, so a trace reads
    # "orchestrator / sql_author / analysis / verifier / synthesize" instead of five identical
    # "ChatOpenAI" entries. Reuses the label _log_call already derives - nothing new to maintain.
    # Deferred import: app.tracing imports app.config, and importing it at module scope here
    # would add a cycle risk for no benefit.
    from app.tracing import enabled as _tracing_on

    # None when tracing is off, which is exactly what invoke() defaults to - so the disabled
    # path is byte-identical to the pre-Langfuse call.
    cfg = {"run_name": _agent_label(system)} if _tracing_on() else None
    effort, verbosity = _tuning(system, fast)
    # Skip the doomed first attempt entirely when the main model is known to accept only its
    # default temperature. Without this, Analysis and the Synthesizer (both temperature=0.2) pay a
    # REJECTED call plus the retry below on EVERY turn — two wasted round trips, invisible except
    # as latency. Set OPENAI_IGNORES_TEMPERATURE=true once the provider error confirms it.
    if settings.reasoning_ignores_temperature and not fast and _is_openai():
        temperature = None
    start = time.perf_counter()
    try:
        resp = get_llm(temperature, fast, effort, verbosity).invoke(messages, config=cfg)
    except Exception as exc:  # noqa: BLE001
        text = str(exc).lower()
        # Some OpenAI models (reasoning-style) reject a non-default temperature — retry without it.
        if _is_openai() and "temperature" in text:
            resp = get_llm(None, fast, effort, verbosity).invoke(messages, config=cfg)
        # A model that does not know reasoning_effort/verbosity must not take the agent down with
        # it: drop both and retry plainly, so a model swap degrades instead of failing the turn.
        elif _is_openai() and (effort or verbosity) and (
            "reasoning_effort" in text or "verbosity" in text or "unsupported" in text
        ):
            get_logger().warning(
                "llm: %s does not accept reasoning_effort/verbosity - retrying without them",
                active_model_for(fast),
            )
            resp = get_llm(temperature, fast).invoke(messages, config=cfg)
        else:
            raise
    _log_call(system, fast, time.perf_counter() - start)
    _record_usage(system, fast, resp)
    return resp.content if isinstance(resp.content, str) else str(resp.content)
