"""
snapshot_tables.py
==================

Auto-append to the three analytics snapshot tables that were backfilled from
the July 2026 Google Drive corpus:

    RAW.MISC.PET_VALUE_PARENT_SNAPSHOT     one row per (pmc, parent, run)
    RAW.MISC.PET_VALUE_PROPERTY_SNAPSHOT   one row per (pmc, property, run)
    RAW.MISC.PET_VALUE_USERS_SNAPSHOT      one row per flagged person/pet per run

Every report generation (app Summary tab, batch_pdf.py, batch_csv.py) calls
into this module so the tables keep growing on their own — the same pattern
as the CACHED_PET_VALUE_DATA fetch cache, but modeled/typed instead of raw
VARIANT rows. Readers should take the latest RUN_DATE/RUN_TS per id.

Config (env / .env):
    PET_VALUE_SNAPSHOT_SCHEMA   default "RAW.MISC"
    PET_VALUE_SNAPSHOT_APPEND   "0" disables all appends (default on)

Rules:
  - Append-only; a failed append never breaks a report run (print + continue).
  - Missing metrics are NULL, never 0 — 0 means "measured as zero".
  - New columns (ESA bucket) are added with ALTER ... IF NOT EXISTS so the
    manually-created tables upgrade themselves on first append.
"""
from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    pass

_SCHEMA_SAFE = re.compile(r"^[A-Za-z0-9_.]+$")


def _schema() -> str:
    s = os.getenv("PET_VALUE_SNAPSHOT_SCHEMA", "RAW.MISC").strip()
    if not _SCHEMA_SAFE.match(s):
        raise ValueError(f"Unsafe snapshot schema: {s!r}")
    return s


def enabled() -> bool:
    return os.getenv("PET_VALUE_SNAPSHOT_APPEND", "1").strip() != "0"


_conn_singleton = None


def _connection(connection_factory=None):
    global _conn_singleton
    if connection_factory is not None:
        return connection_factory()
    if _conn_singleton is None:
        from snowflake_auth import get_snowflake_connection
        _conn_singleton = get_snowflake_connection()
    return _conn_singleton


# ─── Column sets (must match the DDL the tables were created with) ──────

USERS_COLUMNS = [
    "PMC", "REPORT_TYPE", "RUN_TS", "PROPERTY_ID", "PROPERTY_NAME",
    "PARENT_ID", "PARENT_NAME", "USER_FIRST_NAME", "USER_LAST_NAME",
    "USER_EMAIL", "TENANT_CODE", "PET_ID", "PET_NAME", "SPECIES", "BREED",
    "PET_PROFILE_TYPE", "PET_PROFILE_STATUS", "PET_PROFILE_URL",
    "PET_LEVEL_COMPLIANCE_STATUS", "USER_LEVEL_COMPLIANCE_STATUS",
    "USER_PROFILE_URL", "USER_PET_TYPE", "USER_PET_STATUS",
    "COMPLIANCE_STATUS", "HOUSEHOLD_PROFILE_STARTED_ALLTIME",
    "ASSISTANCE_PROFILE_STARTED_ALLTIME", "SUSPECTED_REASON", "UNIT_ID",
    "UNIT_ADDRESS_1", "UNIT_ADDRESS_2", "FULL_UNIT_ADDRESS", "RESIDENT_TYPE",
    "LEASE_START_DATE", "LEASE_END_DATE", "PET_RENT_PAID", "OVERALL_STATUS",
    "SOURCE_FILE", "PROFILE_BUCKET",
]

