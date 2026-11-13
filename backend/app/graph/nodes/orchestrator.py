"""Orchestrator agent — understands the query, routes, and plans."""
from __future__ import annotations

import re
from functools import lru_cache

from app.graph.nodes._common import format_history
from app.graph.prompts import ORCHESTRATOR_SYSTEM
from app.graph.state import AgentState
from app.llm import chat
from app.observability import get_logger
from app.utils import extract_json

log = get_logger()

# The Orchestrator routes WITHOUT the schema — it sees only the user's words, so it cannot tell
# which of them name real things in this database and has to guess. It guessed inconsistently on
# equally-garbled questions, sending one to clarify and answering the other.
#
# These few lines settle it from evidence instead: the entity names are read out of the
# introspected schema, so they follow whatever database is configured. Nothing here is specific
# to any one database.
_TABLE_RE = re.compile(r"^TABLE\s+(\S+)", re.MULTILINE)
_WORD_RE = re.compile(r"[a-z][a-z0-9]*")
_MIN_TERM = 3


@lru_cache(maxsize=4)
def _entity_terms(schema: str) -> frozenset[str]:
    """Words naming a table or schema in the connected database. Cached — built once per process."""
    terms: set[str] = set()
    for name in _TABLE_RE.findall(schema):
        terms |= {p for p in re.split(r"[._]+", name.lower()) if len(p) >= _MIN_TERM}
    return frozenset(terms)


def _singular_forms(word: str):
    """The word plus the plural forms worth testing, so a plural matches a singular table name."""
    yield word
    if word.endswith("ies") and len(word) > 4:
        yield word[:-3] + "y"          # companies -> company
    if word.endswith("es") and len(word) > 3:
        yield word[:-2]                # statuses -> status
    if word.endswith("s") and len(word) > 3:
        yield word[:-1]                # clusters -> cluster


def names_known_entity(question: str, schema: str) -> str:
    """The first word in the question that names something in the database, else ""."""
    terms = _entity_terms(schema)
    if not terms:
        return ""
    for word in _WORD_RE.findall(question.lower()):
        if len(word) < _MIN_TERM:
            continue
        for form in _singular_forms(word):
            if form in terms:
                return form
    return ""


def orchestrator_node(state: AgentState) -> dict:
    log.info("Q: %s", state.get("question", ""))
    user = format_history(state.get("history")) + f"Current question: {state['question']}"
    # Deep mode routes on the MAIN model; normal mode keeps the cheap tier. Measured over 11
    # standalone/follow-up questions asked with a prior turn in history: gpt-4o-mini 8/11 at
    # 2.2s avg, the main model 11/11 at 2.7s avg. EVERY mini miss was a STANDALONE question
    # called a follow-up - it flips on history being PRESENT rather than on whether the
    # message refers back to it.
    #
    # This node also decides route, needs_analysis, multi_step and wants_chart, and a wrong
    # one of those wastes an entire turn - so +0.5s on a 20-70s deep turn is cheap insurance.
    # Normal mode is left alone deliberately: its whole premise is a ~2-3s answer, where the
    # same +0.5s is ~20%, and it runs neither the Planner, Analysis nor the Verifier that the
    # routing flags mostly steer.
    #
    # Same shape sql_author and synthesize already use, so mode drives every tier choice.
    raw = chat(ORCHESTRATOR_SYSTEM, user, temperature=0.0, fast=state.get("mode") == "normal")
    data = extract_json(
        raw,
        default={"route": "sql", "needs_analysis": True, "multi_step": False, "plan": "", "clarify_question": ""},
    )
    route = str(data.get("route", "sql")).lower()
    if route not in ("sql", "clarify", "chitchat"):
        route = "sql"

    # A question that names something the database actually contains IS answerable — that is hard
    # evidence, and it beats the model's guess about whether the wording felt vague.
    matched = ""
    if route == "clarify":
        matched = names_known_entity(state.get("question", ""), state.get("schema", ""))
        if matched:
            log.info("route override: clarify -> sql (question names '%s', which exists here)", matched)
            route = "sql"

    # Charts are opt-in: default False, so an older/oddly-shaped JSON never produces one.
    # Computed AFTER any override, because it is gated on route == "sql".
    wants_chart = bool(data.get("wants_chart", False)) and route == "sql"
    chart_type = str(data.get("chart_type", "auto")).lower()
    if chart_type not in ("bar", "line", "donut", "auto"):
        chart_type = "auto"
    # Does answering this message need the earlier turns? The five downstream agents read
    # history only when this is true (see history_for in nodes/_common.py).
    #
    # Fails OPEN: an absent or unparseable field keeps history flowing, which is exactly the
    # behaviour before this flag existed. The two errors are NOT symmetric - a wrong True
    # costs a few hundred tokens, while a wrong False hides the earlier turns from every
    # later step, so a request to re-present the previous answer silently comes back with a
    # DIFFERENT set of records than the one the user is looking at (SQL_RULES 15).
    #
    # With no history there is nothing to refer back to, so it is False by definition - which
    # also keeps the logged value honest on the first turn of a conversation.
    is_followup = bool(state.get("history")) and bool(data.get("is_followup", True))
    log.info(
        "route=%s needs_analysis=%s multi_step=%s wants_chart=%s%s followup=%s",
        route,
        data.get("needs_analysis"),
        data.get("multi_step"),
        wants_chart,
        f" ({chart_type})" if wants_chart else "",
        is_followup,
    )
    return {
        "route": route,
        "needs_analysis": bool(data.get("needs_analysis", True)),
        "multi_step": bool(data.get("multi_step", False)),
        "wants_chart": wants_chart,
        "chart_type": chart_type,
        "plan": str(data.get("plan", "")),
        # An overridden turn has no clarifying question to fall back on.
        "clarify_question": "" if matched else str(data.get("clarify_question", "")),
        "is_followup": is_followup,
        # reset per-turn working state
        "chart": {},
        "steps": [],
        "rejected_steps": [],
        "tried_sql": [],
        "query_count": 0,
        "retry_count": 0,
        "verify_retry_count": 0,
        "validation_error": "",
        "exec_error": "",
        "verify_feedback": "",
    }
