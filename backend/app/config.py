"""Central configuration, loaded once from .env."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _get(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _get_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)).strip())
    except (TypeError, ValueError):
        return default


_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _resolve_dir(value: str, default: str) -> str:
    """An absolute path as-is; a relative one is taken from the backend/ folder, not the CWD."""
    path = value or default
    return path if os.path.isabs(path) else os.path.normpath(os.path.join(_BACKEND_DIR, path))


@dataclass(frozen=True)
class Settings:
    # ── Identity (shown in the UI, API docs, CLI and export file names) ─────────
    # Nothing in the code names a product or a company. Set these two to describe YOUR data.
    app_name: str = field(default_factory=lambda: _get("APP_NAME", "SQL Assistant") or "SQL Assistant")
    # One plain sentence about what the connected database holds. It is given to the agents so
    # they understand the subject ("sales, products, customers and inventory of a bicycle
    # company"). Blank is allowed: the agents then work from the schema alone.
    app_description: str = field(default_factory=lambda: _get("APP_DESCRIPTION"))
    # Folder holding YOUR domain knowledge: business_rules.md, few_shots.md and golden.jsonl.
    # Relative paths are resolved from backend/. See docs/writing-domain-files.md.
    domain_dir: str = field(default_factory=lambda: _resolve_dir(_get("DOMAIN_DIR"), "domain"))

    # LLM
    llm_provider: str = field(default_factory=lambda: _get("LLM_PROVIDER", "local"))
    llm_model: str = field(default_factory=lambda: _get("LLM_MODEL", "qwen3.6:35b-a3b"))
    ollama_base_url: str = field(default_factory=lambda: _get("OLLAMA_BASE_URL", "http://localhost:11434"))
    ollama_num_ctx: int = field(default_factory=lambda: _get_int("OLLAMA_NUM_CTX", 32768))
    # OpenAI (used when LLM_PROVIDER is 'open'/'openai')
    openai_api_key: str = field(default_factory=lambda: _get("OPENAI_API_KEY"))
    openai_model: str = field(default_factory=lambda: _get("OPENAI_MODEL", "gpt-5-mini"))
    # Blank = OpenAI itself. Set it to use any OpenAI-COMPATIBLE server instead: LM Studio, vLLM,
    # LocalAI, OpenRouter, Groq, Together, ... (e.g. http://localhost:1234/v1). OPENAI_MODEL and
    # OPENAI_FAST_MODEL then name models on THAT server.
    openai_base_url: str = field(default_factory=lambda: _get("OPENAI_BASE_URL"))
    # Optional FAST models for cheap routing steps (blank = use the main model, i.e. no change)
    ollama_fast_model: str = field(default_factory=lambda: _get("OLLAMA_FAST_MODEL"))
    openai_fast_model: str = field(default_factory=lambda: _get("OPENAI_FAST_MODEL"))

    @property
    def _is_openai(self) -> bool:
        return self.llm_provider.lower() in ("open", "openai")

    @property
    def active_model(self) -> str:
        """The model name for whichever provider is selected (for display/logging)."""
        return self.openai_model if self._is_openai else self.llm_model

    @property
    def fast_model(self) -> str:
        """A small/fast model for routing steps; falls back to the main model if none set."""
        if self._is_openai:
            return self.openai_fast_model or self.openai_model
        return self.ollama_fast_model or self.llm_model

    # Database
    db_server: str = field(default_factory=lambda: _get("DB_SERVER"))
    db_port: int = field(default_factory=lambda: _get_int("DB_PORT", 1433))
    db_name: str = field(default_factory=lambda: _get("DB_NAME"))
    db_user: str = field(default_factory=lambda: _get("DB_USER"))
    db_password: str = field(default_factory=lambda: _get("DB_PASSWORD"))
    db_driver: str = field(default_factory=lambda: _get("DB_DRIVER"))
    db_encrypt: str = field(default_factory=lambda: _get("DB_ENCRYPT", "yes"))
    db_trust_cert: str = field(default_factory=lambda: _get("DB_TRUST_CERT", "yes"))

    # Per-request LLM timeout (seconds) and provider-level retry count. A stalled call would
    # otherwise hold a worker thread — and its DB connection — indefinitely.
    llm_timeout: int = field(default_factory=lambda: _get_int("LLM_TIMEOUT", 120))
    llm_max_retries: int = field(default_factory=lambda: _get_int("LLM_MAX_RETRIES", 2))

    # Guardrails
    max_sql_retries: int = field(default_factory=lambda: _get_int("MAX_SQL_RETRIES", 2))
    max_rows: int = field(default_factory=lambda: _get_int("MAX_ROWS", 100))
    query_timeout: int = field(default_factory=lambda: _get_int("QUERY_TIMEOUT", 60))
    # Agentic loop
    max_queries: int = field(default_factory=lambda: _get_int("MAX_QUERIES", 3))
    # How many times a Verifier rejection may send the SQL Author back for a rewrite. Kept
    # SEPARATE from max_sql_retries on purpose: sharing one counter let unrelated syntax errors
    # earlier in the turn leave the Verifier with no rewrites left.
    verify_retries: int = field(default_factory=lambda: _get_int("VERIFY_RETRIES", 1))
    # Conversation memory: how many previous exchanges (question + answer) the agents can see.
    #
    # ONE setting on purpose. This used to be governed by three separate numbers that disagreed —
    # MAX_HISTORY_TURNS=6 in the API, MAX_HISTORY=6 in the CLI, and a max_turns=3 default inside
    # format_history(). The smallest always won, so the agents saw 3 while two constants advertised
    # 6, and raising either of them silently did nothing.
    #
    # Default 3 preserves exactly the behaviour that was in effect before this was consolidated.
    # Raising it costs little (measured: +338 tokens per turn on short answers, +4k on long ones),
    # but the router and the Verifier both read history, so change it deliberately and re-test.
    history_turns: int = field(default_factory=lambda: max(0, _get_int("HISTORY_TURNS", 3)))
    # Observability
    log_level: str = field(default_factory=lambda: _get("LOG_LEVEL", "INFO"))
    # Schemas the chatbot may see. Blank = every user schema in the database (system schemas such
    # as sys, INFORMATION_SCHEMA, guest and db_* are always skipped) — so a database that keeps
    # everything in `dbo` works with no setting at all. The resolved list is in
    # app.db.introspect.visible_schemas().
    allowed_schemas: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            s.strip().lower() for s in _get("ALLOWED_SCHEMAS").split(",") if s.strip()
        )
    )
    # Tables hidden from the LLM even though their schema is allowed — backups, copies, staging
    # leftovers. Comma-separated, and each entry may be either "schema.table" (exact, preferred) or
    # a bare "table" name, which hides that name in EVERY allowed schema.
    excluded_tables: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            t.strip().lower() for t in _get("EXCLUDED_TABLES").split(",") if t.strip()
        )
    )
    # Individual columns hidden from the LLM, on top of the built-in secret-name filter. Each entry
    # may be "schema.table.column" (exact, preferred), "table.column" (that table in any schema), or
    # a bare "column" name, which hides that name in EVERY table.
    excluded_columns: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            c.strip().lower() for c in _get("EXCLUDED_COLUMNS").split(",") if c.strip()
        )
    )

    # ── Export (download an answer's data as Excel / Word) ─────────────────────
    # DELIBERATELY SEPARATE FROM max_rows. `max_rows` is a token-cost control: it limits how many
    # rows reach the LLM prompt, which is why a chat answer shows "the first 20 of 40". An export
    # has no LLM in it, so that cap does not apply — the file carries every row. Nothing here is
    # read by the agent graph, so changing any of it cannot affect answers or token usage.
    export_max_rows: int = field(default_factory=lambda: _get_int("EXPORT_MAX_ROWS", 1_000_000))
    # Own timeout: re-reading a full result legitimately takes longer than a 20-row preview, so
    # export must not inherit the chat query_timeout.
    export_query_timeout: int = field(
        default_factory=lambda: _get_int("EXPORT_QUERY_TIMEOUT", 600)
    )
    # HMAC key proving an export request carries SQL THIS server wrote (see app/api/export.py).
    # MUST be stable across restarts and workers: a per-process fallback is generated when unset,
    # which invalidates open export links on restart and breaks entirely when API_WORKERS > 1.
    export_secret: str = field(default_factory=lambda: _get("EXPORT_SECRET"))
    # How long an export link stays valid (seconds). Default 24h.
    export_token_ttl: int = field(default_factory=lambda: _get_int("EXPORT_TOKEN_TTL", 86400))

    # ── Langfuse tracing (optional, self-hosted) ────────────────────────────────
    # ENTIRELY OPT-IN. Blank langfuse_public_key => no handler is attached anywhere and every
    # code path is identical to the pre-Langfuse behaviour, exactly like a blank API_KEY
    # disables auth. Nothing has to be reverted to turn tracing off.
    #
    # Traces carry the FULL prompt and response of every LLM call, which for this pipeline
    # includes retrieved business rows. Point this ONLY at a trusted, access-controlled
    # instance - never a shared or public one.
    langfuse_base_url: str = field(default_factory=lambda: _get("LANGFUSE_BASE_URL"))
    langfuse_public_key: str = field(default_factory=lambda: _get("LANGFUSE_PUBLIC_KEY"))
    langfuse_secret_key: str = field(default_factory=lambda: _get("LANGFUSE_SECRET_KEY"))

    # ── Reasoning tuning (main tier only) ───────────────────────────────────────
    # Applied ONLY to main-tier calls. The fast tier is a non-reasoning model, which rejects these
    # parameters outright, so app/llm.py never sends them there.
    #
    # Blank disables the whole feature and every call is byte-identical to before — the same
    # convention as a blank API_KEY or LANGFUSE_PUBLIC_KEY. Per-agent values live in llm.py's
    # _AGENT_TUNING, which is keyed on the agent label chat() already derives; these two settings
    # are the global override for turning it off or forcing one value everywhere.
    reasoning_effort: str = field(default_factory=lambda: _get("OPENAI_REASONING_EFFORT"))
    reasoning_verbosity: str = field(default_factory=lambda: _get("OPENAI_VERBOSITY"))
    # Reasoning models often accept only their default temperature. The nodes ask for 0.2 on
    # Analysis and the Synthesizer, which then costs a REJECTED call plus a silent retry on every
    # turn (see chat()'s fallback). true drops the temperature up front for main-tier calls and
    # avoids the wasted round trip entirely.
    reasoning_ignores_temperature: bool = field(
        default_factory=lambda: _get("OPENAI_IGNORES_TEMPERATURE", "false").lower()
        in ("1", "true", "yes")
    )


settings = Settings()
