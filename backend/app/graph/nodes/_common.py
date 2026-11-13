"""Small formatting helpers shared by nodes."""
from __future__ import annotations

from typing import Any

from app.config import settings
from app.graph.state import AgentState


def format_history(history: list[dict[str, str]] | None, max_turns: int | None = None) -> str:
    """Render the last few turns for multi-turn context (e.g. 'now group by month').

    max_turns defaults to settings.history_turns (HISTORY_TURNS in .env). It used to be a hardcoded
    3 that silently overrode the API's own MAX_HISTORY_TURNS=6, so raising that constant had no
    effect — the tighter of the two always won. One setting now governs the whole chain.
    """
    if not history:
        return ""
    limit = settings.history_turns if max_turns is None else max_turns
    recent = history[-limit:] if limit > 0 else []
    if not recent:
        return ""
    lines = ["CONVERSATION SO FAR:"]
    for turn in recent:
        u = turn.get("user", "").strip()
        a = turn.get("assistant", "").strip()
        if u:
            lines.append(f"User: {u}")
        if a:
            lines.append(f"Assistant: {a}")
    lines.append("")
    return "\n".join(lines)


def history_for(state: AgentState) -> str:
    """The transcript for a DOWNSTREAM agent - empty unless this turn is a follow-up.

    Most questions stand on their own, and for those the transcript is dead weight in five
    prompts - two of which (sql_author, verifier) also carry the whole schema block and run on
    the MAIN model tier. Gating on the Orchestrator's is_followup flag keeps history where it
    earns its cost and drops it everywhere else.

    The Orchestrator must NOT use this: it calls format_history() directly, because it is the
    node that MAKES this decision and has to read the transcript to make it.

    Fails OPEN by construction - orchestrator_node defaults the flag to True, so an absent or
    unparseable verdict behaves exactly as it did before this gate existed.
    """
    if not state.get("is_followup"):
        return ""
    return format_history(state.get("history"))


def rows_to_markdown(columns: list[str], rows: list[list[Any]], max_rows: int = 100) -> str:
    """Render a result set as a compact markdown table (truncated for display)."""
    if not columns:
        return "(no columns)"
    if not rows:
        return "(no rows returned)"
    shown = rows[:max_rows]
    header = "| " + " | ".join(str(c) for c in columns) + " |"
    sep = "| " + " | ".join("---" for _ in columns) + " |"
    body = [
        "| " + " | ".join("" if v is None else str(v) for v in row) + " |"
        for row in shown
    ]
    table = "\n".join([header, sep, *body])
    if len(rows) > max_rows:
        table += f"\n\n(showing first {max_rows} of {len(rows)} rows)"
    return table


def render_steps(steps: list[dict[str, Any]] | None, max_rows: int = 200) -> str:
    """Render every gathered query step (SQL + its data) for the analyst/synthesizer.

    Includes an explicit truncation warning so the LLM never treats a capped sample as the
    whole picture.
    """
    if not steps:
        return "(no data retrieved)"
    blocks: list[str] = []
    for i, step in enumerate(steps, 1):
        header = f"### Query {i}: {step.get('sub_question', '')}".rstrip()
        sql = step.get("sql", "")
        table = rows_to_markdown(step.get("columns", []), step.get("rows", []), max_rows=max_rows)
        warn = ""
        if step.get("truncated"):
            warn = (
                "\n\n⚠ NOTE: this result was capped — you are seeing only a sample of the rows. "
                "Do NOT compute totals/averages/counts by reading these rows; rely on SQL "
                "aggregates instead, or state that the figure is based on a partial sample."
            )
        blocks.append(f"{header}\nSQL:\n{sql}\n\nRESULT:\n{table}{warn}")
    return "\n\n".join(blocks)
