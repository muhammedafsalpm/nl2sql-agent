"""Build a compact, LLM-friendly schema description from the live database.

Only the schemas listed in ALLOWED_SCHEMAS are included (blank = every user schema). Excluded
tables and any secret columns are left out so the chatbot can neither see nor query them.

The description includes, per table: columns (+ PK markers) and declared foreign keys only.
An earlier version also guessed "JOINABLE" relationships from columns sharing a name with another
table's primary key. Removed: a name match is not proof of a real relationship — two unrelated
columns can happen to share a name — and a wrong guess here is invisible to review, unlike a
business rule in domain/business_rules.md, which a person actually reads and can correct.
"""
from __future__ import annotations

import hashlib
import os
import re

from app.config import settings
from app.db.connection import get_connection
from app.observability import get_logger

log = get_logger()

# Which tables are hidden is per-database knowledge, so it is configured in .env
# (EXCLUDED_TABLES), never hardcoded here. This module only applies the setting.
# Substrings that mark a column as secret — never exposed to the LLM. Generic credential wording,
# not tied to any schema.
SECRET_COLUMN_MARKERS = ("password", "secret", "token", "pwd", "apikey", "api_key", "credential", "private_key")

# Bumped whenever _render() changes the TEXT it emits for an unchanged database. The cache is
# keyed on a fingerprint of the live STRUCTURE, so without this a rendering change would keep
# serving the old cached text forever - the database has not changed, so nothing else would
# notice. Folded into _live_fingerprint() below.
_RENDER_VERSION = "3-bracket-reserved-and-nonbare-identifiers"

# T-SQL reserved keywords (SQL Server). A column whose NAME is one of these MUST be written
# [bracketed] or the query fails to parse - and the error is misleading: SQL Server reports
# "Incorrect syntax near the keyword 'plan'" (42000), NOT "Invalid column name" (42S22), so it
# reads like the column does not exist when in fact it does.
#
# Real failure this prevents: a table with a column literally named `plan`. The SQL Author wrote
# `t.plan AS item_plan`, which is unparseable, and burned retries re-making the same mistake
# because the schema block showed the bare name with no hint that it needed quoting.
#
# The list follows SQL Server's reserved-keyword list, not a guess: `status`, `time`, `system`
# and `session` LOOK reserved but SQL Server accepts them bare, and bracketing every column would
# add noise to every prompt for no benefit.
_TSQL_RESERVED = frozenset("""
ADD ALL ALTER AND ANY AS ASC AUTHORIZATION BACKUP BEGIN BETWEEN BREAK BROWSE BULK BY CASCADE CASE
CHECK CHECKPOINT CLOSE CLUSTERED COALESCE COLLATE COLUMN COMMIT COMPUTE CONSTRAINT CONTAINS
CONTAINSTABLE CONTINUE CONVERT CREATE CROSS CURRENT CURRENT_DATE CURRENT_TIME CURRENT_TIMESTAMP
CURRENT_USER CURSOR DATABASE DBCC DEALLOCATE DECLARE DEFAULT DELETE DENY DESC DISK DISTINCT
DISTRIBUTED DOUBLE DROP DUMP ELSE END ERRLVL ESCAPE EXCEPT EXEC EXECUTE EXISTS EXIT EXTERNAL FETCH
FILE FILLFACTOR FOR FOREIGN FREETEXT FREETEXTTABLE FROM FULL FUNCTION GOTO GRANT GROUP HAVING
HOLDLOCK IDENTITY IDENTITY_INSERT IDENTITYCOL IF IN INDEX INNER INSERT INTERSECT INTO IS JOIN KEY
KILL LEFT LIKE LINENO LOAD MERGE NATIONAL NOCHECK NONCLUSTERED NOT NULL NULLIF OF OFF OFFSETS ON
OPEN OPENDATASOURCE OPENQUERY OPENROWSET OPENXML OPTION OR ORDER OUTER OVER PERCENT PIVOT PLAN
PRECISION PRIMARY PRINT PROC PROCEDURE PUBLIC RAISERROR READ READTEXT RECONFIGURE REFERENCES
REPLICATION RESTORE RESTRICT RETURN REVERT REVOKE RIGHT ROLLBACK ROWCOUNT ROWGUIDCOL RULE SAVE
SCHEMA SECURITYAUDIT SELECT SESSION_USER SET SETUSER SHUTDOWN SOME STATISTICS SYSTEM_USER TABLE
TABLESAMPLE TEXTSIZE THEN TO TOP TRAN TRANSACTION TRIGGER TRUNCATE TRY_CONVERT TSEQUAL UNION
UNIQUE UNPIVOT UPDATE UPDATETEXT USE USER VALUES VARYING VIEW WAITFOR WHEN WHERE WHILE WITH
WRITETEXT
""".split())


