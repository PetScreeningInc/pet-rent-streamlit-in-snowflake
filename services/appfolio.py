"""AppFolio: Snowflake staging-backed queries and charge fetch."""

import pandas as pd
import snowflake.connector
import streamlit as st
from config import JUNK_EMAILS
from services.snowflake_io import get_snowflake_connection

# ─── AppFolio (Snowflake-backed) ─────────────────────────────────────
# Read from staging tables populated by the AppFolio ingestion job. Live
# Developer Space/API access is intentionally deferred behind a disabled
# sidebar toggle until MFA + direct endpoint probing are available.
# pmc_system value: 'appfolio'

def _format_appfolio_date(value):
    """Return YYYY-MM-DD for Snowflake date/datetime values, else blank."""
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d")
    text = str(value).strip()
    if not text or text.lower() in ("none", "nan", "nat"):
        return ""
    return text[:10]


def _normalize_appfolio_snowflake_charge_row(row):
    """Normalize one AppFolio Snowflake recurring-charge row to app schema.

    AppFolio's staged `getrecurringcharges` data often has NULL FREQUENCY even
    though the endpoint itself is recurring-charge data. Treat blank frequency
    as recurring by endpoint semantics so monthly pet rent does not get
    misclassified as one-time just because the field is empty.
    """
    def _get(key, default=""):
        value = row.get(key, default)
        return default if value is None else value

    occupancy_id = _get("OCCUPANCY_ID") or _get("LEASE_ID")
    tenant_code = _get("TENANT_CODE") or occupancy_id
    frequency = str(_get("FREQUENCY") or "").strip().lower() or "recurring"

    return {
        "parent_company": _get("PARENT_COMPANY"),
        "property_id": _get("PROPERTY_ID"),
        "property_name": _get("PROPERTY_NAME"),
        "property_code": _get("PROPERTY_CODE"),
        "launch_date": _format_appfolio_date(_get("LAUNCH_DATE")),
        "unit_code": _get("UNIT_CODE") or _get("APPFOLIO_UNIT_ID"),
        "unit_type": "",
        "market_rent": "",
        "lease_id": occupancy_id,
        "tenant_code": tenant_code,
        "first_name": _get("FIRST_NAME"),
        "last_name": _get("LAST_NAME"),
        "tenant_status": _get("TENANT_STATUS"),
        "lease_from": _format_appfolio_date(_get("LEASE_FROM")),
        "lease_to": _format_appfolio_date(_get("LEASE_TO")),
        "move_in": _format_appfolio_date(_get("MOVE_IN")),
        "move_out": _format_appfolio_date(_get("MOVE_OUT")),
        "email": _get("EMAIL"),
        "charge_code": _get("CHARGE_CODE"),
        "charge_type": _get("CHARGE_TYPE"),
        "charge_amount": _get("CHARGE_AMOUNT"),
        "charge_from_date": _format_appfolio_date(_get("CHARGE_FROM_DATE")),
        "charge_to_date": _format_appfolio_date(_get("CHARGE_TO_DATE")),
        "frequency": frequency,
        "tenant_type": _get("TENANT_TYPE"),
        "primary_tenant": _get("PRIMARY_TENANT"),
        "_appfolio_charge_id": _get("RECURRING_CHARGE_ID"),
        "_appfolio_database_id": _get("DATABASE_ID"),
        "_appfolio_occupancy_id": occupancy_id,
        "_appfolio_unit_id": _get("APPFOLIO_UNIT_ID"),
    }


@st.cache_data(ttl=300)
def load_appfolio_parent_companies():
    """Count AppFolio properties per parent company with staged Snowflake data."""
    conn = get_snowflake_connection()
    cur = conn.cursor(snowflake.connector.DictCursor)
    cur.execute("""
        SELECT
            p.parent_company_name,
            MAX(p.parent_company_ancestry_id)    AS ancestry_id,
            MAX(p.parent_company_ancestry_name)   AS ancestry_name,
            COUNT(DISTINCT p.property_id)         AS total_props,
            COUNT(DISTINCT CASE
                WHEN p.property_status = 'active'
                     AND p.integration_status = 'enabled'
                     AND PARSE_JSON(p.property_source_id):"property_id"::STRING IS NOT NULL
                THEN p.property_id
            END) AS api_props
        FROM PROD.COMMON.D_PROPERTIES p
        WHERE p.parent_company_name IS NOT NULL
          AND p.property_source_name = 'appfolio'
        GROUP BY 1
        HAVING api_props > 0
        ORDER BY 1
    """)
    return cur.fetchall()


