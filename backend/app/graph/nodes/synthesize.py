"""Synthesizer agent — writes the final answer, once, from data the Verifier already approved."""
from __future__ import annotations

from app.config import settings
from app.graph.nodes._common import history_for, render_steps
from app.graph.prompts import SYNTHESIZE_SYSTEM
from app.graph.state import AgentState
from app.llm import chat
from app.observability import get_logger
from app.utils import strip_think

log = get_logger()


def synthesize_node(state: AgentState) -> dict:
    """Write the final answer. Three distinct situations, three honest treatments.

    The key rule: an answer must never *sound* more certain than the pipeline actually is. A turn
    whose data failed verification used to reach here indistinguishable from a clean one.
    """
    steps = state.get("steps", [])
    rejected = state.get("rejected_steps", [])
    # Read the Verifier's ACTUAL verdict. Deriving it from which list happens to be empty was
    # wrong on multi-step turns: one surviving step made a rejected turn look verified.
    # `verify_ok` is unset only on routes that never reach the Verifier at all (no data path).
    unverified = not state.get("verify_ok", True)

    # 1. Nothing retrieved at all → say so plainly, never fabricate.
    if not steps and not rejected:
        err = state.get("exec_error") or state.get("validation_error") or "no query returned data"
        user = (
            f"USER QUESTION: {state['question']}\n\n"
            f"No data could be retrieved after retries. Technical detail: {err}\n\n"
            "Tell the user honestly (1-2 sentences) that you couldn't retrieve this and suggest "
            "rephrasing. Do NOT fabricate an answer."
        )
        answer = strip_think(
            chat(SYNTHESIZE_SYSTEM, user, temperature=0.2, fast=state.get("mode") == "normal")
        )
        log.info("synthesize: no data retrieved - writing an honest failure answer")
        # Explicit: the API treats a MISSING verify_ok as verified, so an empty-handed turn must
        # say so rather than rely on a default.
        return {"answer": answer, "verify_ok": False}

    # 2. Choose the data. When verification passed, any earlier rejected step was superseded by
    #    the successful rewrite — including it would resurrect data we already know was wrong.
    #    When it failed, show everything we actually have, flagged.
    usable = steps if not unverified else (steps + rejected)

    parts = [history_for(state).rstrip(), f"USER QUESTION: {state['question']}"]
    parts.append(f"\nRETRIEVED DATA:\n{render_steps(usable, max_rows=settings.max_rows)}")
    if state.get("analysis"):
        parts.append(f"\nANALYST'S FINDINGS:\n{state['analysis']}")
    if unverified:
        # Deliberately does NOT include the reviewer's own words: they name tables and columns,
        # which this persona is forbidden to reveal to a business user.
        parts.append(
            "\n⚠ THIS DATA DID NOT PASS INDEPENDENT VERIFICATION.\n"
            "Answer using this data, but you MUST open with one short plain sentence telling the "
            "user the figures could not be fully verified and should be treated as indicative. "
            "Do not explain the technical reason, and do not hide the numbers."
        )
    parts.append("\nWrite the final answer to the user now.")

    answer = strip_think(
        chat(
            SYNTHESIZE_SYSTEM,
            "\n".join(p for p in parts if p),
            temperature=0.2,
            fast=state.get("mode") == "normal",
        )
    )
    log.info(
        "synthesize: drafted %s answer (%d chars) from %d step(s)",
        "UNVERIFIED" if unverified else "verified",
        len(answer),
        len(usable),
    )
    # Do NOT return verify_ok — that would overwrite the Verifier's verdict with our own guess.
    return {"answer": answer}