# A bare (unbracketed) T-SQL identifier may only be a letter/underscore followed by letters,
# digits, underscore, $ or #. ANY other shape must be bracketed - and this is a SECOND, separate
# reason from the reserved-word list above. Columns with spaces are the common case, e.g.
# dbo.inspection_form."Form Number" or dbo.audit_log."No of Items Checked". They fail
# with the SAME misleading error as `plan` ("Incorrect syntax near..."), so checking only the
# reserved-word list would have left this whole family broken.
_BARE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$#]*$")


def quote_column(col: str) -> str:
    """The form the SQL Author must copy: [bracketed] whenever the bare name would not parse.

    Two independent reasons a name needs brackets:
      1. it is a reserved T-SQL keyword (`plan`)
      2. it is not a legal bare identifier at all - spaces or punctuation ("Form Number")

    Applied to the column DECLARATION lines only. FK lines are deliberately left bare because
    app/graph/sqlcheck.py parses them with a \\w+ pattern that brackets would not match.
    """
    if col.upper() in _TSQL_RESERVED or not _BARE_IDENTIFIER.match(col):
        return f"[{col}]"
    return col

_CACHE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), ".cache", "schema.txt")
# Fingerprint of the live structure the cache was built from. Lets a restart notice that the
# database has gained/lost/renamed a table or column, or had a key/constraint added, dropped or
# changed, and rebuild itself — instead of silently writing SQL against a stale picture until a
# query happens to fail, or reasoning about relationships that no longer exist.
_FINGERPRINT_PATH = os.path.join(os.path.dirname(_CACHE_PATH), "schema.fingerprint")


# Schemas that never hold user data. db_* covers the fixed database roles (db_owner, ...),
# which SQL Server also exposes as schemas.
_SYSTEM_SCHEMAS = frozenset({"sys", "information_schema", "guest"})
# Tables SQL Server or its tools create on their own; never business data.
_SYSTEM_TABLES = frozenset({"sysdiagrams"})
_resolved_schemas: tuple[str, ...] | None = None


def visible_schemas(cur) -> tuple[str, ...]:
    """The schemas the chatbot may see, lower-cased.

    ALLOWED_SCHEMAS when it is set. When it is blank, every schema that owns at least one user
    table or view — so a database that keeps everything in `dbo` (the SQL Server default) works
    with no configuration. Resolved once per process; refresh() clears it.
    """
    global _resolved_schemas
    if settings.allowed_schemas:
        return settings.allowed_schemas
    if _resolved_schemas is None:
        cur.execute(
            """
            SELECT DISTINCT LOWER(s.name)
            FROM sys.schemas s
            JOIN sys.objects o ON o.schema_id = s.schema_id
            WHERE o.type IN ('U', 'V') AND o.is_ms_shipped = 0
            ORDER BY 1
            """
        )
        found = tuple(
            r[0] for r in cur.fetchall()
            if r[0] not in _SYSTEM_SCHEMAS and not r[0].startswith("db_")
        )
        # Never an empty tuple: "IN ()" is a syntax error. dbo simply yields nothing if empty.
        _resolved_schemas = found or ("dbo",)
        log.info("schema: ALLOWED_SCHEMAS is blank - using every user schema: %s",
                 ", ".join(_resolved_schemas))
    return _resolved_schemas


def _is_secret(col: str) -> bool:
    lc = col.lower()
    return any(m in lc for m in SECRET_COLUMN_MARKERS)


