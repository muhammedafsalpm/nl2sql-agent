"""Analysis agent — LLM analyzes the retrieved data (calculations, predictions, trends)."""
from __future__ import annotations

from app.config import settings
from app.graph.nodes._common import history_for, render_steps
from app.graph.prompts import ANALYSIS_SYSTEM
from app.graph.state import AgentState
from app.llm import chat
from app.observability import get_logger
from app.utils import strip_think

log = get_logger()


def analysis_node(state: AgentState) -> dict:
    data = render_steps(state.get("steps"), max_rows=settings.max_rows)
    user = (
        # History resolves follow-ups ("and for the other region?") that would otherwise be
        # analysed as if they were standalone questions.
        history_for(state)
        + f"USER QUESTION: {state['question']}\n\n"
        f"RETRIEVED DATA:\n{data}\n\n"
        "Analyze this data to answer the question. Show your reasoning and any figures, basing "
        "every number strictly on the data above. If a result was capped/partial, compute only "
        "what the data supports and say so."
    )
    log.info("analysis over %d step(s)", len(state.get("steps", [])))
    return {"analysis": strip_think(chat(ANALYSIS_SYSTEM, user, temperature=0.2))}
