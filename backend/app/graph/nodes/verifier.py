"""Independent Verifier agent — checks the retrieved data BEFORE the answer is written.

Runs after Executor/Analysis and before Synthesize. This is deliberate: it means a rejection can
send a real fix back to the SQL Author (new SQL, new data) instead of asking the Synthesizer to
reword an answer built on data that was already wrong — which it has no way to actually fix.
"""
from __future__ import annotations

from app.config import settings
from app.graph.nodes._common import history_for, render_steps
from app.graph.prompts import VERIFIER_SYSTEM
from app.graph.sqlcheck import check as sql_concerns
from app.graph.state import AgentState
from app.llm import chat
from app.observability import get_logger
from app.utils import extract_json

log = get_logger()

# Sentinel: extract_json hands back this exact object when it cannot parse a verdict, so a
# parse failure is distinguishable from a genuine {"ok": true}.
_UNREADABLE: dict = {"__unreadable__": True}

# Feedback is re-sent to the SQL Author on every rewrite, so it needs a bound. But a hard slice
# is dangerous: at 700 chars it was cutting mid-word, and in one real case the cut landed inside
# the "Fix:" instruction — the single part the SQL Author is supposed to act on. It received a
# dangling fragment. The cap is now generous enough to rarely bind, and it always cuts at a
# sentence or line boundary so the surviving text is a complete instruction.
MAX_FEEDBACK_CHARS = 1500


def _trim_feedback(text: str) -> str:
    """Bound the feedback without ever truncating an instruction mid-sentence.

    Cuts back to the last sentence end or line break inside the budget, so whatever survives is
    something the SQL Author can actually act on. Falls back to a word boundary if the text has
    no punctuation at all.
    """
    text = (text or "").strip()
    if len(text) <= MAX_FEEDBACK_CHARS:
        return text
    window = text[:MAX_FEEDBACK_CHARS]
    # Prefer a sentence end, then a line break, then a word break — whichever is latest.
    cut = max(window.rfind(". "), window.rfind(".\n"), window.rfind("\n"), window.rfind("; "))
    if cut < MAX_FEEDBACK_CHARS // 2:  # no usable boundary late enough; fall back to a word break
        cut = window.rfind(" ")
    if cut <= 0:
        cut = MAX_FEEDBACK_CHARS
    return window[: cut + 1].rstrip() + " […truncated]"