def _is_excluded(schema: str, table: str) -> bool:
    """True when EXCLUDED_TABLES in .env hides this table from the LLM.

    Matches either "schema.table" (exact — the safe form) or a bare "table" name, which hides that
    name in every allowed schema. Excluding a table also removes it from the primary keys, the
    foreign keys, the inferred joins and the value hints, because each of those is built from the
    surviving table list.
    """
    if table.lower() in _SYSTEM_TABLES:
        return True
    if not settings.excluded_tables:
        return False
    excluded = settings.excluded_tables
    return f"{schema}.{table}".lower() in excluded or table.lower() in excluded


def _is_excluded_column(schema: str, table: str, column: str) -> bool:
    """True when EXCLUDED_COLUMNS in .env hides this column from the LLM.

    Matches "schema.table.column" (exact), "table.column" (that table in any schema), or a bare
    "column" name (that name in every table). This runs on top of SECRET_COLUMN_MARKERS, which
    always applies and needs no configuration.

    A hidden column is also stripped from the primary keys and from any foreign key that touches
    it, so the schema block never points the model at a column it cannot see.
    """
    if not settings.excluded_columns:
        return False
    excluded = settings.excluded_columns
    return (
        f"{schema}.{table}.{column}".lower() in excluded
        or f"{table}.{column}".lower() in excluded
        or column.lower() in excluded
    )


def _fetch_columns(cur) -> dict[str, list[tuple]]:
    schemas = visible_schemas(cur)
    placeholders = ",".join("?" for _ in schemas)
    cur.execute(
        f"""
        SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, DATA_TYPE,
               CHARACTER_MAXIMUM_LENGTH, IS_NULLABLE, ORDINAL_POSITION
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE LOWER(TABLE_SCHEMA) IN ({placeholders})
        ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION
        """,
        *schemas,
    )
    tables: dict[str, list[tuple]] = {}
    for sch, tbl, col, dtype, maxlen, nullable, _ in cur.fetchall():
        if _is_excluded(sch, tbl) or _is_secret(col) or _is_excluded_column(sch, tbl, col):
            continue
        tables.setdefault(f"{sch}.{tbl}", []).append((col, dtype, maxlen, nullable))
    return tables


def _fetch_primary_keys(cur) -> dict[str, set[str]]:
    """schema.table -> set of primary-key column names."""
    schemas = visible_schemas(cur)
    placeholders = ",".join("?" for _ in schemas)
    cur.execute(
        f"""
        SELECT sch.name, t.name, c.name
        FROM sys.indexes i
        JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id
        JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
        JOIN sys.tables  t   ON t.object_id  = i.object_id
        JOIN sys.schemas sch ON sch.schema_id = t.schema_id
        WHERE i.is_primary_key = 1 AND LOWER(sch.name) IN ({placeholders})
        """,
        *schemas,
    )
    pks: dict[str, set[str]] = {}
    for sch, tbl, col in cur.fetchall():
        # A hidden PK column is dropped here rather than later: pks feeds the " PK" marker in the
        # rendered schema and the duplicate-key scan, both of which would otherwise name a column
        # the model was never shown.
        if _is_excluded(sch, tbl) or _is_excluded_column(sch, tbl, col):
            continue
        pks.setdefault(f"{sch}.{tbl}", set()).add(col)
    return pks


def _fetch_foreign_keys(cur, visible: set[str]) -> dict[str, list[str]]:
    """Declared FKs, restricted so both endpoints are visible (no dangling references)."""
    cur.execute(
        """
        SELECT
            sch.name  AS from_schema, t.name  AS from_table,  c.name  AS from_col,
            rsch.name AS to_schema,   rt.name AS to_table,    rc.name AS to_col
        FROM sys.foreign_key_columns fkc
        JOIN sys.tables  t   ON t.object_id  = fkc.parent_object_id
        JOIN sys.schemas sch ON sch.schema_id = t.schema_id
        JOIN sys.columns c   ON c.object_id  = fkc.parent_object_id AND c.column_id = fkc.parent_column_id
        JOIN sys.tables  rt  ON rt.object_id = fkc.referenced_object_id
        JOIN sys.schemas rsch ON rsch.schema_id = rt.schema_id
        JOIN sys.columns rc  ON rc.object_id  = fkc.referenced_object_id AND rc.column_id = fkc.referenced_column_id
        """
    )
    fks: dict[str, list[str]] = {}
    for fs, ft, fc, ts, tt, tc in cur.fetchall():
        frm, to = f"{fs}.{ft}", f"{ts}.{tt}"
        if frm not in visible or to not in visible:
            continue
        # Either END being hidden makes the relationship unusable — a join the model cannot write.
        if _is_excluded_column(fs, ft, fc) or _is_excluded_column(ts, tt, tc):
            continue
        fks.setdefault(frm, []).append(f"{fc} -> {to}.{tc}")
    return fks


