"""Request/response models for the HTTP API."""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator


# ── Replayed-history limits ─────────────────────────────────────────────────────────
# The API caps how MANY turns it uses (settings.history_turns, default 3) but nothing bounded how
# BIG one is: `user`/`assistant` were plain `str` and the transcript arrives from the client. One
# oversized turn reaches SIX agent prompts (orchestrator, planner, sql_author, analysis, verifier,
# synthesize), so the caller - not this server - decided how much work a request cost.
#
# These TRUNCATE rather than reject. A 422 here would kill the conversation the first time a
# legitimately long answer was replayed, and the client is replaying text THIS SERVER wrote. They
# sit well above any real turn (a MAX_ROWS=20 answer with its markdown table runs ~7 KB), so normal
# traffic is untouched and no behaviour changes; they exist only to bound the worst case.
#
# NOT a substitute for a request body limit - that belongs at the proxy/ASGI layer, since the body
# is already in memory by the time a validator runs.
MAX_TURN_USER_CHARS = 4_000
MAX_TURN_ASSISTANT_CHARS = 20_000
# Only the last `settings.history_turns` are ever used, so anything past this is pure transport
# cost. Trimmed rather than rejected, so a client that replays a whole session still works.
MAX_REPLAYED_TURNS = 20


def _bound(text: str, limit: int) -> str:
    """Cut over-long replayed text, preferring a line boundary so a table row is not halved."""
    if len(text) <= limit:
        return text
    cut = text.rfind("\n", 0, limit)
    return text[: cut if cut > limit // 2 else limit].rstrip() + "\n… (truncated)"


class Turn(BaseModel):
    """One previous exchange, supplied by the client for multi-turn context."""

    user: str
    assistant: str

    @field_validator("user")
    @classmethod
    def _bound_user(cls, value: str) -> str:
        return _bound(value, MAX_TURN_USER_CHARS)

    @field_validator("assistant")
    @classmethod
    def _bound_assistant(cls, value: str) -> str:
        return _bound(value, MAX_TURN_ASSISTANT_CHARS)


class ChatRequest(BaseModel):
    question: str = Field(..., min_length=1, description="The user's question.")
    history: list[Turn] = Field(
        default_factory=list,
        description=(
            "Previous turns, oldest first. The API is stateless - the client owns the "
            "conversation and replays it here for follow-up questions. Only the last few "
            "turns are used; longer transcripts and oversized turns are trimmed, not rejected."
        ),
    )
    include_sql: bool = Field(
        False,
        description=(
            "Include the generated SQL and raw rows in the response. Off by default so end "
            "users never see internals."
        ),
    )
    mode: Literal["normal", "deep"] = Field(
        "normal",
        description=(
            "'normal' = fast path (~2-3s): no Planner, Analysis or Verifier, cheap model tier. "
            "Best for counts, lists and simple groupings. "
            "'deep' = full pipeline (~52s): every agent and check runs. Use for analysis, "
            "rankings, predictions and multi-part questions."
        ),
    )

    @field_validator("history")
    @classmethod
    def _bound_history(cls, value: list[Turn]) -> list[Turn]:
        return value[-MAX_REPLAYED_TURNS:]


class DataPayload(BaseModel):
    columns: list[str] = []
    rows: list[list[Any]] = []
    truncated: bool = False


class QueryStep(BaseModel):
    sub_question: str = ""
    sql: str = ""
    row_count: int = 0
    truncated: bool = False


class ChartSeries(BaseModel):
    name: str
    data: list[float] = []


class ChartSpec(BaseModel):
    """Render-ready chart. Values come straight from the executed query, never from an LLM.

    The client only maps this onto its chart library - every decision (type, ordering, capping)
    was already made server-side.
    """

    type: Literal["bar", "line", "donut", "kpi"]
    title: str = ""
    labels: list[str] = []
    series: list[ChartSeries] = []
    x_axis: str = ""
    y_axis: str = ""
    # Caveats the user must see, e.g. "top 8 of 34 shown", "3 non-numeric value(s) plotted as 0".
    note: str = ""


class ChatResponse(BaseModel):
    answer: str
    route: Literal["sql", "clarify", "chitchat"] = "sql"
    needs_analysis: bool = False
    # False when the data behind this answer did not pass independent verification. The answer
    # text also says so, but this flag lets a client flag it visually without parsing prose.
    verified: bool = True
    # Which pipeline produced this answer, so a client can label it.
    mode: Literal["normal", "deep"] = "deep"
    latency_seconds: float = 0.0
    # Present only when the user asked for a chart AND the result could honestly be plotted.
    # Independent of include_sql: chart data is business data, not internals.
    chart: Optional[ChartSpec] = None
    # Only populated when include_sql=true.
    steps: Optional[list[QueryStep]] = None
    data: Optional[DataPayload] = None
    # Opaque, signed handle for POST /api/export — present only when this turn actually retrieved
    # data, so a chitchat/clarify reply naturally has nothing to export.
    #
    # Deliberately INDEPENDENT of include_sql: the SQL drawer is off by default, but a user should
    # still be able to download their data. The token reveals no internals (it is opaque and
    # signed), so sending it always is safe where sending raw SQL would not be.
    export_token: Optional[str] = None


class ExportRequest(BaseModel):
    """Download the data behind an answer. `token` comes from that answer's ChatResponse."""

    token: str = Field(..., min_length=1, description="The export_token from the chat response.")
    format: Literal["xlsx", "docx"] = Field(
        "xlsx", description="xlsx carries every row; docx adds the answer but caps the table."
    )
    # Word embeds the answer so the document explains itself. This is the Synthesizer's
    # ALREADY-GENERATED text handed back by the client — no model is called to produce it, and
    # xlsx ignores it entirely.
    answer: str = Field("", description="Answer text to include as a preamble (docx only).")
    question: str = Field("", description="Used for the file name and document title.")
    # Carries the chat response's `verified` flag through, so an unverified export is stamped as
    # indicative in the file rather than looking authoritative once detached from the chat.
    verified: bool = Field(True, description="False stamps an 'unverified' note in the file.")


class HealthResponse(BaseModel):
    status: str


class ReadyResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    database: str
    llm: str
    schema_loaded: bool
    schema_chars: int = 0


class InfoResponse(BaseModel):
    # Display name and one-line subject of the assistant (APP_NAME / APP_DESCRIPTION in .env).
    app_name: str = "SQL Assistant"
    app_description: str = ""
    provider: str
    model: str
    # Small model used only for routing steps (orchestrator/planner/chitchat).
    # Equal to `model` when no fast model is configured.
    fast_model: str = ""
    fast_model_enabled: bool = False
    database: str
    allowed_schemas: list[str]
    # Main entity names in the connected database, biggest tables first. Read from the live
    # schema so a client can build starter prompts without knowing anything about this domain.
    entities: list[str] = []
    max_rows: int
    max_queries: int
