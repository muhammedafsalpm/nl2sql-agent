"""Golden eval harness. Runs each question through the graph and reports:
  - the SQL generated, whether it executed, row counts,
  - a heuristic pass/fail (did the expected tables appear in the SQL),
  - the final answer.

Usage:  python -m eval.run_eval        (from the backend/ folder)

The questions come from golden.jsonl in your domain folder (DOMAIN_DIR in .env, default
backend/domain/), so the test set lives next to the business rules it is meant to check.

This does NOT assert exact SQL — Text-to-SQL has many correct forms. It gives you a fast,
repeatable readout to eyeball after any prompt/rule change.
"""
from __future__ import annotations

import json
import os
import sys

# Windows' console defaults to a narrow codepage (cp1252) that cannot represent characters an
# LLM answer routinely contains - an em dash, a curly quote, a non-breaking hyphen (U+2011).
# print() then raises UnicodeEncodeError and kills the run mid-question, discarding every result
# gathered so far. reconfigure() (Python 3.7+) exists on every stdout/stderr; errors="replace"
# swaps only the unrepresentable character for "?" rather than losing the whole eval run.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

# Allow running as `python eval/run_eval.py` or `python -m eval.run_eval`.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings  # noqa: E402
from app.db.introspect import build_schema_text, build_value_hints  # noqa: E402
from app.graph.build import build_graph  # noqa: E402
from app.observability import setup_logging  # noqa: E402

GOLDEN = os.path.join(settings.domain_dir, "golden.jsonl")


def load_cases() -> list[dict]:
    cases = []
    with open(GOLDEN, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                cases.append(json.loads(line))
    return cases


def main() -> None:
    if not os.path.exists(GOLDEN):
        print(f"No test questions found at {GOLDEN}.")
        print("Create golden.jsonl there (format: backend/domain/README.md), or point DOMAIN_DIR")
        print("at a folder that has one, e.g. DOMAIN_DIR=../examples/adventureworks")
        return
    setup_logging()
    schema = build_schema_text()
    hints = build_value_hints()
    if hints:
        schema += "\n\n" + hints
    graph = build_graph()
    cases = load_cases()
    if not cases:
        print(f"{GOLDEN} has no questions in it.")
        return

    passed = 0
    for i, case in enumerate(cases, 1):
        q = case["question"]
        try:
            result = graph.invoke(
                {"question": q, "history": [], "schema": schema},
                config={"recursion_limit": 60},
            )
        except Exception as exc:  # noqa: BLE001
            print(f"\n[{i}] {q}\n    ERROR: {exc}")
            continue

        # Count steps the Verifier set aside too, otherwise every rejected turn scores as a miss
        # and the harness silently reports a regression that did not happen.
        steps = (result.get("steps") or []) + (result.get("rejected_steps") or [])
        all_sql = " ".join(s.get("sql", "") for s in steps).lower()
        expect = [t.lower() for t in case.get("expect_tables", [])]
        hit = all(t in all_sql for t in expect) if expect else True
        passed += 1 if hit else 0

        print(f"\n[{i}] {q}")
        for j, s in enumerate(steps, 1):
            print(f"    SQL{j}: {s.get('sql','').strip()[:300]}")
            print(f"          -> {len(s.get('rows', []))} rows"
                  + (" (truncated)" if s.get("truncated") else ""))
        print(f"    expect tables {expect}: {'PASS' if hit else 'CHECK'}")
        print(f"    answer: {result.get('answer','')[:200].strip()}")

    print(f"\n=== {passed}/{len(cases)} cases hit expected tables (heuristic) ===")


if __name__ == "__main__":
    main()