@st.cache_data(ttl=300)
def load_appfolio_all_properties():
    conn = get_snowflake_connection()
    cur = conn.cursor(snowflake.connector.DictCursor)
    cur.execute("""
        SELECT DISTINCT
            p.property_id,
            p.property_name,
            p.parent_company_name,
            p.parent_company_ancestry_id
        FROM PROD.COMMON.D_PROPERTIES p
        WHERE p.property_source_name = 'appfolio'
        ORDER BY p.property_name
    """)
    return cur.fetchall()


def load_appfolio_properties_for_selection(parent_company_name=None, property_id=None, ancestry_id=None):
    """Load AppFolio properties matching the selection criteria.

    AppFolio uses the staged property_source_id:property_id as the provider
    property key. We also require active/enabled metadata so the user sees the
    same integration-ready portfolio semantics as RealPage.
    """
    conn = get_snowflake_connection()
    cur = conn.cursor(snowflake.connector.DictCursor)

    where_clause = """
        WHERE p.property_source_name = 'appfolio'
          AND p.property_status = 'active'
          AND p.integration_status = 'enabled'
          AND PARSE_JSON(p.property_source_id):"property_id"::STRING IS NOT NULL
    """
    if parent_company_name:
        _safe = parent_company_name.replace("'", "''")
        where_clause += f" AND p.parent_company_name = '{_safe}'"
    if property_id:
        where_clause += f" AND p.property_id = {int(property_id)}"
    if ancestry_id:
        _safe = str(ancestry_id).replace("'", "''")
        where_clause += f" AND p.parent_company_ancestry_id = '{_safe}'"

    cur.execute(f"""
        SELECT DISTINCT
            PARSE_JSON(p.property_source_id):"property_id"::STRING AS appfolio_property_id,
            p.property_id,
            p.property_name,
            p.parent_company_name,
            PARSE_JSON(p.property_source_id):"property_id"::STRING AS property_code,
            kf.property_launch_date AS property_launch_date
        FROM PROD.COMMON.D_PROPERTIES p
        LEFT JOIN PROD.PETSCREENING.PETSCREENING__PROPERTY_KEY_FACTS kf
            ON p.property_id = kf.property_id
        {where_clause}
        ORDER BY p.property_name
    """)
    return cur.fetchall()


