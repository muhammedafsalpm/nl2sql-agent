"""SQL Author agent — writes one read-only T-SQL SELECT for the current sub-question."""
from __future__ import annotations

import re

from app.graph.nodes._common import history_for
from app.graph.prompts import SQL_AUTHOR_SYSTEM
from app.graph.state import AgentState
from app.llm import chat
from app.observability import get_logger
from app.utils import extract_sql

log = get_logger()

_WS = re.compile(r"\s+")


def normalize_sql(sql: str) -> str:
    """Whitespace/case-insensitive form, for recognising a query we have already run.

    Only used to detect repeats — never to alter what is executed. Collapse whitespace BEFORE
    trimming the trailing semicolon, otherwise "SELECT 1 ;" keeps a trailing space and compares
    unequal to "SELECT 1".
    """
    return _WS.sub(" ", (sql or "")).strip().rstrip(";").strip().lower()


def _already_gathered(steps: list[dict]) -> str:
    if not steps:
        return ""
    done = "; ".join(f"'{s.get('sub_question','')}'" for s in steps)
    return f"\nALREADY RETRIEVED (do not repeat these): {done}\n"


def sql_author_node(state: AgentState) -> dict:
    target = state.get("sub_question") or state["question"]
    parts = [
        "SCHEMA (only these tables/columns exist — never use others):",
        state.get("schema", ""),
        "",
        history_for(state),
        _already_gathered(state.get("steps", [])),
        f"WRITE SQL FOR THIS DATA REQUEST: {target}",
    ]
    # Carry the orchestrator's plan (data needed + requested result size) on the single-query path.
    if not state.get("sub_question") and state.get("plan"):
        parts.append(f"PLAN (data needed + requested result size): {state['plan']}")
    # A chart needs a breakdown to plot; a scalar cannot be charted.
    if state.get("wants_chart"):
        # A donut/pie can only show ONE metric's share per slice. "one or more" numeric columns
        # is correct for a bar chart but was letting the model add a helpful extra column (e.g. a
        # computed percentage) for a donut request — which then forces a downgrade to a bar chart,
        # since two numeric columns can't both be plotted as one ring.
        if state.get("chart_type") == "donut":
            numeric_rule = (
                "EXACTLY ONE numeric column (the value each slice represents) — do NOT add a "
                "computed percentage/share column, the chart itself renders each slice's share"
            )
        else:
            numeric_rule = "one or more NUMERIC columns (the values)"
        parts.append(
            "\nTHE USER ASKED FOR A CHART. Return a SHAPE THAT CAN BE PLOTTED:\n"
            f"- exactly ONE label column first (the category or date to plot along the axis), then\n"
            f"  {numeric_rule}.\n"
            "- GROUP BY the label column — never return a single scalar row for a chart.\n"
            "- Keep it small and readable: aggregate, and use TOP 20 ordered by the value for a\n"
            "  category breakdown (a chart with hundreds of categories is unreadable).\n"
            "- Give every column a clear alias — the aliases become the axis and legend labels."
        )
    if state.get("validation_error"):
        parts.append(
            "\nYOUR PREVIOUS SQL WAS REJECTED BY THE SAFETY VALIDATOR:\n"
            f"{state['validation_error']}\nRewrite it as a single safe SELECT."
        )
    if state.get("exec_error"):
        parts.append(
            "\nYOUR PREVIOUS SQL FAILED WHEN RUN. Database error:\n"
            f"{state['exec_error']}\nFix the query (check table/column names, joins, types)."
        )
    if state.get("verify_feedback"):
        # Show the exact SQL being criticised. Without it the writer had to infer which query the
        # feedback referred to, and frequently re-emitted the same one.
        previous = state.get("sql", "")
        parts.append(
            "\nAN INDEPENDENT VERIFIER REVIEWED YOUR PREVIOUS SQL AND REJECTED IT."
            + (f"\n\nYOUR PREVIOUS SQL WAS:\n{previous}" if previous else "")
            + f"\n\nWHAT TO FIX:\n{state['verify_feedback']}\n"
            "Write a DIFFERENT query that addresses this. Do NOT resubmit the same SQL — it would "
            "return the same data and be rejected again. If you believe the previous SQL was "
            "already correct, make the smallest change that satisfies the feedback."
        )
    if state.get("tried_sql"):
        parts.append(
            f"\nYou have already tried {len(state['tried_sql'])} quer(y/ies) this turn. Each new "
            "attempt must be materially different from the previous ones."
        )
    parts.append("\nWrite the T-SQL now as ONE ```sql block.")

    # Normal mode writes SQL on the cheap tier: measured 8/8 identical results on simple questions
    # at ~1.2s vs ~4.2s. Any other mode (including unset) keeps the main model exactly as before.
    sql = extract_sql(
        chat(
            SQL_AUTHOR_SYSTEM,
            "\n".join(p for p in parts if p),
            temperature=0.0,
            fast=state.get("mode") == "normal",
        )
    )
    log.info("SQL[q%d try%d]: %s", state.get("query_count", 0) + 1, state.get("retry_count", 0), sql.replace("\n", " "))
    # `verify_feedback` is deliberately NOT cleared here. It is the Verifier's semantic
    # instruction, and it stays valid until the Verifier next rules on the rewrite. Clearing it
    # at this point dropped it the moment the rewrite tripped the validator or the database: the
    # NEXT attempt then saw only the syntax error, lost the reason the query was rejected, and
    # was free to drift back to the SQL that had already been rejected. The Verifier clears it
    # itself once it has judged the new query.
    return {
        "sql": sql,
        "validation_error": "",
        "exec_error": "",
        "tried_sql": state.get("tried_sql", []) + [normalize_sql(sql)],
    }