_SHARED_METRICS = [
    "COMPARABLE_COUNT", "COMPARABLE_UNITS", "PRE_REVENUE", "CURRENT_REVENUE",
    "POST_AVG_REVENUE", "MONTHLY_LIFT", "LIFT_PER_UNIT", "ASSET_VALUE_IMPACT",
    "PET_REVENUE_FOUND_MONTHLY", "FOUND_PER_UNIT", "EST_VALUE_IMPACT_FOUND",
    "TOTAL_OPPORTUNITY_MONTHLY", "COMBINED_LIFT_PER_UNIT",
    "COMBINED_EST_VALUE_IMPACT", "COMBINED_OPPORTUNITY_MONTHLY",
    "TOTAL_PORTFOLIO_UNITS", "PROJECTED_ANNUAL_LIFT",
    "PROJECTED_ASSET_VALUE_IMPACT", "UNCOLLECTED_TENANTS",
    "MISSING_MONTHLY_RENT", "ONE_TIME_UNCOLLECTED", "ANNUAL_REVENUE_IMPACT",
    "SUSPECTED_UNDISCLOSED", "SUSPECTED_MONTHLY", "AVG_UNIT_ADOPTION_PCT",
    "ADDITIONAL_OPPORTUNITY_AT_100PCT", "PROJECTED_REVENUE_AT_100PCT",
    "PROPERTIES_WITH_LAUNCH", "PROPERTIES_WITH_DATA", "TOTAL_PROPERTIES",
    "TOTAL_PROPERTIES_IN_PORTFOLIO", "ANNUALIZED_LIFT",
    # ESA bucket (added July 2026, ALTERed in on first append)
    "MISSING_ESA_TENANTS", "MISSING_ESA_MONTHLY",
]

PROPERTY_COLUMNS = ["PMC", "PROPERTY_ID", "PROPERTY_NAME", "PARENT_ID",
                    "PARENT_NAME", "UNIT_COUNT", "RUN_DATE", "PDF_FILENAME",
                    ] + _SHARED_METRICS

PARENT_COLUMNS = ["PMC", "PARENT_ID", "PARENT_NAME", "PROPERTY_COUNT",
                  "UNIT_COUNT", "RUN_DATE", "PDF_FILENAME"] + _SHARED_METRICS

# Columns that did not exist in the original manually-loaded tables.
_NEW_COLUMNS = {
    "PET_VALUE_USERS_SNAPSHOT": [("PROFILE_BUCKET", "VARCHAR(32)")],
    "PET_VALUE_PROPERTY_SNAPSHOT": [("MISSING_ESA_TENANTS", "NUMBER"),
                                    ("MISSING_ESA_MONTHLY", "NUMBER(18,2)")],
    "PET_VALUE_PARENT_SNAPSHOT": [("MISSING_ESA_TENANTS", "NUMBER"),
                                  ("MISSING_ESA_MONTHLY", "NUMBER(18,2)")],
}

_ensured: set = set()


def _ensure_columns(cur, table_short: str) -> None:
    if table_short in _ensured:
        return
    for col, coltype in _NEW_COLUMNS.get(table_short, []):
        try:
            cur.execute(f"ALTER TABLE {_schema()}.{table_short} "
                        f"ADD COLUMN IF NOT EXISTS {col} {coltype}")
        except Exception as exc:  # noqa: BLE001 — older Snowflake or no grant
            print(f"[snapshot_tables] could not add {table_short}.{col}: "
                  f"{str(exc)[:120]}")
    _ensured.add(table_short)


def _insert(table_short: str, columns: List[str], rows: List[List[Any]],
            connection_factory=None) -> int:
    """Chunked parameterized INSERT. Returns rows written (0 on failure)."""
    if not rows:
        return 0
    conn = _connection(connection_factory)
    cur = conn.cursor()
    try:
        _ensure_columns(cur, table_short)
        col_sql = ", ".join(columns)
        placeholders = "(" + ", ".join(["%s"] * len(columns)) + ")"
        written = 0
        for start in range(0, len(rows), 500):
            chunk = rows[start:start + 500]
            values_sql = ", ".join([placeholders] * len(chunk))
            params: List[Any] = []
            for r in chunk:
                params.extend(r)
            cur.execute(
                f"INSERT INTO {_schema()}.{table_short} ({col_sql}) "
                f"VALUES {values_sql}", params)
            written += len(chunk)
        return written
    finally:
        cur.close()


