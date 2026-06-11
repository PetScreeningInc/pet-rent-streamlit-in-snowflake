"""RealPage Snowflake queries and fetch functions."""

import streamlit as st
from services.snowflake_io import run_query
from config import JUNK_EMAILS


# ─── Snowflake queries ────────────────────────────────────────────────

@st.cache_data(ttl=300)
def load_realpage_parent_companies():
    """Count RealPage properties per parent company."""
    return run_query("""
        SELECT
            p.parent_company_name,
            MAX(p.parent_company_ancestry_id)    AS ancestry_id,
            MAX(p.parent_company_ancestry_name)   AS ancestry_name,
            COUNT(DISTINCT p.property_id)         AS total_props,
            COUNT(DISTINCT CASE
                WHEN p.property_status = 'active'
                     AND p.integration_status = 'enabled'
                     AND PARSE_JSON(p.property_source_id):"pmcid"::STRING IS NOT NULL
                     AND PARSE_JSON(p.property_source_id):"siteid"::STRING IS NOT NULL
                THEN p.property_id
            END) AS api_props
        FROM PROD.COMMON.D_PROPERTIES p
        WHERE p.parent_company_name IS NOT NULL
          AND p.property_source_name = 'real_page'
        GROUP BY 1
        HAVING api_props > 0
        ORDER BY 1
    """)


@st.cache_data(ttl=300)
def load_realpage_all_properties():
    return run_query("""
        SELECT DISTINCT
            p.property_id,
            p.property_name,
            p.parent_company_name,
            p.parent_company_ancestry_id
        FROM PROD.COMMON.D_PROPERTIES p
        WHERE p.property_source_name = 'real_page'
        ORDER BY p.property_name
    """)


def load_realpage_properties_for_selection(parent_company_name=None, property_id=None, ancestry_id=None):
    """Load RealPage properties matching the selection criteria."""
    where_clause = """
        WHERE p.property_source_name = 'real_page'
          AND p.property_status = 'active'
          AND p.integration_status = 'enabled'
          AND PARSE_JSON(p.property_source_id):"pmcid"::STRING IS NOT NULL
          AND PARSE_JSON(p.property_source_id):"siteid"::STRING IS NOT NULL
    """
    if parent_company_name:
        _safe = parent_company_name.replace("'", "''")
        where_clause += f" AND p.parent_company_name = '{_safe}'"
    if property_id:
        where_clause += f" AND p.property_id = {int(property_id)}"
    if ancestry_id:
        _safe = str(ancestry_id).replace("'", "''")
        where_clause += f" AND p.parent_company_ancestry_id = '{_safe}'"

    return run_query(f"""
        SELECT DISTINCT
            PARSE_JSON(p.property_source_id):"pmcid"::STRING  AS pmc_id,
            PARSE_JSON(p.property_source_id):"siteid"::STRING AS site_id,
            p.property_id,
            p.property_name,
            p.parent_company_name,
            PARSE_JSON(p.property_source_id):"siteid"::STRING AS property_code,
            kf.property_launch_date AS property_launch_date
        FROM PROD.COMMON.D_PROPERTIES p
        LEFT JOIN PROD.PETSCREENING.PETSCREENING__PROPERTY_KEY_FACTS kf
            ON p.property_id = kf.property_id
        {where_clause}
        ORDER BY p.property_name
    """)


# ─── RealPage charge fetch (Snowflake-backed) ─────────────────────────