# Below this many rows a repeated key cannot distort an answer enough to be worth a scan, and
# these are the lookup tables anyway. Keeps rebuild time flat as the fact tables grow.
_DUP_CHECK_MIN_ROWS = 100


def _detect_duplicate_keys(
    cur,
    tables: dict[str, list[tuple]],
    pks: dict[str, set[str]],
    row_counts: dict[str, int] | None = None,
) -> dict[str, str]:
    """table -> the key column that repeats, for tables holding many rows per entity.

    Measured from the live data, not assumed: a table with more rows than distinct key values must
    be de-duplicated before per-entity questions, and forgetting that is the single most damaging
    SQL mistake in this kind of database. Surfacing it in the schema block means the SQL Author and
    the checks learn it from the database instead of from a hand-written rule.

    Only the COUNT(DISTINCT ...) is a scan — the total comes free from catalogue statistics, and
    small tables are skipped entirely, so this stays fast as the data grows.
    """
    row_counts = row_counts or {}
    found: dict[str, str] = {}
    for table, cols in tables.items():
        total = row_counts.get(table)
        if total is not None and total < _DUP_CHECK_MIN_ROWS:
            continue

        tpk = pks.get(table, set())
        # A SINGLE-column primary key is unique by definition — the database enforces it, so
        # scanning to confirm would tell us nothing. Only tables where the entity key is a guess
        # (no primary key at all, or a composite one whose parts individually repeat) need a scan.
        if len(tpk) == 1:
            continue

        names = [c[0] for c in cols]
        candidates = sorted(tpk) or [c for c in names if c.lower().endswith("id")]
        if not candidates:
            continue
        key = candidates[0]
        sch, _, tbl = table.partition(".")
        try:
            if total is None:
                cur.execute(f"SELECT COUNT(*), COUNT(DISTINCT [{key}]) FROM [{sch}].[{tbl}]")
                total, distinct = cur.fetchone()
            else:
                cur.execute(f"SELECT COUNT(DISTINCT [{key}]) FROM [{sch}].[{tbl}]")
                distinct = cur.fetchone()[0]
        except Exception:  # noqa: BLE001 - a table we cannot count must not break introspection
            continue
        if total and distinct and total > distinct:
            found[table] = key
    return found


def _render(
    tables: dict[str, list[tuple]],
    pks: dict[str, set[str]],
    fks: dict[str, list[str]],
    dup_keys: dict[str, str] | None = None,
) -> str:
    dup_keys = dup_keys or {}
    lines: list[str] = []
    for table, cols in tables.items():
        lines.append(f"TABLE {table}")
        tpk = pks.get(table, set())
        for col, dtype, maxlen, nullable in cols:
            typ = dtype + (f"({maxlen})" if maxlen and maxlen > 0 else "")
            null = "" if nullable == "YES" else " NOT NULL"
            pk = " PK" if col in tpk else ""
            # quote_column() only for DISPLAY. `col` stays raw for the PK comparison above, and
            # every other consumer (primary keys, FK lines, value hints) keeps the bare name.
            lines.append(f"  - {quote_column(col)} {typ}{null}{pk}")
        if table in dup_keys:
            lines.append(
                f"  ⚠ MANY ROWS PER {dup_keys[table]} — de-duplicate (COUNT(DISTINCT ...) or "
                f"GROUP BY) before answering per-{dup_keys[table]} questions"
            )
        for rel in fks.get(table, []):
            lines.append(f"  FK: {rel}")
        lines.append("")
    return "\n".join(lines).strip()


