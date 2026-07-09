"""RealPage/OneSite: Snowflake queries + staging/live-hybrid charge fetch."""

from collections import defaultdict
import snowflake.connector
import streamlit as st
from config import JUNK_EMAILS
from services.snowflake_io import get_snowflake_connection

# ─── RealPage / OneSite (Snowflake-backed) ───────────────────────────
# Read from staging tables populated daily by EKS jobs. No live SOAP.
# pmc_system value: 'real_page' (with underscore)
# tenant_code = lease_id (from f_leases.lease_source_external_id:lease_id)

@st.cache_data(ttl=300)
def load_realpage_parent_companies():
    """Count RealPage properties per parent company.

    NOTE: RealPage uses global credentials — there's no integration_id check.
    A property is "API-accessible" iff it's enabled + active in d_properties
    AND its property_source_id has both pmcid and siteid populated.
    """
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
    return cur.fetchall()


@st.cache_data(ttl=300)
def load_realpage_all_properties():
    conn = get_snowflake_connection()
    cur = conn.cursor(snowflake.connector.DictCursor)
    cur.execute("""
        SELECT DISTINCT
            p.property_id,
            p.property_name,
            p.parent_company_name,
            p.parent_company_ancestry_id
        FROM PROD.COMMON.D_PROPERTIES p
        WHERE p.property_source_name = 'real_page'
        ORDER BY p.property_name
    """)
    return cur.fetchall()


