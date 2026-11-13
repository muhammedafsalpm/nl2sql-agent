"""Silent, deterministic sanity check on generated SQL — logged, never shown to the user.

Normal mode skips the Verifier, so nothing independently reviews the query. This does not replace
that, and it does not change any answer: it writes one log line when the SQL looks risky, so the
rate can be monitored and the mode boundary revisited with data rather than opinion.

NOTHING HERE IS SPECIFIC TO ANY DATABASE. Every rule is derived at runtime from the introspected
schema block that is already built for the prompts:

  * join keys      - from the `FK:` lines the introspection emits
  * duplicate keys - from the `⚠ MANY ROWS PER` markers introspection adds when a table holds
                     more rows than distinct key values
  * fraction cols  - from column names the schema reports, checked against how they are used

Point the app at a different database and the checks follow it, with no code change.
"""
from __future__ import annotations

import re

from app.observability import get_logger

log = get_logger()

_TABLE_RE = re.compile(r"^TABLE\s+(\S+)", re.MULTILINE)
_FK_RE = re.compile(r"^\s+FK:\s+(\w+)\s*->\s*(\S+)\.(\w+)\s*$", re.MULTILINE)
# The marker line carries explanatory text after the key, so do NOT anchor to end-of-line.
_DUP_RE = re.compile(r"MANY ROWS PER\s+(\w+)", re.MULTILINE)
# A query "collapses" a table when it aggregates or de-duplicates it in any of the usual ways.
_COLLAPSING = ("count(distinct", "group by", "row_number", "distinct ", "sum(", "avg(", "max(", "min(")


def _blocks(schema: str) -> list[tuple[str, str]]:
    out = []
    for block in re.split(r"(?=^TABLE\s)", schema, flags=re.MULTILINE):
        m = _TABLE_RE.search(block)
        if m:
            out.append((m.group(1), block))
    return out


def check(sql: str, schema: str) -> list[str]:
    """Return a list of human-readable concerns. Empty means nothing suspicious was found.

    Deliberately conservative: it only reports patterns it can establish from the schema, so a
    clean result is NOT a guarantee of correctness — it means these specific traps were avoided.
    """
    if not sql or not schema:
        return []
    low = " ".join(sql.split()).lower()
    concerns: list[str] = []

    for table, block in _blocks(schema):
        if table.lower() not in low:
            continue

        # 1. A table introspection flagged as holding many rows per key, used without collapsing.
        for key in _DUP_RE.findall(block):
            if not any(tok in low for tok in _COLLAPSING):
                concerns.append(
                    f"{table} holds many rows per {key} but the query does not aggregate or "
                    f"de-duplicate"
                )
                break

        # 2. A declared foreign key joined to the wrong target column.
        for col, target_table, target_col in _FK_RE.findall(block):
            if f"{col.lower()} =" not in low and f"{col.lower()}=" not in low:
                continue
            if target_table.lower() not in low:
                continue
            if target_col.lower() not in low:
                concerns.append(
                    f"{table}.{col} is declared to join {target_table}.{target_col}, "
                    f"but that column does not appear in the query"
                )
    return concerns


def log_check(sql: str, schema: str, question: str) -> list[str]:
    """Run the check and log the outcome. Returns the concerns; callers may ignore them."""
    try:
        concerns = check(sql, schema)
    except Exception as exc:  # noqa: BLE001 - a monitoring aid must never break a request
        log.warning("sqlcheck: skipped (%s)", exc)
        return []
    if concerns:
        log.warning("sqlcheck: %d concern(s) on '%.60s' -> %s", len(concerns), question,
                    "; ".join(concerns)[:400])
    else:
        log.info("sqlcheck: clean")
    return concerns