def fetch_realpage_for_properties(properties, progress_bar, status_text, lookback_months=24):
    """Read RealPage scheduled charges + residents from Snowflake staging,
    join in SQL, and emit charge rows in the same shape as Yardi/Entrata.

    Returns (all_charges, results_log).
    """
    if not properties:
        return [], []

    progress_bar.progress(0.05)
    status_text.text(f"Reading RealPage data from Snowflake for {len(properties)} properties...")

    def _esc(s):
        return str(s if s is not None else "").replace("'", "''")

    select_rows = []
    for p in properties:
        pmc = _esc(p.get("PMC_ID") or p.get("pmc_id") or "")
        site = _esc(p.get("SITE_ID") or p.get("site_id") or "")
        pid = int(p.get("PROPERTY_ID") or p.get("property_id") or 0)
        pname = _esc(p.get("PROPERTY_NAME") or p.get("property_name") or "")
        pcode = _esc(p.get("PROPERTY_CODE") or p.get("property_code") or site)
        parent = _esc(p.get("PARENT_COMPANY_NAME") or p.get("parent_company_name") or "")
        launch = p.get("PROPERTY_LAUNCH_DATE") or p.get("property_launch_date")
        if launch:
            try:
                launch_str = launch.strftime("%Y-%m-%d") if hasattr(launch, "strftime") else str(launch)[:10]
            except Exception:
                launch_str = ""
        else:
            launch_str = ""
        launch_sql = "NULL" if not launch_str else f"TO_DATE('{launch_str}')"
        select_rows.append(
            f"SELECT '{pmc}'::STRING AS pmc_id, '{site}'::STRING AS site_id, "
            f"{pid}::NUMBER AS property_id, '{pname}'::STRING AS property_name, "
            f"'{pcode}'::STRING AS property_code, '{parent}'::STRING AS parent_company_name, "
            f"{launch_sql} AS property_launch_date"
        )
    values_sql = "\n        UNION ALL ".join(select_rows)

    junk_email_clause = f"AND LOWER(TRIM(j.email)) NOT IN ({JUNK_EMAILS})"

    sql = f"""
    WITH props AS (
        {values_sql}
    ),
    latest_charges AS (
        SELECT
            c.pmc_id, c.site_id, c.resh_id, c.lease_id, c.unit_id,
            c.transaction_name, c.description, c.amount,
            c.bill_start, c.bill_end, c.category_code, c.sub_journal,
            TRY_TO_DATE(c.post_month) AS post_month,
            c.ingested_at
        FROM PROD.STAGING.STG_PMC_INTEGRATIONS_REALPAGE__GETSCHEDULEDCHARGES c
        JOIN props p ON c.pmc_id = p.pmc_id AND c.site_id = p.site_id
        WHERE c.category_code = 'C'
          AND TRY_TO_DATE(c.post_month) IS NOT NULL
        QUALIFY ROW_NUMBER() OVER (
            PARTITION BY c.pmc_id, c.site_id, c.unit_id, c.lease_id,
                         c.resh_id, c.transaction_name, TRY_TO_DATE(c.post_month)
            ORDER BY c.ingested_at DESC
        ) = 1
    ),
    all_residents AS (
        SELECT
            r.pmc_id, r.site_id, r.resident_member_id, r.lease_id, r.unit_id,
            r.first_name, r.last_name, r.email, r.lease_status,
            r.move_out_date, r.unit_number,
            r.hoh_bit, r.signer_bit, r.active_bit, r.contact_status,
            r.ingested_at
        FROM PROD.STAGING.STG_PMC_INTEGRATIONS_REALPAGE__GETRESIDENTLIST r
        JOIN props p ON r.pmc_id = p.pmc_id AND r.site_id = p.site_id
        QUALIFY ROW_NUMBER() OVER (
            PARTITION BY r.pmc_id, r.site_id, r.lease_id, r.resident_member_id
            ORDER BY r.ingested_at DESC
        ) = 1
    ),
    lease_resident_pick AS (
        SELECT
            r.pmc_id, r.site_id, r.lease_id,
            r.first_name, r.last_name, r.email,
            r.lease_status, r.move_out_date, r.unit_number, r.hoh_bit
        FROM all_residents r
        QUALIFY ROW_NUMBER() OVER (
            PARTITION BY r.pmc_id, r.site_id, r.lease_id
            ORDER BY
                CASE WHEN r.hoh_bit = 'TRUE' THEN 0 ELSE 1 END,
                CASE WHEN r.signer_bit = 'TRUE' THEN 0 ELSE 1 END,
                CASE WHEN r.active_bit = 'TRUE' THEN 0 ELSE 1 END,
                CASE WHEN r.contact_status = 'Lease signer' THEN 0 ELSE 1 END,
                COALESCE(r.last_name, ''),
                COALESCE(r.first_name, '')
        ) = 1
    ),
    direct_resident AS (
        SELECT
            r.pmc_id, r.site_id, r.lease_id, r.resident_member_id,
            r.first_name, r.last_name, r.email,
            r.lease_status, r.move_out_date, r.unit_number
        FROM all_residents r
    ),
    joined AS (
        SELECT
            c.*,
            COALESCE(dr.first_name,    lp.first_name)    AS resolved_first_name,
            COALESCE(dr.last_name,     lp.last_name)     AS resolved_last_name,
            COALESCE(dr.email,         lp.email)         AS resolved_email,
            COALESCE(dr.lease_status,  lp.lease_status)  AS resolved_lease_status,
            COALESCE(dr.move_out_date, lp.move_out_date) AS resolved_move_out_date,
            COALESCE(dr.unit_number,   lp.unit_number)   AS resolved_unit_number,
            CASE
                WHEN dr.email IS NOT NULL THEN 'resh_id'
                WHEN lp.email IS NOT NULL THEN 'lease_id'
                ELSE 'no_match'
            END AS match_type
        FROM latest_charges c
        LEFT JOIN direct_resident dr
            ON dr.pmc_id = c.pmc_id
           AND dr.site_id = c.site_id
           AND dr.lease_id = c.lease_id
           AND dr.resident_member_id = c.resh_id
        LEFT JOIN lease_resident_pick lp
            ON lp.pmc_id = c.pmc_id
           AND lp.site_id = c.site_id
           AND lp.lease_id = c.lease_id
    )
    SELECT
        p.parent_company_name AS parent_company,
        p.property_id          AS property_id,
        p.property_name        AS property_name,
        p.property_code        AS property_code,
        p.property_launch_date AS launch_date,
        COALESCE(j.resolved_unit_number, j.unit_id) AS unit_code,
        ''                     AS unit_type,
        ''                     AS market_rent,
        j.lease_id::STRING     AS lease_id,
        j.lease_id::STRING     AS tenant_code,
        j.resolved_first_name  AS first_name,
        j.resolved_last_name   AS last_name,
        CASE j.resolved_lease_status
            WHEN 'Current'                  THEN 'Current'
            WHEN 'Former'                   THEN 'Past'
            WHEN 'Future Lease'             THEN 'Future'
            WHEN 'Applicant'                THEN 'Applicant'
            WHEN 'Applicant - Lease Signed' THEN 'Applicant'
            WHEN 'Former Applicant'         THEN 'Past'
            WHEN 'Pending'                  THEN 'Future'
            ELSE COALESCE(j.resolved_lease_status, '')
        END AS tenant_status,
        ''                     AS lease_from,
        ''                     AS lease_to,
        ''                     AS move_in,
        TO_CHAR(j.resolved_move_out_date, 'YYYY-MM-DD') AS move_out,
        j.resolved_email       AS email,
        j.transaction_name     AS charge_code,
        j.description          AS charge_type,
        j.amount::STRING       AS charge_amount,
        TO_CHAR(j.post_month, 'YYYY-MM-DD') AS charge_from_date,
        TO_CHAR(
            LEAST(
                LAST_DAY(j.post_month),
                COALESCE(j.resolved_move_out_date::DATE, LAST_DAY(j.post_month)),
                CURRENT_DATE
            ),
            'YYYY-MM-DD'
        )                       AS charge_to_date,
        j.match_type           AS _realpage_match_type
    FROM joined j
    JOIN props p ON p.pmc_id = j.pmc_id AND p.site_id = j.site_id
    WHERE j.resolved_email IS NOT NULL
      AND TRIM(j.resolved_email) <> ''
      AND (j.resolved_lease_status IS NULL
           OR j.resolved_lease_status NOT IN (
               'Future Lease', 'Applicant',
               'Applicant - Lease Signed', 'Pending'
           ))
      AND (j.resolved_move_out_date IS NULL
           OR j.bill_start::DATE <= j.resolved_move_out_date)
      {junk_email_clause.replace("j.email", "j.resolved_email")}
    """

    progress_bar.progress(0.30)
    status_text.text("Querying Snowflake staging tables...")
    rows = run_query(sql)
    progress_bar.progress(0.85)

    test_words = {'model', 'test', 'sample', 'unknown', 'noname', 'tba', 'vacant', '*', ''}

    def _is_test(fn, ln):
        f = (fn or "").lower().strip()
        l = (ln or "").lower().strip()
        if f in test_words or l in test_words:
            return True
        if 'model' in f or 'model' in l:
            return True
        return False

    by_prop = {}
    test_filtered = 0
    for r in rows:
        fn = r.get("FIRST_NAME", "")
        ln = r.get("LAST_NAME", "")
        if _is_test(fn, ln):
            test_filtered += 1
            continue
        pid = r.get("PROPERTY_ID")
        by_prop.setdefault(pid, {
            "property_name": r.get("PROPERTY_NAME"),
            "property_code": r.get("PROPERTY_CODE"),
            "charges": 0,
            "resh_matches": 0,
            "lease_matches": 0,
            "no_matches": 0,
        })
        by_prop[pid]["charges"] += 1
        mt = r.get("_REALPAGE_MATCH_TYPE", "no_match")
        if mt == "resh_id":
            by_prop[pid]["resh_matches"] += 1
        elif mt == "lease_id":
            by_prop[pid]["lease_matches"] += 1
        else:
            by_prop[pid]["no_matches"] += 1

    all_charges = []
    for r in rows:
        if _is_test(r.get("FIRST_NAME", ""), r.get("LAST_NAME", "")):
            continue
        all_charges.append({
            "parent_company": r.get("PARENT_COMPANY"),
            "property_id":    r.get("PROPERTY_ID"),
            "property_name":  r.get("PROPERTY_NAME"),
            "property_code":  r.get("PROPERTY_CODE"),
            "launch_date":    r.get("LAUNCH_DATE"),
            "unit_code":      r.get("UNIT_CODE", ""),
            "unit_type":      r.get("UNIT_TYPE", ""),
            "market_rent":    r.get("MARKET_RENT", ""),
            "lease_id":       r.get("LEASE_ID", ""),
            "tenant_code":    r.get("TENANT_CODE", ""),
            "first_name":     r.get("FIRST_NAME", ""),
            "last_name":      r.get("LAST_NAME", ""),
            "tenant_status":  r.get("TENANT_STATUS", ""),
            "lease_from":     r.get("LEASE_FROM", ""),
            "lease_to":       r.get("LEASE_TO", ""),
            "move_in":        r.get("MOVE_IN", ""),
            "move_out":       r.get("MOVE_OUT", ""),
            "email":          r.get("EMAIL", ""),
            "charge_code":    r.get("CHARGE_CODE", ""),
            "charge_type":    r.get("CHARGE_TYPE", ""),
            "charge_amount":  r.get("CHARGE_AMOUNT", ""),
            "charge_from_date": r.get("CHARGE_FROM_DATE", ""),
            "charge_to_date":   r.get("CHARGE_TO_DATE", ""),
        })

    results_log = []
    for prop in properties:
        pid = prop.get("PROPERTY_ID") or prop.get("property_id")
        info = by_prop.get(pid)
        if info is None:
            results_log.append({
                "property": prop.get("PROPERTY_NAME") or prop.get("property_name", ""),
                "code": prop.get("PROPERTY_CODE") or prop.get("property_code", ""),
                "status": "Warning: No charges found in Snowflake",
                "charges": 0,
            })
        else:
            status_str = f"Success ({info['charges']} charges; "
            status_str += f"{info['resh_matches']} resh-match / {info['lease_matches']} lease-match"
            if info['no_matches'] > 0:
                status_str += f" / {info['no_matches']} unmatched"
            status_str += ")"
            if info['no_matches'] > 0 and info['no_matches'] > info['charges'] * 0.05:
                status_str = status_str.replace("Success", "Warning")
            results_log.append({
                "property": info["property_name"],
                "code":     info["property_code"],
                "status":   status_str,
                "charges":  info["charges"],
            })

    progress_bar.progress(1.0)
    if test_filtered > 0:
        status_text.text(f"Done — {len(all_charges):,} charge rows ({test_filtered} test/model residents filtered).")
    else:
        status_text.text(f"Done — {len(all_charges):,} charge rows.")

    return all_charges, results_log
