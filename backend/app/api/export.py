"""Export a finished answer's data as a downloadable file.

WHY THIS IS A SEPARATE PATH, NOT PART OF THE AGENT GRAPH
--------------------------------------------------------
`MAX_ROWS` exists to keep row data OUT of the LLM prompt — it is a token-cost control, applied in
`executor_node`'s `fetchmany()`. The chat answer therefore shows a preview ("showing the first 20
of 40"), which is exactly what we want for cost.

An export has no LLM in it at all, so that cap is meaningless here: the file must carry every row.
This module therefore re-runs the SAME SQL the answer was built from, with its OWN row cap and its
OWN timeout. It never imports `executor_node`, never reads `settings.max_rows`, and makes ZERO
model calls — so nothing about the pipeline's token usage changes because this file exists.

THE TRUST MODEL — why the client cannot just POST us SQL
--------------------------------------------------------
The chat pipeline only ever executes SQL its own SQL Author wrote, against a schema that hides
`EXCLUDED_TABLES`/`EXCLUDED_COLUMNS`. If this endpoint accepted raw SQL from the browser, that
guarantee would be gone: `is_select_only()` blocks writes, but places NO restriction on WHICH
tables may be read, so a caller could exfiltrate a deliberately-hidden table.

So the server hands the client an opaque, HMAC-signed token instead of SQL. On export, the
signature is re-checked: a valid one proves THIS SERVER authored that exact SQL. Tampered or
hand-crafted SQL fails verification and never reaches the database.

The token is signed, NOT encrypted — it is not a secret, it just cannot be forged. It also carries
an expiry, so a leaked token stops working.

Stateless on purpose: no server-side token cache. `runtime.py` notes the API is stateless so it can
scale out, and an in-memory cache would break as soon as API_WORKERS > 1 (a token minted by worker
1 would be unknown to worker 2). HMAC needs only a shared secret, which every worker reads from
the same .env.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time

from app.config import settings
from app.db.connection import get_connection
from app.graph.nodes.validator import is_select_only
from app.observability import get_logger

log = get_logger()


class ExportError(Exception):
    """Anything that should become a 400 rather than a 500."""


# ── Signing key ─────────────────────────────────────────────────────────────────
# A per-process fallback keeps the feature working out of the box, but it is NOT good enough for
# real use: every restart invalidates tokens already on screen, and with more than one worker a
# token minted by one process fails verification in another. Warned about once, loudly.
_FALLBACK_SECRET: str | None = None


def _secret() -> bytes:
    global _FALLBACK_SECRET
    configured = settings.export_secret
    if configured:
        return configured.encode("utf-8")
    if _FALLBACK_SECRET is None:
        _FALLBACK_SECRET = secrets.token_urlsafe(32)
        log.warning(
            "EXPORT_SECRET is not set - using a random per-process key. Export links will stop "
            "working after a restart, and will fail entirely if API_WORKERS > 1. Set EXPORT_SECRET "
            "in .env for anything other than local testing."
        )
    return _FALLBACK_SECRET.encode("utf-8")


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _sign(payload: bytes) -> str:
    return _b64e(hmac.new(_secret(), payload, hashlib.sha256).digest())


# ── Token ───────────────────────────────────────────────────────────────────────

def make_token(sql: str) -> str | None:
    """Mint an opaque export token for SQL this server just executed.

    Returns None when there is nothing exportable, so callers can simply pass the result through
    to an optional response field.
    """
    sql = (sql or "").strip()
    if not sql:
        return None
    try:
        payload = json.dumps(
            {"sql": sql, "exp": int(time.time()) + settings.export_token_ttl},
            separators=(",", ":"),
        ).encode("utf-8")
        return f"{_b64e(payload)}.{_sign(payload)}"
    except Exception:  # noqa: BLE001 - never break a chat response over an optional field
        log.exception("export: could not mint a token")
        return None


def read_token(token: str) -> str:
    """Verify a token and return the SQL it carries. Raises ExportError if it is not ours."""
    if not token or "." not in token:
        raise ExportError("Malformed export token.")
    encoded, signature = token.rsplit(".", 1)
    try:
        payload = _b64d(encoded)
    except Exception as exc:  # noqa: BLE001
        raise ExportError("Malformed export token.") from exc

    # compare_digest, not ==, so a wrong signature cannot be discovered byte-by-byte by timing.
    if not hmac.compare_digest(_sign(payload), signature):
        raise ExportError("This export link is not valid for this server.")

    try:
        data = json.loads(payload)
    except Exception as exc:  # noqa: BLE001
        raise ExportError("Malformed export token.") from exc

    if int(data.get("exp", 0)) < int(time.time()):
        raise ExportError("This export link has expired — please ask the question again.")

    sql = (data.get("sql") or "").strip()
    if not sql:
        raise ExportError("This export link carries no query.")
    return sql


# ── Query execution ─────────────────────────────────────────────────────────────

def fetch_all(sql: str) -> tuple[list[str], list[list], bool]:
    """Run an app-authored SELECT and return (columns, rows, capped).

    SYNCHRONOUS: pyodbc blocks. Callers MUST run this in a worker thread — the same rule
    runtime.py follows for the agent graph. A large export on the event loop would freeze every
    other request for its whole duration.
    """
    # Belt and braces. The signature already proves we wrote this SQL, but the SELECT-only gate is
    # the app's actual safety boundary, so it runs here too rather than being assumed.
    ok, reason = is_select_only(sql)
    if not ok:
        raise ExportError(f"This query cannot be exported: {reason}")

    cap = settings.export_max_rows
    conn = None
    try:
        conn = get_connection()
        # Override the CHAT query timeout: a full export legitimately takes longer than a
        # 20-row preview. get_connection() sets settings.query_timeout; this replaces it for
        # this connection only and leaves connection.py untouched.
        conn.timeout = settings.export_query_timeout
        cur = conn.cursor()
        cur.execute(sql)
        columns = [d[0] for d in cur.description] if cur.description else []
        rows: list[list] = []
        capped = False
        # Batched rather than fetchall(): one 1M-row list materialised in a single call is the
        # easiest way to exhaust memory, and batching lets the cap stop work early.
        while True:
            batch = cur.fetchmany(5000)
            if not batch:
                break
            for record in batch:
                if cap and len(rows) >= cap:
                    capped = True
                    break
                rows.append(list(record))
            if capped:
                break
        log.info(
            "export: %d row(s) x %d col(s)%s", len(rows), len(columns), " (capped)" if capped else ""
        )
        return columns, rows, capped
    except ExportError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("export: query failed: %s", exc)
        raise ExportError("The data for this answer could not be re-read for export.") from exc
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