def _num(v) -> Optional[float]:
    """NULL-preserving numeric coercion ('' / None / 'nan' -> None)."""
    if v is None:
        return None
    s = str(v).strip()
    if s == "" or s.lower() in ("nan", "none"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _s(v) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s if s and s.lower() not in ("nan", "none") else None


def _date(v) -> Optional[str]:
    """First 10 chars of a date-ish value ('2026-07-01 00:00:00' -> DATE-safe)."""
    s = _s(v)
    return s[:10] if s else None


# ─── Users snapshot ──────────────────────────────────────────────────────

def append_users_snapshot(missing_df=None, suspected_df=None,
                          pmc_system: str = "", run_label: str = "",
                          parent_id: str = "", parent_name: str = "",
                          connection_factory=None) -> int:
    """Append the person-level rows of a report run.

    ``missing_df`` is generate_missing_pet_rent_report output (per-pet rows,
    includes the ESA bucket); ``suspected_df`` is
    generate_suspected_undisclosed_report output (per-profile rows).
    """
    if not enabled():
        return 0
    run_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    src = f"live_run::{run_label}"[:250]

    def g(row, name):
        for k in (name, name.upper(), name.lower()):
            if k in row:
                return row[k]
        return None

    rows: List[List[Any]] = []

    def _one(row, report_type):
        # Types follow the manually-loaded tables (Snowsight inferred them
        # from the backfill CSVs): ids and boolean-ish flags are NUMBER,
        # lease dates are DATE.
        bucket = _s(g(row, "profile_bucket"))
        return [
            _s(pmc_system), report_type, run_ts,
            _num(g(row, "PROPERTY_ID")), _s(g(row, "PROPERTY_NAME")),
            _num(parent_id), _s(parent_name),
            _s(g(row, "USER_FIRST_NAME")), _s(g(row, "USER_LAST_NAME")),
            (_s(g(row, "USER_EMAIL")) or "").lower() or None,
            _s(g(row, "TENANT_CODE")),
            _s(g(row, "PET_ID")), _s(g(row, "PET_NAME")),
            _s(g(row, "SPECIES")), _s(g(row, "BREED")),
            _s(g(row, "PET_PROFILE_TYPE")), _s(g(row, "PET_PROFILE_STATUS")),
            _s(g(row, "PET_PROFILE_URL")),
            _s(g(row, "PET_LEVEL_COMPLIANCE_STATUS")),
            _s(g(row, "USER_LEVEL_COMPLIANCE_STATUS")),
            _s(g(row, "USER_PROFILE_URL")), _s(g(row, "USER_PET_TYPE")),
            _s(g(row, "USER_PET_STATUS")), _s(g(row, "COMPLIANCE_STATUS")),
            _num(g(row, "HOUSEHOLD_PROFILE_STARTED_ALLTIME")),
            _num(g(row, "ASSISTANCE_PROFILE_STARTED_ALLTIME")),
            _s(g(row, "SUSPECTED_REASON")), _s(g(row, "UNIT_ID")),
            _s(g(row, "UNIT_ADDRESS_1")), _s(g(row, "UNIT_ADDRESS_2")),
            _s(g(row, "FULL_UNIT_ADDRESS")), _s(g(row, "RESIDENT_TYPE")),
            _date(g(row, "LEASE_START_DATE")), _date(g(row, "LEASE_END_DATE")),
            _num(g(row, "pet_rent_paid")) or 0, _s(g(row, "overall_status")),
            src, bucket,
        ]

    for df, rtype in ((missing_df, "missing_pet_rent"),
                      (suspected_df, "suspected_undisclosed_pets")):
        if df is None or getattr(df, "empty", True):
            continue
        for _, row in df.iterrows():
            rows.append(_one(row, rtype))

    try:
        n = _insert("PET_VALUE_USERS_SNAPSHOT", USERS_COLUMNS, rows,
                    connection_factory)
        if n:
            print(f"[snapshot_tables] users snapshot +{n} rows ({run_label})")
        return n
    except Exception as exc:  # noqa: BLE001 — never break a report run
        print(f"[snapshot_tables] users append failed: {str(exc)[:200]}")
        return 0


# ─── Property / parent snapshots ────────────────────────────────────────

def append_property_snapshot(rows: List[Dict[str, Any]],
                             connection_factory=None) -> int:
    """Append per-property metric rows. Each dict may carry any subset of
    PROPERTY_COLUMNS (case-insensitive keys); the rest go in as NULL."""
    if not enabled() or not rows:
        return 0
    numeric = set(_SHARED_METRICS) | {"UNIT_COUNT", "PROPERTY_ID", "PARENT_ID"}
    out = []
    for r in rows:
        upper = {str(k).upper(): v for k, v in r.items()}
        upper.setdefault("RUN_DATE", datetime.now().strftime("%Y-%m-%d"))
        out.append([
            (_num(upper.get(c)) if c in numeric else _s(upper.get(c)))
            for c in PROPERTY_COLUMNS
        ])
    try:
        n = _insert("PET_VALUE_PROPERTY_SNAPSHOT", PROPERTY_COLUMNS, out,
                    connection_factory)
        if n:
            print(f"[snapshot_tables] property snapshot +{n} rows")
        return n
    except Exception as exc:  # noqa: BLE001
        print(f"[snapshot_tables] property append failed: {str(exc)[:200]}")
        return 0


def append_parent_snapshot(row: Dict[str, Any], connection_factory=None) -> int:
    """Append one parent-level metric row (same key semantics as property)."""
    if not enabled() or not row:
        return 0
    numeric = set(_SHARED_METRICS) | {"UNIT_COUNT", "PROPERTY_COUNT", "PARENT_ID"}
    upper = {str(k).upper(): v for k, v in row.items()}
    upper.setdefault("RUN_DATE", datetime.now().strftime("%Y-%m-%d"))
    vals = [(_num(upper.get(c)) if c in numeric else _s(upper.get(c)))
            for c in PARENT_COLUMNS]
    try:
        n = _insert("PET_VALUE_PARENT_SNAPSHOT", PARENT_COLUMNS, [vals],
                    connection_factory)
        if n:
            print(f"[snapshot_tables] parent snapshot +1 row "
                  f"({upper.get('PARENT_NAME', '')})")
        return n
    except Exception as exc:  # noqa: BLE001
        print(f"[snapshot_tables] parent append failed: {str(exc)[:200]}")
        return 0


def build_property_rows_from_report(pmc_system, missing_rent_data,
                                    suspected_data, monthly_by_prop,
                                    latest_month, pid_by_name,
                                    doors_by_name=None,
                                    parent_id: str = "",
                                    parent_name: str = "") -> List[Dict[str, Any]]:
    """Assemble property snapshot rows from the structures every report run
    already has in hand. Only observed metrics are set; the PDF-derived
    columns not computable per-property here stay NULL."""
    doors_by_name = doors_by_name or {}
    names = set(missing_rent_data or {}) | set(suspected_data or {}) | set(monthly_by_prop or {})
    rows = []
    for pname in sorted(names):
        mr = (missing_rent_data or {}).get(pname) or {}
        su = (suspected_data or {}).get(pname) or {}
        cur_rev = (monthly_by_prop or {}).get(pname, {}).get(latest_month) if latest_month else None
        n_miss = mr.get("missing_count")
        avg_rec = mr.get("avg_recurring", 0) or 0
        n_susp = su.get("missing_count")
        rows.append({
            "PMC": pmc_system,
            "PROPERTY_ID": (pid_by_name or {}).get(pname),
            "PROPERTY_NAME": pname,
            "PARENT_ID": parent_id,
            "PARENT_NAME": parent_name,
            "UNIT_COUNT": doors_by_name.get(pname),
            "PDF_FILENAME": None,
            "CURRENT_REVENUE": cur_rev,
            "UNCOLLECTED_TENANTS": n_miss,
            "MISSING_MONTHLY_RENT": (n_miss * avg_rec) if n_miss is not None else None,
            "SUSPECTED_UNDISCLOSED": n_susp,
            "SUSPECTED_MONTHLY": (n_susp * avg_rec) if (n_susp is not None and avg_rec) else None,
            "MISSING_ESA_TENANTS": mr.get("esa_missing_count"),
            "MISSING_ESA_MONTHLY": (mr.get("esa_missing_count", 0) * avg_rec)
                                   if mr.get("esa_missing_count") is not None else None,
        })
    return rows
