"""Terminal chat loop. Shows the SQL the SQL Author generates, then the final answer."""
from __future__ import annotations

import sys
import time

# Windows' console defaults to cp1252, which cannot encode characters an LLM answer routinely
# contains - an em dash, a curly quote, a non-breaking hyphen (U+2011 in "follow-up", "year-end").
# The answer is rendered at the bottom of the loop below, OUTSIDE the try/except that guards
# graph.invoke(), so an unencodable character raised UnicodeEncodeError and killed the whole
# session - taking the conversation history with it - after the answer had already been paid for.
# Must run before Console() is constructed, since rich captures the stream at build time.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from rich.console import Console  # noqa: E402 - must follow the reconfigure above
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax

from app.config import settings
from app.db.connection import ping
from app.db.introspect import build_schema_text, build_value_hints
from app.graph.build import build_graph
from app.observability import get_logger, log_path, setup_logging

console = Console()
RECURSION_LIMIT = 60


def _banner() -> None:
    console.print(
        Panel.fit(
            f"[bold cyan]{settings.app_name}[/]\n"
            f"provider: [green]{settings.llm_provider}[/]   model: [green]{settings.active_model}[/]   "
            f"db: [green]{settings.db_name}[/]\n"
            "Ask about the records in this database — counts, lists, status, dates, progress,\n"
            "comparisons, trends and predictions.\n"
            "Type [yellow]exit[/]/[yellow]quit[/] to leave, [yellow]reset[/] to clear the conversation.",
            border_style="cyan",
        )
    )


def _load_schema() -> str | None:
    console.print("[dim]Loading schema + value hints...[/]")
    try:
        schema = build_schema_text()
        hints = build_value_hints()
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]✗ Could not load schema:[/] {exc}")
        return None
    combined = schema + (("\n\n" + hints) if hints else "")
    console.print(f"[green]✓[/] Schema loaded ({len(combined)} chars).\n")
    return combined


def main() -> None:
    setup_logging()
    log = get_logger()
    _banner()
    console.print(f"[dim]Logging to {log_path()}[/]")
    log.info("LLM provider=%s model=%s", settings.llm_provider, settings.active_model)

    console.print("[dim]Checking database connection...[/]")
    ok, msg = ping()
    if not ok:
        console.print(f"[red]✗ Database connection failed:[/] {msg}")
        console.print("[yellow]Fix DB settings in .env, then restart.[/]")
        return
    console.print(f"[green]✓[/] {msg}")

    schema = _load_schema()
    if schema is None:
        return

    graph = build_graph()
    history: list[dict[str, str]] = []

    while True:
        try:
            question = console.input("[bold blue]You[/] › ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]Bye.[/]")
            break
        if not question:
            continue
        if question.lower() in {"exit", "quit", ":q"}:
            console.print("[dim]Bye.[/]")
            break
        if question.lower() == "reset":
            history.clear()
            console.print("[dim]Conversation cleared.[/]\n")
            continue

        start = time.perf_counter()
        try:
            result = graph.invoke(
                {"question": question, "history": history, "schema": schema},
                config={"recursion_limit": RECURSION_LIMIT},
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("graph error")
            console.print(f"[red]Error:[/] {exc}\n")
            continue
        elapsed = time.perf_counter() - start
        log.info("turn done in %.2fs", elapsed)

        # Show every SQL the agent generated (one per gathered step).
        for i, step in enumerate(result.get("steps", []), 1):
            title = f"[bold]SQL Author[/] — query {i}"
            console.print(Panel(Syntax(step.get("sql", ""), "sql", theme="ansi_dark", word_wrap=True),
                                title=title, border_style="magenta"))
        if any(s.get("truncated") for s in result.get("steps", [])):
            console.print(f"[yellow]Note:[/] results were capped at {settings.max_rows} rows; "
                          "summary figures were computed in SQL where possible.")

        answer = result.get("answer", "(no answer produced)")
        console.print(Panel(Markdown(answer), title="[bold green]Assistant[/]", border_style="green"))
        console.print(f"[dim]({elapsed:.1f}s)[/]\n")

        history.append({"user": question, "assistant": answer})
        # Bound the list so a long session cannot grow without limit. Same setting the agents read,
        # so the CLI and the API remember exactly the same amount.
        history[:] = history[-settings.history_turns:] if settings.history_turns > 0 else []
