"""Terminal-only routes that need no data: clarify and chitchat."""
from __future__ import annotations

import re

from app.graph.prompts import CHITCHAT_SYSTEM
from app.graph.state import AgentState
from app.llm import chat
from app.observability import get_logger
from app.utils import strip_think

log = get_logger()

# Fallback only. The Orchestrator is now required to supply a specific clarify_question naming what
# is missing, so this should rarely appear — but it is what the user reads when it does, and the
# previous wording ("Could you clarify what exactly you'd like to know?") was the single most
# repeated reply in the logs: every route=clarify turn with an empty question produced it verbatim.
_GENERIC_CLARIFY = (
    "I'd like to get this right — could you tell me a little more about what you're looking for?"
)

# Readable entity names taken from the introspected schema. Nothing here is specific to any one
# database: the names come from whatever schema is loaded, so they follow the configured database.
#
# Selection matters more than it looks. Taking the first few tables in schema order returned
# 'sysdiagrams' and '<table>_bak' copies — names that would produce example questions the
# database cannot answer. Three generic filters fix it without naming any table:
#   * prefer non-`dbo` schemas: when a database has curated schemas, `dbo` is usually where
#     system objects, staging and hand-made copies end up. But many databases keep EVERYTHING in
#     `dbo`, so it is skipped only when another schema actually has tables;
#   * skip names that look like a backup, copy or system object;
#   * rank what remains by column count, since a link table has two or three columns while a real
#     business entity has many.
_TABLE_BLOCK_RE = re.compile(r"^TABLE\s+(\S+)\.(\S+)(.*?)(?=^TABLE\s|\Z)", re.MULTILINE | re.DOTALL)
_COLUMN_RE = re.compile(r"^\s+-\s+\S+", re.MULTILINE)
_SKIP_SCHEMAS = {"dbo"}
_SKIP_NAME_RE = re.compile(r"(^sys)|(_(bak|backup|copy|old|tmp|temp|test|archive)\d*$)", re.I)
# Plumbing suffixes that carry no meaning for a person reading an example question.
_DROP_SUFFIXES = (" master", " mstr", " mstr tbl", " tbl", " table")
_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _entity_names(schema: str, limit: int = 4) -> list[str]:
    scored: list[tuple[int, str]] = []
    seen: set[str] = set()
    blocks = _TABLE_BLOCK_RE.findall(schema or "")
    has_curated = any(sch.lower() not in _SKIP_SCHEMAS for sch, _, _ in blocks)
    skip = _SKIP_SCHEMAS if has_curated else set()
    for sch, table, body in blocks:
        if sch.lower() in skip or _SKIP_NAME_RE.search(table):
            continue
        # snake_case and CamelCase both read as words: "order_item" / "OrderItem" -> "order item".
        pretty = _CAMEL_RE.sub(" ", table).replace("_", " ")
        pretty = " ".join(pretty.split()).lower()
        for suffix in _DROP_SUFFIXES:
            if pretty.endswith(suffix):
                pretty = pretty[: -len(suffix)].strip()
        if not pretty or pretty in seen:
            continue
        seen.add(pretty)
        scored.append((len(_COLUMN_RE.findall(body)), pretty))
    # Richest tables first; stable on ties so the list does not shuffle between restarts.
    scored.sort(key=lambda t: -t[0])
    return [name for _, name in scored[:limit]]


def clarify_node(state: AgentState) -> dict:
    q = state.get("clarify_question") or _GENERIC_CLARIFY
    log.info("clarify: asking -> %s", q)
    return {"answer": q}


def chitchat_node(state: AgentState) -> dict:
    """Reply to a message that needs no data.

    The question used to be sent alone, which left the model unable to name anything real — so it
    invented examples from a generic business domain ("total sales this month", "customer
    satisfaction") that do not exist in this database and would fail if a user asked them. Passing
    the entity names read from the live schema keeps any example concrete and correct; when no
    schema is loaded the prompt falls back to describing kinds of question instead.
    """
    parts = [f"USER MESSAGE: {state['question']}"]
    entities = _entity_names(state.get("schema", ""))
    if entities:
        parts.append(
            "ENTITY NAMES IN THIS DATABASE (use these if you give an example; never invent "
            f"others): {', '.join(entities)}"
        )
    # 0.6 rather than 0.4: the reply must not read as the same stock paragraph every time, and the
    # prompt now forbids a fixed opening — a little more variation supports that.
    answer = strip_think(chat(CHITCHAT_SYSTEM, "\n\n".join(parts), temperature=0.6, fast=True))
    log.info("chitchat: replied (%d chars)", len(answer))
    return {"answer": answer}