def load_realpage_properties_for_selection(parent_company_name=None, property_id=None, ancestry_id=None):
    """Load RealPage properties matching the selection criteria.

    Returns rows with PMC_ID, SITE_ID, PROPERTY_ID, PROPERTY_NAME, etc.
    No integration credential lookup — RealPage uses global creds.
    """
    conn = get_snowflake_connection()
    cur = conn.cursor(snowflake.connector.DictCursor)

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

    cur.execute(f"""
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
    return cur.fetchall()


# ─── RealPage charge fetch (Snowflake-backed, not live SOAP) ─────────
def fetch_realpage_for_properties(properties, progress_bar, status_text, lookback_months=24):
    """Read RealPage scheduled charges + residents from Snowflake staging,
    join in SQL, and emit charge rows in the same shape as Yardi/Entrata.

    Returns (all_charges, results_log) — same tuple shape as
    fetch_rentroll_for_properties (Yardi) for downstream parity.

    Single SQL roundtrip. The join logic mirrors the canonical pattern from
    the standardized pet-signals SQL (see realpage-query-analysis.md):
    direct match on (pmc_id, site_id, lease_id, resident_member_id), with
    HOH-priority fallback to any resident on the same lease when direct match
    misses. Lease_id is required because RealPage resident_member_id values are
    not unique enough by themselves within a site.

    The lookback_months argument is kept for interface compat but does not
    constrain the data — BillStart/BillEnd carry the full charge lifespan
    inside each row, and downstream display logic clips to the user's window.
    """
    if not properties:
        return [], []

    progress_bar.progress(0.05)
    status_text.text(f"Reading RealPage data from Snowflake for {len(properties)} properties...")

    conn = get_snowflake_connection()
    cur = conn.cursor(snowflake.connector.DictCursor)

    # Build a VALUES list for the property scope. We carry pmc_id, site_id,
    # property_id (PetScreening), property_name, property_code, parent_company,
    # and launch_date so the final SELECT can stamp every charge row.
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

    # Junk emails to filter out — must match the JUNK_EMAILS constant in app.py.
    # Reusing it directly via Python f-string isn't ideal because this lives
    # inside app.py once spliced; we reference JUNK_EMAILS at call time.
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
        WHERE c.category_code = 'C'  -- skip P=Payment/Reimbursement
          AND TRY_TO_DATE(c.post_month) IS NOT NULL
        QUALIFY ROW_NUMBER() OVER (
            -- RealPage scheduled charges are snapshots by requested PostMonth.
            -- BillStart can be years earlier, but the API/raw table only proves
            -- the charge existed for the post_month snapshot. Keep one row per
            -- observed month instead of deduping to latest and back-projecting
            -- BillStart history before the first available post_month.
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
    -- Pick the representative resident per (pmc, site, lease) using
    -- HOH > signer > active > contact_status priority. This handles
    -- the common case where ReshID on the charge does NOT directly match
    -- a residentmemberid (in our spot-check, 0 of 169 (resh_id, lease_id)
    -- pairs matched directly while 212 matched via lease_id only).
    -- Lease-id is the canonical join key — same as the standardized
    -- pet-signals SQL across PMCs.
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
    -- Also pick a direct resident match by (pmc, site, lease_id, resh_id) when it
    -- exists.  RealPage resident_member_id/ReshID values are not globally unique
    -- within a site; joining only on resident_member_id can attribute a pet
    -- charge to an unrelated historical resident and prevent the actual lease
    -- from being recognized as paying.  If the lease does not line up, the join
    -- intentionally misses and the lease-level fallback below is used.
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
        -- RealPage scheduled-charge rows are monthly snapshots. Do NOT use
        -- BillStart/BillEnd as the app-visible date range; BillStart often
        -- predates our first available PostMonth by years and would fabricate
        -- pre-2025 history. Count each observed PostMonth as one monthly charge.
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
      -- Drop charges for residents who are Future/Applicant/Pending —
      -- they haven't started paying yet and would inflate revenue.
      -- Past tenants ARE kept (with clipped charge_to_date) so historical
      -- revenue is preserved. Current/Notice/Former all flow through.
      AND (j.resolved_lease_status IS NULL
           OR j.resolved_lease_status NOT IN (
               'Future Lease', 'Applicant',
               'Applicant - Lease Signed', 'Pending'
           ))
      -- Drop charges where the entire bill window is after move-out
      -- (defensive — the LEAST() above would clip charge_to to before
      -- charge_from in this case, producing zero spread).
      AND (j.resolved_move_out_date IS NULL
           OR j.bill_start::DATE <= j.resolved_move_out_date)
      {junk_email_clause.replace("j.email", "j.resolved_email")}
    """

    progress_bar.progress(0.30)
    status_text.text("Querying Snowflake staging tables...")
    cur.execute(sql)
    rows = cur.fetchall()
    progress_bar.progress(0.85)

    # Filter out test/model residents (data hygiene — model units are common in OneSite)
    test_words = {'model', 'test', 'sample', 'unknown', 'noname', 'tba', 'vacant', '*', ''}
    def _is_test(fn, ln):
        f = (fn or "").lower().strip()
        l = (ln or "").lower().strip()
        if f in test_words or l in test_words:
            return True
        if 'model' in f or 'model' in l:
            return True
        return False

    # Build per-property fetch summary
    by_prop = {}
    test_filtered = 0
    for r in rows:
        # DictCursor returns uppercase keys in this codebase
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

    # Strip the internal _REALPAGE_MATCH_TYPE field from the returned rows
    # (it's diagnostic, not part of the unified schema)
    all_charges = []
    for r in rows:
        if _is_test(r.get("FIRST_NAME", ""), r.get("LAST_NAME", "")):
            continue
        # Lowercase keys for parity with extract_charges_from_property /
        # _extract_charges_from_entrata_lease (which return lowercase dicts)
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

    # Build results_log — one entry per property (matches Yardi/Entrata shape)
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
            # Surface a clear warning when the no_match count is non-trivial.
            if info['no_matches'] > 0 and info['no_matches'] > info['charges'] * 0.05:
                status_str = status_str.replace("Success", "Warning")
            results_log.append({
                "property": info["property_name"],
                "code":     info["property_code"],
                "status":   status_str,
                "charges":  info["charges"],
            })

    cur.close()
    progress_bar.progress(1.0)
    if test_filtered > 0:
        status_text.text(f"Done — {len(all_charges):,} charge rows ({test_filtered} test/model residents filtered).")
    else:
        status_text.text(f"Done — {len(all_charges):,} charge rows.")

    return all_charges, results_log


def fetch_realpage_hybrid(properties, progress_bar, status_text, lookback_months=24):
    """Best-of-both RealPage fetch: staging history + live current snapshot.

    Evidence (docs/realpage-accuracy-audit.md, 2026-07-05):
      - The live OneSite API returns only the CURRENT scheduled-charge
        snapshot: the `postmonth` parameter is ignored (identical data for
        any requested month) and Former-resident charges are unreachable
        (residentstatus=F returns zero rows). Its "history" is synthesized
        by expanding each charge across BillStart→BillEnd, which misses any
        charge that ended before today.
      - Snowflake staging accumulates real monthly snapshots (including
        residents who have since moved out) but can lag weeks behind — the
        audit found a property whose ingestion stopped 5 weeks earlier —
        and some properties have no usable staging rows at all.

    Stitching rule, per property and month: an OBSERVED staging snapshot
    wins; live rows fill months staging never observed (typically the most
    recent weeks, future scheduled charges, and whole properties staging
    misses). Live rows for months staging covers are dropped — they are
    synthetic back-projection, not observations.
    """
    stg_charges, stg_log = fetch_realpage_for_properties(
        properties, progress_bar, status_text, lookback_months
    )

    from realpage_live_api import fetch_realpage_live
    live_charges, live_log = fetch_realpage_live(
        properties, progress_bar, status_text, lookback_months,
        junk_emails={e.strip().strip("'").lower() for e in JUNK_EMAILS.split(",")},
    )

    # Months observed by staging, per property
    stg_months = defaultdict(set)
    for r in stg_charges:
        m = str(r.get("charge_from_date") or "")[:7]
        if m:
            stg_months[r.get("property_id")].add(m)

    combined = list(stg_charges)
    live_fill = defaultdict(int)
    for r in live_charges:
        m = str(r.get("charge_from_date") or "")[:7]
        if m and m in stg_months.get(r.get("property_id"), set()):
            continue
        combined.append(r)
        live_fill[r.get("property_id")] += 1

    # Merge the two logs into one entry per property
    _stg_by_name = {row.get("property"): row for row in stg_log}
    merged_log = []
    for row in live_log:
        name = row.get("property")
        stg_row = _stg_by_name.pop(name, None)
        stg_n = stg_row.get("charges", 0) if stg_row else 0
        pid = next(
            (p.get("PROPERTY_ID") or p.get("property_id") for p in properties
             if (p.get("PROPERTY_NAME") or p.get("property_name")) == name),
            None,
        )
        fill_n = live_fill.get(pid, 0)
        total = stg_n + fill_n
        if total > 0:
            status = (
                f"Success ({total} charges: {stg_n} staging history "
                f"+ {fill_n} live fill)"
            )
            if stg_row and str(stg_row.get("status", "")).startswith("Warning") and fill_n == 0:
                status = status.replace("Success", "Warning")
        else:
            stg_status = stg_row.get("status", "") if stg_row else "no staging rows"
            status = f"Warning: no usable rows (staging: {stg_status}; live: {row.get('status', '')})"[:400]
        merged_log.append({
            "property": name,
            "code": row.get("code", ""),
            "status": status,
            "charges": total,
        })
    # Properties present only in the staging log
    merged_log.extend(_stg_by_name.values())

    try:
        status_text.text(
            f"RealPage hybrid done — {len(combined):,} charge rows "
            f"({len(stg_charges):,} staging + {len(combined) - len(stg_charges):,} live fill)."
        )
    except Exception:  # noqa: BLE001
        pass

    return combined, merged_log
