"""Parsing helpers — tolerant extraction from local-LLM output."""
from __future__ import annotations

import json
import re
from typing import Any

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_SQL_FENCE_RE = re.compile(r"```(?:sql)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)


def strip_think(text: str) -> str:
    """Remove Qwen3 <think>...</think> reasoning blocks before parsing."""
    return _THINK_RE.sub("", text or "").strip()


def extract_sql(text: str) -> str:
    """Pull the SQL statement out of an LLM reply (fenced block preferred)."""
    text = strip_think(text)
    m = _SQL_FENCE_RE.search(text)
    candidate = m.group(1) if m else text
    # Keep only up to the first statement terminator; strip stray fences/prose.
    candidate = candidate.replace("```", "").strip()
    # If the model added prose before SELECT/WITH, cut to the first keyword.
    kw = re.search(r"\b(WITH|SELECT)\b", candidate, re.IGNORECASE)
    if kw:
        candidate = candidate[kw.start():]
    return candidate.strip().rstrip(";").strip()


def extract_json(text: str, default: dict[str, Any] | None = None) -> dict[str, Any]:
    """Pull a JSON object out of an LLM reply, tolerating fences/prose."""
    text = strip_think(text)
    m = _JSON_FENCE_RE.search(text)
    blob = m.group(1) if m else None
    if blob is None:
        # Fall back to the first {...} span.
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            blob = text[start : end + 1]
    if blob:
        try:
            return json.loads(blob)
        except json.JSONDecodeError:
            pass
    return default if default is not None else {}
