"""Observability — file logging plus an optional colour console view of the agent pipeline.

Every request's route, generated SQL, retries, DB errors, row counts and latency already go
to logs/chatbot.log. `setup_logging(console=True)` additionally renders those same records to
the terminal with rich, one colour-coded line per agent, so you can watch the orchestration
while the API serves.

The agent is inferred from the message text, deliberately: it keeps all of this in this
module and means no node, prompt or graph code has to change.
"""
from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler

from app.config import settings

_LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs")
_LOG_FILE = os.path.join(_LOG_DIR, "chatbot.log")
_configured = False
_console_attached = False


def _max_line_chars() -> int:
    """Console line cap. 0 (default) = never truncate, so the full SQL and the full verifier
    feedback are always visible. Set LOG_CONSOLE_MAX_CHARS to shorten them again."""
    try:
        return max(0, int(os.getenv("LOG_CONSOLE_MAX_CHARS", "0").strip() or 0))
    except (TypeError, ValueError):
        return 0

# (match, agent label, colour). First match wins, so order matters.
_AGENT_STYLES: list[tuple[str, str, str]] = [
    ("Q:", "Question", "bold white"),
    ("llm:", "LLM", "dim"),
    ("route=", "Orchestrator", "cyan"),
    ("planner:", "Planner", "magenta"),
    ("SQL[", "SQL Author", "yellow"),
    ("validation ok", "Validator", "green"),
    ("validation rejected", "Validator", "bold red"),
    ("sqlcheck: clean", "SQL Check", "green"),
    ("sqlcheck:", "SQL Check", "yellow"),
    ("exec ok", "Executor", "green"),
    ("exec error", "Executor", "bold red"),
    ("analysis over", "Analysis", "blue"),
    ("synthesize:", "Synthesizer", "bright_blue"),
    ("verify:", "Verifier", "bright_magenta"),
    ("chart:", "Chart", "bright_cyan"),
    ("clarify:", "Clarify", "bright_white"),
    ("chitchat:", "Chitchat", "bright_yellow"),
    ("turn done", "Done", "bold green"),
    ("API startup", "Startup", "dim"),
    ("API chat", "API", "red"),
    ("API stream", "API", "red"),
]
_DEFAULT_STYLE = ("Agent", "white")


def _classify(message: str) -> tuple[str, str]:
    for needle, label, colour in _AGENT_STYLES:
        if message.startswith(needle) or needle in message[:40]:
            return label, colour
    return _DEFAULT_STYLE


class _RichAgentHandler(logging.Handler):
    """Render each log record as an aligned, colour-coded agent line."""

    def __init__(self) -> None:
        super().__init__()
        from rich.console import Console

        self._console = Console(stderr=False)
        self._max_chars = _max_line_chars()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            label, colour = _classify(message)
            if record.levelno >= logging.ERROR:
                colour = "bold red"
            elif record.levelno >= logging.WARNING and label == "Agent":
                colour = "yellow"

            # Collapse to a single line, then show it in full (rich soft-wraps it to the
            # terminal width) so a long SQL statement is never cut off.
            body = " ".join(message.split())
            cap = self._max_chars
            if cap and len(body) > cap:
                body = body[:cap] + " …"

            ts = self.format_time(record)
            self._console.print(
                f"[dim]{ts}[/] [{colour}]{label:<13}[/] {body}",
                highlight=False,
                soft_wrap=True,
            )
        except Exception:  # noqa: BLE001 - logging must never break the request
            self.handleError(record)

    @staticmethod
    def format_time(record: logging.LogRecord) -> str:
        import time

        return time.strftime("%H:%M:%S", time.localtime(record.created))


def setup_logging(console: bool | None = None) -> None:
    """Configure the 'chatbot' logger.

    Args:
        console: also print colour agent lines to the terminal. Defaults to the
            LOG_CONSOLE env var (off), so the interactive CLI keeps its clean panel output
            while the API server can switch it on.
    """
    global _configured, _console_attached

    if console is None:
        console = os.getenv("LOG_CONSOLE", "false").strip().lower() in ("1", "true", "yes")

    logger = logging.getLogger("chatbot")

    if not _configured:
        os.makedirs(_LOG_DIR, exist_ok=True)
        logger.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))
        handler = RotatingFileHandler(
            _LOG_FILE, maxBytes=5_000_000, backupCount=5, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
        logger.addHandler(handler)
        logger.propagate = False
        _configured = True

    # Attach the console view at most once, even if setup_logging is called again.
    if console and not _console_attached:
        logger.addHandler(_RichAgentHandler())
        _console_attached = True


def get_logger() -> logging.Logger:
    return logging.getLogger("chatbot")


def log_path() -> str:
    return _LOG_FILE
