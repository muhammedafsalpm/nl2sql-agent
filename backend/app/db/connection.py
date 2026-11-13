"""Read-only-intent MS SQL connection via pyodbc.

The app never issues writes; combined with the Validator (SELECT-only) this keeps the
database safe even though the configured login may be privileged.
"""
from __future__ import annotations

import datetime
import struct

import pyodbc

from app.config import settings

# pyodbc has no built-in decoder for SQL Server's `datetimeoffset` type (ODBC type code -155,
# SQL_SS_TIMESTAMPOFFSET) - fetching ANY column of this type raises
# "ODBC SQL type -155 is not yet supported" regardless of the value, even when it is NULL for
# every row in the table. No SQL the app writes can avoid this; it is a driver gap, not a query
# problem. Audit/event tables commonly use it, e.g. a `created_at datetimeoffset` column.
#
# The fix is the converter Microsoft's own ODBC samples document for this type: the driver hands
# back the raw fixed-layout bytes, and this unpacks them into a normal timezone-aware datetime.
_SQL_SS_TIMESTAMPOFFSET = -155


def _decode_datetimeoffset(raw: bytes) -> datetime.datetime:
    year, month, day, hour, minute, second, frac_100ns, tz_hour, tz_minute = struct.unpack(
        "<6hI2h", raw
    )
    return datetime.datetime(
        year, month, day, hour, minute, second, frac_100ns // 1000,
        tzinfo=datetime.timezone(datetime.timedelta(hours=tz_hour, minutes=tz_minute)),
    )

# Preferred driver order when DB_DRIVER is not pinned in .env.
_DRIVER_CANDIDATES = [
    "ODBC Driver 18 for SQL Server",
    "ODBC Driver 17 for SQL Server",
    "SQL Server",
]


def _pick_driver() -> str:
    if settings.db_driver:
        return settings.db_driver
    installed = {d.strip() for d in pyodbc.drivers()}
    for cand in _DRIVER_CANDIDATES:
        if cand in installed:
            return cand
    if installed:
        return sorted(installed)[-1]
    raise RuntimeError(
        "No ODBC driver found. Install 'ODBC Driver 18 for SQL Server' (or 17)."
    )


def _connection_string() -> str:
    driver = _pick_driver()
    return (
        f"DRIVER={{{driver}}};"
        f"SERVER={settings.db_server},{settings.db_port};"
        f"DATABASE={settings.db_name};"
        f"UID={settings.db_user};"
        f"PWD={settings.db_password};"
        f"Encrypt={settings.db_encrypt};"
        f"TrustServerCertificate={settings.db_trust_cert};"
    )


def get_connection() -> pyodbc.Connection:
    """Open a fresh autocommit connection with the configured query timeout.

    Registered per-connection (not via the module-level pyodbc.add_output_converter) because
    every call here opens a brand-new connection - there is no pool to configure once.
    """
    conn = pyodbc.connect(_connection_string(), autocommit=True, timeout=settings.query_timeout)
    conn.timeout = settings.query_timeout
    conn.add_output_converter(_SQL_SS_TIMESTAMPOFFSET, _decode_datetimeoffset)
    return conn


def ping() -> tuple[bool, str]:
    """Health check used at startup. Returns (ok, message)."""
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT DB_NAME(), @@VERSION")
        row = cur.fetchone()
        conn.close()
        return True, f"Connected to {row[0]}"
    except Exception as exc:  # noqa: BLE001 - surface any driver/network error to the user
        return False, str(exc)
