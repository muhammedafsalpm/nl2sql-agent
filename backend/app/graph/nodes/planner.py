"""Planner agent — the ReAct controller: decide whether to run another query or answer."""
from __future__ import annotations

from typing import Any

from app.config import settings
from app.graph.nodes._common import history_for
from app.graph.prompts import PLANNER_SYSTEM
from app.graph.state import AgentState
from app.llm import chat
from app.observability import get_logger
from app.utils import extract_json

log = get_logger()

# Preview caps. The Planner has to judge whether the right data came back, which row/column
# COUNTS alone cannot tell it — but a result can hold MAX_ROWS rows, so the sample is hard-capped
# rather than sized by the result. The rows are already in state; nothing extra is fetched.
PREVIEW_ROWS = 3
PREVIEW_COLS = 8
PREVIEW_CELL = 30


def _cell(value: Any) -> str:
    text = "" if value is None else " ".join(str(value).split())
    return text if len(text) <= PREVIEW_CELL else text[: PREVIEW_CELL - 3] + "..."


def _gathered_summary(steps: list[dict]) -> str:
    if not steps:
        return "Nothing gathered yet."
    lines: list[str] = []
    for i, s in enumerate(steps, 1):
        columns = list(s.get("columns") or [])
        rows = list(s.get("rows") or [])
        # The true size stays on its own line — the sample below must never stand in for it.
        lines.append(f"{i}. '{s.get('sub_question','')}' -> {len(rows)} rows x {len(columns)} cols")
        if not columns:
            continue
        shown = columns[:PREVIEW_COLS]
        extra = f" (+{len(columns) - len(shown)} more)" if len(columns) > len(shown) else ""
        lines.append(f"   columns: {', '.join(str(c) for c in shown)}{extra}")
        for row in rows[:PREVIEW_ROWS]:
            lines.append("   sample:  " + " | ".join(_cell(v) for v in list(row)[:PREVIEW_COLS]))
        if len(rows) > PREVIEW_ROWS:
            lines.append(f"   ... {len(rows) - PREVIEW_ROWS} more row(s) not shown")
    return "\n".join(lines)


def planner_node(state: AgentState) -> dict:
    steps = state.get("steps", [])

    # Must gather at least once before answering.
    if not steps:
        log.info("planner: first query")
        return {
            "next_action": "query",
            "sub_question": state.get("plan") or state["question"],
            "retry_count": 0,
            "validation_error": "",
            "exec_error": "",
            # A new sub-question starts clean, exactly like the two error fields above. The SQL
            # Author no longer clears this itself (it must survive a failed rewrite of the SAME
            # query), so without this a rejection on the PREVIOUS sub-question would be shown as
            # criticism of a query that no longer exists.
            "verify_feedback": "",
        }

    user = (
        history_for(state)
        + f"USER QUESTION: {state['question']}\n\n"
        f"DATA GATHERED SO FAR:\n{_gathered_summary(steps)}\n\n"
        f"(You have run {state.get('query_count', 0)} of max {settings.max_queries} queries.)\n"
        "Decide whether to run one more focused query or to answer now."
    )
    data = extract_json(chat(PLANNER_SYSTEM, user, temperature=0.0, fast=True), default={"action": "answer"})
    action = str(data.get("action", "answer")).lower()

    if action == "query" and state.get("query_count", 0) < settings.max_queries:
        sub_q = str(data.get("sub_question") or state["question"])
        log.info("planner: another query -> %s", sub_q)
        return {
            "next_action": "query",
            "sub_question": sub_q,
            "retry_count": 0,
            "validation_error": "",
            "exec_error": "",
            "verify_feedback": "",  # new sub-question starts clean — see the note above
        }
    log.info("planner: answer")
    return {"next_action": "answer"}
