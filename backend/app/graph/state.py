"""Shared state passed between LangGraph nodes."""
from __future__ import annotations

from typing import Any, TypedDict


class AgentState(TypedDict, total=False):
    # Input
    question: str                 # current user question
    history: list[dict[str, str]] # prior turns: [{"user":..., "assistant":...}, ...]
    schema: str                   # schema block + value hints (injected once per run)
    # "normal" = the short, fast pipeline. Anything else (including unset) = the full pipeline,
    # so an old client or a malformed request always gets the thorough path, never the weak one.
    mode: str

    # Orchestrator output
    route: str                    # "sql" | "clarify" | "chitchat"
    needs_analysis: bool          # does the answer need computation/prediction?
    multi_step: bool              # does it need several queries (use the Planner/ReAct loop)?
    wants_chart: bool             # did the user ASK for a chart/graph? (never inferred)
    # Does answering need the earlier turns? Gates whether the five DOWNSTREAM agents are
    # shown the transcript at all (see history_for in nodes/_common.py).
    is_followup: bool
    chart_type: str               # "bar" | "line" | "donut" | "auto" (user's stated preference)
    plan: str                     # brief note of what data to fetch
    clarify_question: str         # question to ask the user (route == clarify)

    # Planner / multi-query ReAct loop
    next_action: str              # "query" | "answer"
    sub_question: str             # current sub-question for the SQL Author
    query_count: int              # number of successful queries run this turn
    steps: list[dict[str, Any]]   # [{sub_question, sql, columns, rows, truncated}]
    # Steps the Verifier rejected. Kept rather than deleted: the data really was retrieved, and
    # an answer hedged with "this could not be verified" beats silently answering from nothing.
    rejected_steps: list[dict[str, Any]]
    # Normalized SQL already attempted this turn. Re-emitting one is pointless (identical SQL
    # returns identical data), so it short-circuits instead of burning another verify cycle.
    tried_sql: list[str]

    # Current query working fields. Note the result set itself is NOT held here — it lives only
    # inside `steps`, so it is stored once per query rather than duplicated per turn.
    sql: str
    validation_error: str
    exec_error: str
    # SQL regenerations for the current sub-question. Shared budget (MAX_SQL_RETRIES) for ALL
    # reasons a query gets rewritten: an unsafe query, a DB execution error, or the Verifier
    # deciding the data doesn't support answering the question — one counter, one concept.
    retry_count: int

    # Finalize. The Verifier now runs BEFORE the Synthesizer (see build.py): it checks the
    # retrieved data + analysis against the question, not a written answer, so it can send a
    # real fix back to SQL Author instead of asking the Synthesizer to reword unfixable data.
    analysis: str                 # LLM analysis of the retrieved data
    verify_ok: bool               # does the retrieved data (+ analysis) support answering the question?
    verify_feedback: str          # if not: fed back to the SQL Author, not the Synthesizer
    # Verifier-triggered SQL rewrites used. Deliberately SEPARATE from retry_count: sharing one
    # budget let unrelated earlier syntax errors leave the Verifier with zero rewrites available.
    verify_retry_count: int
    answer: str                   # final answer to the user — written once, after verification passes

    # Chart (built deterministically from `steps` — never from LLM-written numbers)
    chart: dict[str, Any]         # ChartSpec-shaped dict, or {} when nothing is chartable