def _row_counts(cur) -> list[tuple]:
    """Approximate row count per table, from catalogue statistics — not a table scan.

    Read from sys.dm_db_partition_stats in ONE query, so it is effectively instant no matter how
    much data the tables hold. Used only to notice that data changed, never as a reported figure.

    This is what makes a restart pick up newly loaded DATA and not just structural changes: the
    value hints are built from real values, so they go stale when rows are added even though
    every column stayed the same.
    """
    schemas = visible_schemas(cur)
    placeholders = ",".join("?" for _ in schemas)
    cur.execute(
        f"""
        SELECT s.name, t.name, SUM(p.row_count)
        FROM sys.dm_db_partition_stats p
        JOIN sys.tables  t ON t.object_id = p.object_id
        JOIN sys.schemas s ON s.schema_id = t.schema_id
        WHERE p.index_id IN (0, 1) AND LOWER(s.name) IN ({placeholders})
        GROUP BY s.name, t.name
        ORDER BY s.name, t.name
        """,
        *schemas,
    )
    return cur.fetchall()


def _key_signature(cur) -> list[tuple]:
    """Everything a table's structure can change WITHOUT a column being added, dropped, renamed
    or retyped: primary keys, foreign keys, CHECK constraints, DEFAULT values, and non-primary-key
    unique indexes/constraints.

    None of these appear in INFORMATION_SCHEMA.COLUMNS, so without this a DBA could drop a foreign
    key, add a CHECK constraint, or change a column's default — and the cache would never notice.
    The rendered schema would keep describing relationships, defaults and validation rules that no
    longer exist, silently, with nothing in the log to say so.

    Five catalogue reads, all metadata-only (no table is scanned). Unfiltered by EXCLUDED_TABLES/
    EXCLUDED_COLUMNS, deliberately: the column signature in _live_fingerprint() isn't filtered by
    them either, so this stays consistent with it rather than applying two different rules inside
    one fingerprint. The cost is a rebuild occasionally triggered by a change on a table the LLM
    never sees — harmless, since a rebuild here is a handful of cheap catalogue reads, not a scan.
    """
    schemas = visible_schemas(cur)
    placeholders = ",".join("?" for _ in schemas)
    rows: list[tuple] = []

    cur.execute(
        f"""
        SELECT sch.name, t.name, c.name
        FROM sys.indexes i
        JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id
        JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
        JOIN sys.tables t ON t.object_id = i.object_id
        JOIN sys.schemas sch ON sch.schema_id = t.schema_id
        WHERE i.is_primary_key = 1 AND LOWER(sch.name) IN ({placeholders})
        ORDER BY sch.name, t.name, c.name
        """,
        *schemas,
    )
    rows += [("PK", *r) for r in cur.fetchall()]

    cur.execute(
        f"""
        SELECT sch.name, t.name, c.name, rsch.name, rt.name, rc.name
        FROM sys.foreign_key_columns fkc
        JOIN sys.tables t ON t.object_id = fkc.parent_object_id
        JOIN sys.schemas sch ON sch.schema_id = t.schema_id
        JOIN sys.columns c ON c.object_id = fkc.parent_object_id AND c.column_id = fkc.parent_column_id
        JOIN sys.tables rt ON rt.object_id = fkc.referenced_object_id
        JOIN sys.schemas rsch ON rsch.schema_id = rt.schema_id
        JOIN sys.columns rc ON rc.object_id = fkc.referenced_object_id AND rc.column_id = fkc.referenced_column_id
        WHERE LOWER(sch.name) IN ({placeholders})
        ORDER BY sch.name, t.name, c.name
        """,
        *schemas,
    )
    rows += [("FK", *r) for r in cur.fetchall()]

    cur.execute(
        f"""
        SELECT sch.name, t.name, cc.name, cc.definition
        FROM sys.check_constraints cc
        JOIN sys.tables t ON t.object_id = cc.parent_object_id
        JOIN sys.schemas sch ON sch.schema_id = t.schema_id
        WHERE LOWER(sch.name) IN ({placeholders})
        ORDER BY sch.name, t.name, cc.name
        """,
        *schemas,
    )
    rows += [("CHECK", *r) for r in cur.fetchall()]

    cur.execute(
        f"""
        SELECT sch.name, t.name, c.name, dc.definition
        FROM sys.default_constraints dc
        JOIN sys.tables t ON t.object_id = dc.parent_object_id
        JOIN sys.columns c ON c.object_id = dc.parent_object_id AND c.column_id = dc.parent_column_id
        JOIN sys.schemas sch ON sch.schema_id = t.schema_id
        WHERE LOWER(sch.name) IN ({placeholders})
        ORDER BY sch.name, t.name, c.name
        """,
        *schemas,
    )
    rows += [("DEFAULT", *r) for r in cur.fetchall()]

    cur.execute(
        f"""
        SELECT sch.name, t.name, i.name, c.name
        FROM sys.indexes i
        JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id
        JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
        JOIN sys.tables t ON t.object_id = i.object_id
        JOIN sys.schemas sch ON sch.schema_id = t.schema_id
        WHERE i.is_unique = 1 AND i.is_primary_key = 0 AND LOWER(sch.name) IN ({placeholders})
        ORDER BY sch.name, t.name, i.name, ic.key_ordinal
        """,
        *schemas,
    )
    rows += [("UNIQUE", *r) for r in cur.fetchall()]

    return rows