def fetch_appfolio_for_properties(properties, progress_bar, status_text, lookback_months=24):
    """Read AppFolio recurring charges from Snowflake and emit unified rows.

    Returns (all_charges, results_log), matching Yardi/RealPage downstream shape.
    `lookback_months` is kept for interface parity; staged recurring charges
    carry their own effective START_DATE/END_DATE and the UI clips display later.
    """
    if not properties:
        return [], []

    progress_bar.progress(0.05)
    status_text.text(f"Reading AppFolio recurring-charge data from Snowflake for {len(properties)} properties...")

    conn = get_snowflake_connection()
    cur = conn.cursor(snowflake.connector.DictCursor)

    def _esc(s):
        return str(s if s is not None else "").replace("'", "''")

    select_rows = []
    for p in properties:
        appfolio_pid = _esc(p.get("APPFOLIO_PROPERTY_ID") or p.get("appfolio_property_id") or p.get("PROPERTY_CODE") or p.get("property_code") or "")
        pid = int(p.get("PROPERTY_ID") or p.get("property_id") or 0)
        pname = _esc(p.get("PROPERTY_NAME") or p.get("property_name") or "")
        pcode = _esc(p.get("PROPERTY_CODE") or p.get("property_code") or appfolio_pid)
        parent = _esc(p.get("PARENT_COMPANY_NAME") or p.get("parent_company_name") or "")
        launch = p.get("PROPERTY_LAUNCH_DATE") or p.get("property_launch_date")
        launch_str = _format_appfolio_date(launch)
        launch_sql = "NULL" if not launch_str else f"TO_DATE('{launch_str}')"
        select_rows.append(
            f"SELECT '{appfolio_pid}'::STRING AS appfolio_property_id, "
            f"{pid}::NUMBER AS property_id, '{pname}'::STRING AS property_name, "
            f"'{pcode}'::STRING AS property_code, '{parent}'::STRING AS parent_company_name, "
            f"{launch_sql} AS property_launch_date"
        )
    values_sql = "\n        UNION ALL ".join(select_rows)

    junk_email_clause = f"AND LOWER(TRIM(t.email)) NOT IN ({JUNK_EMAILS})"

    sql = f"""
    WITH props AS (
        {values_sql}
    ),
    latest_charges AS (
        SELECT
            c.recurring_charge_id,
            c.database_id,
            c.amount,
            c.description,
            c.frequency,
            c.start_date,
            c.end_date,
            c.occupancy_id,
            c.gl_account_id,
            c.ingested_at
        FROM PROD.STAGING.STG_PMC_INTEGRATIONS_APPFOLIO__GETRECURRINGCHARGES c
        QUALIFY ROW_NUMBER() OVER (
            PARTITION BY c.recurring_charge_id
            ORDER BY c.ingested_at DESC
        ) = 1
    ),
    latest_tenants AS (
        SELECT
            t.tenant_id,
            t.property_id AS appfolio_property_id,
            t.occupancy_id,
            t.unit_id,
            t.first_name,
            t.last_name,
            t.email,
            t.status,
            t.tenant_type,
            t.primary_tenant,
            t.lease_start_date,
            t.lease_end_date,
            t.move_in_on,
            t.move_out_on,
            t.ingested_at
        FROM PROD.STAGING.STG_PMC_INTEGRATIONS_APPFOLIO__GETTENANTS t
        JOIN props p ON p.appfolio_property_id = t.property_id
        WHERE COALESCE(t.status, '') NOT IN ('Future')
          AND t.email IS NOT NULL
          AND TRIM(t.email) <> ''
          {junk_email_clause}
        QUALIFY ROW_NUMBER() OVER (
            PARTITION BY t.tenant_id, t.occupancy_id
            ORDER BY t.ingested_at DESC
        ) = 1
    ),
    tenant_pick AS (
        SELECT *
        FROM latest_tenants
        QUALIFY ROW_NUMBER() OVER (
            PARTITION BY occupancy_id
            ORDER BY
                CASE WHEN primary_tenant THEN 0 ELSE 1 END,
                CASE WHEN tenant_type = 'Financially Responsible' THEN 0 ELSE 1 END,
                CASE status WHEN 'Current' THEN 0 WHEN 'Notice' THEN 1 WHEN 'Past' THEN 2 ELSE 3 END,
                COALESCE(last_name, ''), COALESCE(first_name, '')
        ) = 1
    ),
    latest_units AS (
        SELECT
            u.unit_id,
            u.appfolio_property_id,
            u.name AS unit_name,
            u.current_occupancy_id,
            u.ingested_at
        FROM PROD.STAGING.STG_PMC_INTEGRATIONS_APPFOLIO__GETUNITS u
        JOIN props p ON p.appfolio_property_id = u.appfolio_property_id
        QUALIFY ROW_NUMBER() OVER (
            PARTITION BY u.unit_id
            ORDER BY u.ingested_at DESC
        ) = 1
    ),
    joined AS (
        SELECT
            c.*,
            t.tenant_id,
            t.appfolio_property_id,
            t.unit_id AS tenant_unit_id,
            t.first_name,
            t.last_name,
            t.email,
            t.status AS tenant_status,
            t.tenant_type,
            t.primary_tenant,
            t.lease_start_date,
            t.lease_end_date,
            t.move_in_on,
            t.move_out_on,
            u.unit_id AS appfolio_unit_id,
            u.unit_name,
            CASE WHEN t.occupancy_id IS NOT NULL THEN 'occupancy_id' ELSE 'no_match' END AS match_type
        FROM latest_charges c
        LEFT JOIN tenant_pick t ON t.occupancy_id = c.occupancy_id
        LEFT JOIN latest_units u ON u.unit_id = t.unit_id
    )
    SELECT
        p.parent_company_name AS parent_company,
        p.property_id          AS property_id,
        p.property_name        AS property_name,
        p.property_code        AS property_code,
        p.property_launch_date AS launch_date,
        COALESCE(j.unit_name, j.appfolio_unit_id, j.tenant_unit_id) AS unit_code,
        j.occupancy_id::STRING AS lease_id,
        COALESCE(j.tenant_id::STRING, j.occupancy_id::STRING) AS tenant_code,
        j.first_name           AS first_name,
        j.last_name            AS last_name,
        j.tenant_status        AS tenant_status,
        j.tenant_type          AS tenant_type,
        j.primary_tenant       AS primary_tenant,
        j.lease_start_date     AS lease_from,
        j.lease_end_date       AS lease_to,
        j.move_in_on           AS move_in,
        j.move_out_on          AS move_out,
        j.email                AS email,
        j.description          AS charge_code,
        j.gl_account_id        AS charge_type,
        j.amount               AS charge_amount,
        j.start_date           AS charge_from_date,
        j.end_date             AS charge_to_date,
        COALESCE(NULLIF(j.frequency, ''), 'recurring') AS frequency,
        j.recurring_charge_id  AS recurring_charge_id,
        j.database_id          AS database_id,
        j.occupancy_id         AS occupancy_id,
        COALESCE(j.appfolio_unit_id, j.tenant_unit_id) AS appfolio_unit_id,
        j.match_type           AS _appfolio_match_type
    FROM joined j
    JOIN props p ON p.appfolio_property_id = j.appfolio_property_id
    WHERE j.tenant_id IS NOT NULL
      AND j.email IS NOT NULL
      AND TRIM(j.email) <> ''
      AND COALESCE(j.tenant_status, '') NOT IN ('Future')
    """

    progress_bar.progress(0.30)
    status_text.text("Querying AppFolio Snowflake staging tables...")
    cur.execute(sql)
    rows = cur.fetchall()
    progress_bar.progress(0.85)

    by_prop = {}
    for r in rows:
        pid = r.get("PROPERTY_ID")
        by_prop.setdefault(pid, {
            "property_name": r.get("PROPERTY_NAME"),
            "property_code": r.get("PROPERTY_CODE"),
            "charges": 0,
            "matched": 0,
            "unmatched": 0,
        })
        by_prop[pid]["charges"] += 1
        if r.get("_APPFOLIO_MATCH_TYPE") == "occupancy_id":
            by_prop[pid]["matched"] += 1
        else:
            by_prop[pid]["unmatched"] += 1

    all_charges = [_normalize_appfolio_snowflake_charge_row(r) for r in rows]

    results_log = []
    for prop in properties:
        pid = prop.get("PROPERTY_ID") or prop.get("property_id")
        info = by_prop.get(pid)
        if info is None:
            results_log.append({
                "property": prop.get("PROPERTY_NAME") or prop.get("property_name", ""),
                "code": prop.get("PROPERTY_CODE") or prop.get("property_code", ""),
                "status": "Warning: No AppFolio recurring charges found in Snowflake",
                "charges": 0,
            })
        else:
            status_str = f"Success ({info['charges']} recurring charges; {info['matched']} occupancy matches"
            if info["unmatched"] > 0:
                status_str += f" / {info['unmatched']} unmatched"
            status_str += ")"
            if info["unmatched"] > 0 and info["unmatched"] > info["charges"] * 0.05:
                status_str = status_str.replace("Success", "Warning")
            results_log.append({
                "property": info["property_name"],
                "code": info["property_code"],
                "status": status_str,
                "charges": info["charges"],
            })

    cur.close()
    progress_bar.progress(1.0)
    status_text.text(f"Done — {len(all_charges):,} AppFolio recurring-charge rows.")
    return all_charges, results_log
