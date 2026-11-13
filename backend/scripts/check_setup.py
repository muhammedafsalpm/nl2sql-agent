"""Check that everything the assistant needs is in place:  python scripts/check_setup.py

Run it from the backend/ folder after filling in .env. It changes nothing; it only reads.

  1. .env and the identity settings
  2. an ODBC driver for SQL Server
  3. the database login (connects, and warns if the login can WRITE)
  4. which schemas and how many tables the assistant will see
  5. your domain files (business_rules.md, few_shots.md, golden.jsonl)
  6. the LLM settings (add --llm to also make one tiny test call)

Exit code 0 when nothing failed (warnings allowed), 1 otherwise.
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Windows consoles default to cp1252; keep the status symbols from crashing the script.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from app.config import settings  # noqa: E402

_failures = 0


def ok(msg: str) -> None:
    print(f"  [ OK ]  {msg}")


def warn(msg: str) -> None:
    print(f"  [WARN]  {msg}")


def fail(msg: str) -> None:
    global _failures
    _failures += 1
    print(f"  [FAIL]  {msg}")


def section(title: str) -> None:
    print(f"\n{title}")


def check_env() -> None:
    section("1. Settings (.env)")
    backend = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if os.path.exists(os.path.join(backend, ".env")):
        ok("backend/.env found")
    else:
        fail("backend/.env not found - copy .env.example to .env and fill it in")
    ok(f"APP_NAME = {settings.app_name}")
    if settings.app_description:
        ok(f"APP_DESCRIPTION = {settings.app_description}")
    else:
        warn("APP_DESCRIPTION is blank - optional, but one sentence about your data helps the agents")
    missing = [k for k, v in (("DB_SERVER", settings.db_server), ("DB_NAME", settings.db_name),
                              ("DB_USER", settings.db_user)) if not v]
    if missing:
        fail(f"missing database settings: {', '.join(missing)}")


def check_driver() -> bool:
    section("2. ODBC driver")
    try:
        from app.db.connection import _pick_driver

        ok(f"using '{_pick_driver()}'")
        return True
    except ImportError as exc:
        fail(f"pyodbc is not installed ({exc}) - run: pip install -r requirements.txt")
    except Exception as exc:  # noqa: BLE001
        fail(str(exc))
    return False


def check_database() -> bool:
    section("3. Database login")
    from app.db.connection import get_connection

    try:
        conn = get_connection()
    except Exception as exc:  # noqa: BLE001
        fail(f"cannot connect to {settings.db_server}:{settings.db_port}/{settings.db_name}: {exc}")
        return False
    try:
        cur = conn.cursor()
        cur.execute("SELECT DB_NAME(), SUSER_SNAME(), CAST(SERVERPROPERTY('ProductVersion') AS nvarchar(50))")
        db, login, version = cur.fetchone()
        ok(f"connected to '{db}' as '{login}' (SQL Server {version})")
        cur.execute(
            """
            SELECT IS_SRVROLEMEMBER('sysadmin'),
                   IS_ROLEMEMBER('db_owner'),
                   IS_ROLEMEMBER('db_datawriter'),
                   IS_ROLEMEMBER('db_ddladmin'),
                   HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'INSERT'),
                   HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'ALTER')
            """
        )
        sysadmin, owner, writer, ddl, can_insert, can_alter = cur.fetchone()
        risky = [name for name, flag in (("sysadmin", sysadmin), ("db_owner", owner),
                                         ("db_datawriter", writer), ("db_ddladmin", ddl),
                                         ("INSERT permission", can_insert),
                                         ("ALTER permission", can_alter)) if flag == 1]
        if risky:
            warn("this login can MODIFY the database (" + ", ".join(risky) + "). The assistant only "
                 "runs SELECTs, but use a read-only login (db_datareader) - it is the real safety "
                 "boundary")
        else:
            ok("login is read-only")
        return True
    except Exception as exc:  # noqa: BLE001
        warn(f"could not check permissions ({exc})")
        return True
    finally:
        conn.close()


def check_schema() -> None:
    section("4. What the assistant will see")
    from app.db.connection import get_connection
    from app.db.introspect import visible_schemas

    conn = get_connection()
    try:
        cur = conn.cursor()
        schemas = visible_schemas(cur)
        where = "configured" if settings.allowed_schemas else "auto-detected (ALLOWED_SCHEMAS blank)"
        ok(f"schemas {where}: {', '.join(schemas)}")
        placeholders = ",".join("?" for _ in schemas)
        cur.execute(
            f"""
            SELECT LOWER(TABLE_SCHEMA), COUNT(*)
            FROM INFORMATION_SCHEMA.TABLES
            WHERE LOWER(TABLE_SCHEMA) IN ({placeholders})
            GROUP BY LOWER(TABLE_SCHEMA)
            """,
            *schemas,
        )
        counts = dict(cur.fetchall())
        total = sum(counts.values())
        if total == 0:
            fail("no tables or views visible - check ALLOWED_SCHEMAS and the login's permissions")
        else:
            ok(f"{total} tables/views visible: "
               + ", ".join(f"{s} ({n})" for s, n in sorted(counts.items())))
        empty = [s for s in schemas if s not in counts]
        if empty and settings.allowed_schemas:
            warn(f"listed in ALLOWED_SCHEMAS but empty or not readable: {', '.join(empty)}")
    except Exception as exc:  # noqa: BLE001
        fail(f"could not read the schema: {exc}")
    finally:
        conn.close()


def check_domain() -> None:
    section(f"5. Domain files ({settings.domain_dir})")
    if not os.path.isdir(settings.domain_dir):
        fail("DOMAIN_DIR does not exist")
        return
    from app.graph import prompts

    if prompts.BUSINESS_RULES:
        ok(f"business_rules.md loaded ({len(prompts.BUSINESS_RULES):,} chars)")
    else:
        warn("business_rules.md is empty or still the template - optional, but rules improve accuracy")
    if prompts.FEW_SHOTS:
        shots = len(re.findall(r"^Q:", prompts.FEW_SHOTS, re.MULTILINE))
        ok(f"few_shots.md loaded ({shots} example(s))")
    else:
        warn("few_shots.md is empty or still the template - optional, but examples improve accuracy")
    golden = os.path.join(settings.domain_dir, "golden.jsonl")
    if os.path.exists(golden):
        with open(golden, encoding="utf-8") as fh:
            n = sum(1 for line in fh if line.strip())
        ok(f"golden.jsonl has {n} test question(s) - run: python -m eval.run_eval")
    else:
        warn("no golden.jsonl - optional; add test questions to measure accuracy")


def check_llm(call: bool) -> None:
    section("6. LLM")
    provider = settings.llm_provider.lower()
    if provider in ("open", "openai"):
        if settings.openai_api_key:
            ok(f"OpenAI: main model '{settings.openai_model}', fast model '{settings.fast_model}'")
        else:
            fail("LLM_PROVIDER is openai but OPENAI_API_KEY is blank")
            return
    else:
        ok(f"Ollama at {settings.ollama_base_url}: main model '{settings.llm_model}', "
           f"fast model '{settings.fast_model}'")
    if not call:
        print("          (add --llm to make one tiny test call)")
        return
    try:
        from app.llm import chat

        reply = chat("Reply with the single word: ready", "Are you ready?", fast=True)
        ok(f"test call answered: {reply.strip()[:60]!r}")
    except Exception as exc:  # noqa: BLE001
        fail(f"test call failed: {exc}")


def main() -> int:
    print("=" * 70)
    print(f"Setup check - {settings.app_name}")
    print("=" * 70)
    check_env()
    if check_driver() and check_database():
        check_schema()
    check_domain()
    check_llm(call="--llm" in sys.argv)
    print()
    if _failures:
        print(f"{_failures} problem(s) found - fix the [FAIL] lines above.")
        return 1
    print("All required checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