def _live_fingerprint(cur) -> str:
    """Stable hash of the live database: structure, keys/constraints, and how many rows each
    table holds.

    Metadata-only catalogue queries throughout — no table is scanned, so the cost does not grow
    with the data. The hash changes when a table or column is added, dropped, renamed or retyped,
    when a column's nullability, primary key, foreign key, CHECK constraint, DEFAULT, or non-PK
    unique index changes, when ALLOWED_SCHEMAS or EXCLUDED_TABLES/EXCLUDED_COLUMNS changes, and
    when rows are inserted or deleted.
    """
    schemas = visible_schemas(cur)
    placeholders = ",".join("?" for _ in schemas)
    cur.execute(
        f"""
        SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH,
               IS_NULLABLE
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE LOWER(TABLE_SCHEMA) IN ({placeholders})
        ORDER BY TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME
        """,
        *schemas,
    )
    h = hashlib.sha256()
    # The catalogue query above is NOT filtered by EXCLUDED_TABLES, so editing that setting would
    # leave the hash unchanged and the cache would keep serving a schema that still lists the
    # tables you just hid. Fold the setting into the hash explicitly so a change rebuilds.
    h.update(("!excluded=" + ",".join(sorted(settings.excluded_tables))).encode("utf-8"))
    h.update(("!excluded_cols=" + ",".join(sorted(settings.excluded_columns))).encode("utf-8"))
    # Same reasoning as the two settings above: the database structure is unchanged when only the
    # RENDERING changes, so the cache would keep serving text built by the previous renderer.
    h.update(("!render=" + _RENDER_VERSION).encode("utf-8"))
    # WHICH database this is, not just what shape it has. Two different databases can have an
    # IDENTICAL structure - a restored copy, or dev vs production - and every signal below is
    # structural, so the fingerprint would match and the caches would be served unchanged.
    #
    # For schema.txt that is harmless (same structure renders the same text). For value_hints.txt
    # it is not: those are REAL VALUES read out of the data (category codes, source names, statuses),
    # injected so the SQL Author filters on codes that actually exist. Carrying them over to a
    # different database means filtering on values from the wrong system - which returns zero rows
    # and looks like a confident, correct "nothing found" answer.
    #
    # Folding the identity in means a connection change invalidates the fingerprint, which makes
    # build_schema_text() take its drift branch, which also deletes the value-hints cache.
    h.update((f"!db={settings.db_server}:{settings.db_port}/{settings.db_name}").encode("utf-8"))
    h.update(b"\n")
    for row in cur.fetchall():
        h.update("|".join("" if v is None else str(v) for v in row).encode("utf-8"))
        h.update(b"\n")

    # Row counts need VIEW DATABASE STATE. A login without it still gets structure-only drift
    # detection rather than a failed startup.
    try:
        for row in _row_counts(cur):
            h.update(("#" + "|".join(str(v) for v in row)).encode("utf-8"))
            h.update(b"\n")
    except Exception as exc:  # noqa: BLE001
        log.info("schema: row-count signal unavailable (%s) - tracking structure only", exc)

    # Keys/constraints live in separate catalogues from INFORMATION_SCHEMA.COLUMNS, so a key or
    # constraint changing alone would otherwise leave the hash — and the cache — untouched.
    try:
        for row in _key_signature(cur):
            h.update(("$" + "|".join("" if v is None else str(v) for v in row)).encode("utf-8"))
            h.update(b"\n")
    except Exception as exc:  # noqa: BLE001
        log.info("schema: key/constraint signal unavailable (%s) - tracking columns only", exc)
    return h.hexdigest()