def verifier_node(state: AgentState) -> dict:
    """Reports a verdict only — never decides whether the retry budget allows another attempt.

    That decision belongs to build.py's routing (_route_after_verifier), exactly like the
    Validator and Executor never decide their own retry budget either. Keeping it there means
    one place (settings.max_sql_retries) governs every reason a query gets regenerated.
    """
    steps = state.get("steps", [])
    # Nothing to check yet (e.g. an honest failure answer is about to be written) → accept.
    if not steps:
        log.info("verify: skipped - nothing retrieved to check yet")
        return {"verify_ok": True}

    # Ordering matters for cost: the schema block is ~14k chars and byte-identical on every call,
    # so it goes FIRST where a provider can prefix-cache it. Everything below it varies per turn
    # and would defeat that cache if placed above.
    #
    # History matters for correctness: without it the reviewer cannot resolve a follow-up like
    # "how many are completed?" and rejects a correct query as "ambiguous" — which the SQL Author
    # cannot fix, because the query was right all along.
    schema = state.get("schema", "")
    parts = []
    if schema:
        parts.append(
            "DATABASE SCHEMA (the ONLY tables/columns that exist — judge the SQL against this):\n"
            f"{schema}"
        )
    history = history_for(state)
    if history:
        parts.append(history.rstrip())
    parts.append(f"USER QUESTION: {state['question']}")
    parts.append(f"EXECUTED SQL + RETRIEVED DATA:\n{render_steps(steps, max_rows=settings.max_rows)}")
    if state.get("analysis"):
        parts.append(f"ANALYST'S FINDINGS:\n{state['analysis']}")

    # The deterministic check used to run ONLY in the fast graph — the mode with no reviewer —
    # and its findings were thrown away. The wiring was inverted: the mode with a reviewer never
    # saw the evidence. Running it here (rather than as a node) keeps build_graph()'s routing
    # provably untouched, as its own comment requires.
    #
    # This matters because VERIFIER_SYSTEM tells this agent not to reject on a hunch. These
    # concerns are not hunches: each one is derived from the schema's own FK lines and
    # "MANY ROWS PER" markers, so they give it something factual to rule on. They are ADJUDICATED,
    # not obeyed — the check is conservative and can flag a query that is actually fine.
    #
    # Wrapped: `check()` is the raw function (only sqlcheck's own log_check() guards itself), and
    # an advisory signal must never be the reason a turn fails — the same rule that module states.
    concerns: list[str] = []
    try:
        for step in steps:
            concerns.extend(sql_concerns(step.get("sql", ""), schema))
    except Exception as exc:  # noqa: BLE001
        log.warning("verify: deterministic check skipped (%s)", exc)
        concerns = []
    if concerns:
        unique = list(dict.fromkeys(concerns))  # de-dup across steps, keep order
        log.info("verify: %d deterministic concern(s) handed to the reviewer", len(unique))
        parts.append(
            "DETERMINISTIC CONCERNS TO ADJUDICATE (derived from the schema's own foreign keys and "
            "row-per-key markers, NOT from an opinion — decide whether each one is real for this "
            "question, and reject only if it genuinely affects the answer):\n"
            + "\n".join(f"- {c}" for c in unique)
        )

    parts.append(
        "Does this data (and analysis, if any) support answering the user's question? Reply "
        "with the JSON verdict."
    )
    user = "\n\n".join(parts)
    raw = chat(VERIFIER_SYSTEM, user, temperature=0.0)
    data = extract_json(raw, default=_UNREADABLE)

    # Fail CLOSED. Defaulting an unreadable verdict to ok=True let a malformed reply (a trailing
    # comma is enough) silently turn a rejection into an approval, with nothing in the log.
    if data is _UNREADABLE or "ok" not in data:
        log.warning(
            "verify: verdict unreadable (%d chars) - failing closed, not approving", len(raw or "")
        )
        return _reject(
            state,
            "The automated review could not be completed. Re-check the query against the "
            "schema and business rules and rewrite it if anything looks wrong.",
        )

    ok = bool(data.get("ok"))
    if ok:
        log.info("verify: ok")
        # Clearing it HERE, not in the SQL Author, is what lets the feedback survive a failed
        # rewrite attempt (see the note on sql_author_node's return). This is the point at which
        # it is genuinely spent: the rewrite has now been judged, and approved.
        return {"verify_ok": True, "verify_feedback": ""}

    feedback = str(data.get("feedback", ""))
    log.info("verify: rejected -> %s", feedback)
    return _reject(state, feedback)


def _reject(state: AgentState, feedback: str) -> dict:
    """Move the rejected step aside; never destroy it.

    It used to be deleted outright, which caused two bugs at once:
      * `_already_gathered()` in the SQL Author reads `steps`, so deleting the step erased the
        only record that this query had been tried — the SQL Author then re-emitted byte-identical
        SQL, which was rejected again, three times, for 72 seconds.
      * When the budget ran out with `steps` empty, the Synthesizer's honest-failure branch could
        not fire (it keys off error strings, which are cleared on the verifier path), so it wrote
        a confident answer from "(no data retrieved)".

    Keeping it in `rejected_steps` lets the Synthesizer answer honestly WITH a caveat instead of
    answering from nothing. `analysis` is cleared because it described the rows being set aside.
    """
    # The Verifier returns ONE verdict over ALL gathered steps, with no index saying which one it
    # objected to. Assuming "the last one" was a guess, and a wrong guess is harmful: on a
    # multi-query turn it deleted an innocent step and kept the faulty one, which then wrote the
    # answer. With more than one step we cannot know, so set them all aside and re-gather.
    steps = state.get("steps", [])
    return {
        "verify_ok": False,
        "verify_feedback": _trim_feedback(feedback),
        "steps": [],
        "rejected_steps": state.get("rejected_steps", []) + steps,
        "analysis": "",
        "verify_retry_count": state.get("verify_retry_count", 0) + 1,
        # Both budgets are reset because this rejection DISCARDS the gathered steps above: the
        # work has to be redone, so it must be funded. Leaving them alone caused two failures.
        #
        # retry_count: unrelated syntax errors earlier in the turn could have already spent it,
        # leaving the Verifier's rewrite with no attempts for a fixable error — the Verifier
        # correctly identified a problem and the fix was denied budget for an unrelated reason.
        #
        # query_count: on a multi-step turn, `steps` is cleared but the count was not, so the
        # Planner hit `query_count >= max_queries` and could NOT re-gather the sub-questions
        # this function just set aside. The turn then answered from a fraction of the required
        # data — and, if the Verifier approved that fragment, labelled it verified.
        #
        # Neither reset is unbounded: verify_retry_count (above) still caps how many times this
        # path can be taken at all, so total work stays bounded by verify_retries × max_queries.
        "retry_count": 0,
        "query_count": 0,
    }