def _read_fingerprint() -> str:
    try:
        with open(_FINGERPRINT_PATH, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def build_schema_text(use_cache: bool = True) -> str:
    """Introspect the DB and return the schema block for prompts (cached to .cache/schema.txt).

    With use_cache=True the cache is used ONLY if the live database still matches the structure it
    was built from. If the data team adds a table or column, the fingerprint differs and the cache
    is rebuilt automatically on the next start — no one has to remember to run the refresh command.
    """
    cached = ""
    if use_cache and os.path.exists(_CACHE_PATH):
        with open(_CACHE_PATH, encoding="utf-8") as fh:
            cached = fh.read().strip()

    conn = get_connection()
    try:
        cur = conn.cursor()
        if cached:
            try:
                live = _live_fingerprint(cur)
            except Exception as exc:  # noqa: BLE001 - a drift check must never break startup
                log.warning("schema: drift check failed (%s) - using the cached schema", exc)
                return cached
            if live and live == _read_fingerprint():
                return cached
            log.warning(
                "schema: the database structure changed since the cache was built - rebuilding"
            )
            # The value hints describe the same structure, so they are stale too. Drop them here
            # and the next build_value_hints() call re-reads them from the live database — a new
            # lookup table would otherwise appear in the schema but never in the hints.
            try:
                if os.path.exists(_VALUE_CACHE_PATH):
                    os.remove(_VALUE_CACHE_PATH)
                    log.info("schema: value hints invalidated, they will be rebuilt")
            except OSError as exc:  # noqa: BLE001 - never let cache cleanup break startup
                log.warning("schema: could not invalidate value hints (%s)", exc)

        tables = _fetch_columns(cur)
        visible = set(tables.keys())
        pks = _fetch_primary_keys(cur)
        fks = _fetch_foreign_keys(cur, visible)
        try:
            sizes = {f"{s}.{t}": (n or 0) for s, t, n in _row_counts(cur)}
        except Exception:  # noqa: BLE001 - fall back to counting per table
            sizes = {}
        dup_keys = _detect_duplicate_keys(cur, tables, pks, sizes)
        text = _render(tables, pks, fks, dup_keys)
        fingerprint = _live_fingerprint(cur)
    finally:
        conn.close()

    # Never cache an empty schema (e.g. wrong ALLOWED_SCHEMAS) — that would silently degrade later runs.
    if not text.strip():
        return text
    os.makedirs(os.path.dirname(_CACHE_PATH), exist_ok=True)
    with open(_CACHE_PATH, "w", encoding="utf-8") as fh:
        fh.write(text)
    with open(_FINGERPRINT_PATH, "w", encoding="utf-8") as fh:
        fh.write(fingerprint)
    log.info("schema: rebuilt from the live database (%d tables)", text.count("\nTABLE ") + 1)
    return text


# ── Value / entity hints ────────────────────────────────────────────────────────
# Real coded values from small lookup tables, injected into the prompt so the SQL Author filters
# and decodes using ACTUAL values instead of guessing them.
#
# Nothing here names a specific table or column. A lookup table is recognised by shape — few rows,
# few columns, and a name/description-ish text column — so this follows whatever database is
# configured. Previously it read only the `ref` schema and listed this database's own column
# names, which meant status/type lookups elsewhere were never sampled.
_VALUE_CACHE_PATH = os.path.join(os.path.dirname(_CACHE_PATH), "value_hints.txt")
# Suffixes that mark a column as a human-readable label or code, in any schema.
_HINT_COL_SUFFIXES = ("name", "code", "description", "desc", "label", "status", "type")
_HINT_MAX_TABLES = 60
_HINT_MAX_VALUES = 30
# A lookup table is small by definition. Above this it is transactional data, not a code list.
_HINT_MAX_ROWS = 50


def _is_hint_column(col: str) -> bool:
    lc = col.lower()
    return lc.endswith(_HINT_COL_SUFFIXES) or lc in ("code", "name")


def build_value_hints(use_cache: bool = True) -> str:
    if use_cache and os.path.exists(_VALUE_CACHE_PATH):
        with open(_VALUE_CACHE_PATH, encoding="utf-8") as fh:
            return fh.read().strip()
    conn = get_connection()
    try:
        cur = conn.cursor()
        schemas = visible_schemas(cur)
        placeholders = ",".join("?" for _ in schemas)
        cur.execute(
            f"""
            SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE LOWER(TABLE_SCHEMA) IN ({placeholders})
            ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION
            """,
            *schemas,
        )
        table_cols: dict[tuple[str, str], list[str]] = {}
        for sch, tbl, col in cur.fetchall():
            if _is_excluded(sch, tbl) or _is_secret(col) or _is_excluded_column(sch, tbl, col):
                continue
            if _is_hint_column(col):
                table_cols.setdefault((sch, tbl), []).append(col)

        lines = [
            "VALUE HINTS (real coded values from the lookup tables — filter, join and decode "
            "using these ACTUAL values rather than guessing them):"
        ]
        # Table sizes come from catalogue statistics in one query, so a large transactional table
        # is skipped WITHOUT being queried at all. Previously every candidate was counted first,
        # which meant scanning the biggest tables only to discard them.
        try:
            sizes = {f"{s}.{t}": (n or 0) for s, t, n in _row_counts(cur)}
        except Exception:  # noqa: BLE001
            sizes = {}

        for (sch, tbl), cols in list(table_cols.items())[:_HINT_MAX_TABLES]:
            known = sizes.get(f"{sch}.{tbl}")
            if known is not None and known > _HINT_MAX_ROWS:
                continue  # transactional data, not a code list
            col_sql = ", ".join(f"[{c}]" for c in cols)
            try:
                if known is None:
                    cur.execute(f"SELECT COUNT(*) FROM [{sch}].[{tbl}]")
                    if (cur.fetchone()[0] or 0) > _HINT_MAX_ROWS:
                        continue
                cur.execute(
                    f"SELECT DISTINCT TOP {_HINT_MAX_VALUES} {col_sql} "
                    f"FROM [{sch}].[{tbl}] ORDER BY {col_sql}"
                )
                rows = cur.fetchall()
            except Exception:  # noqa: BLE001 - a bad/empty table must not break hint building
                continue
            if not rows:
                continue
            vals = ["|".join("" if v is None else str(v).strip() for v in r) for r in rows]
            lines.append(f"- {sch}.{tbl} ({', '.join(cols)}): " + "; ".join(vals))
        text = "\n".join(lines) if len(lines) > 1 else ""
    finally:
        conn.close()

    if not text.strip():
        return text
    os.makedirs(os.path.dirname(_VALUE_CACHE_PATH), exist_ok=True)
    with open(_VALUE_CACHE_PATH, "w", encoding="utf-8") as fh:
        fh.write(text)
    return text


def refresh(verbose: bool = True) -> tuple[str, str]:
    """Force a full re-introspection, overwriting both caches.

    Use this after the database or its schema changes - the cached files are otherwise
    reused forever and would describe the OLD database.
    """
    global _resolved_schemas
    _resolved_schemas = None
    for path in (_CACHE_PATH, _VALUE_CACHE_PATH, _FINGERPRINT_PATH):
        if os.path.exists(path):
            os.remove(path)
            if verbose:
                print(f"  removed {os.path.basename(path)}")

    schema = build_schema_text(use_cache=False)
    hints = build_value_hints(use_cache=False)

    if verbose:
        tables = schema.count("\nTABLE ") + (1 if schema.startswith("TABLE ") else 0)
        print(f"  schema.txt      : {tables} tables, {len(schema)} chars")
        print(f"  value_hints.txt : {len(hints)} chars")
        if not schema.strip():
            print("  WARNING: empty schema - check DB_NAME / ALLOWED_SCHEMAS / the login's permissions in .env")
    return schema, hints


if __name__ == "__main__":
    # Run with:  python -m app.db.introspect      (from the project root)
    from app.config import settings

    print(f"Re-introspecting {settings.db_name} on {settings.db_server} "
          f"(schemas: {', '.join(settings.allowed_schemas) or 'all user schemas'})")
    refresh()
    print("Done. Restart the chatbot to pick up the new schema.")
