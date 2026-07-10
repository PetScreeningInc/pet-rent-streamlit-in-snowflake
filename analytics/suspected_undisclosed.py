"""Suspected Undisclosed Pets: unresolved profile signals, no pet charges."""

from datetime import datetime
import numpy as np
import pandas as pd
import snowflake.connector
import streamlit as st
from analytics.launch_analysis import parse_date
from analytics.missing_pet_rent import _apply_paying_flag, _build_paying_sets, _classify_pet_charge_codes, _dedup_missing_rows, _estimate_property_fees, _filter_realpage_profiles_to_live_residents, _recent_paying_charges, _unit_sources_sql
from components.charts import _resolve_launch_dt
from config import JUNK_EMAILS
from services.snowflake_io import get_snowflake_connection

# ─── Suspected Undisclosed Pets ──────────────────────────────────────

def fetch_suspected_undisclosed_by_property(
    all_charges_df, selected_codes, property_ids, launch_dates, months,
):
    """
    Per-property, per-month estimated uncollected pet rent from *suspected*
    undisclosed pets — residents whose PetScreening profile signals a likely
    pet they haven't disclosed or completed screening for.

    Uses the same revenue estimation methodology as confirmed missing rent:
    property-specific average fee from paying tenants, with portfolio-wide
    fallback.

    Returns
    -------
    dict  – same shape as fetch_missing_pet_rent_by_property:
    {property_name: {
        "missing_count": int,
        "monthly_missing": {month: float},
        "avg_recurring": float,
        "avg_onetime": float,
        "charge_type_label": str,
        "estimated_missing_per_month": float,
        "total_missing_in_window": float,
        "missing_tenants": [list of dicts],  # includes suspected_reason
    }}
    """
    pmc_system = st.session_state.get("pmc_system", "yardi")
    conn = get_snowflake_connection()
    cur = conn.cursor(snowflake.connector.DictCursor)

    # ── Scope to properties that actually have PET-RELATED charge data ──
    _pet_charges_filter = all_charges_df[all_charges_df['charge_code'].isin(selected_codes)]
    _charge_pids = set(_pet_charges_filter['property_id'].astype(str).str.strip().unique())
    property_ids = [pid for pid in property_ids if str(pid).strip() in _charge_pids]
    if not property_ids:
        return {}

    props_str = ", ".join(str(int(pid)) for pid in property_ids)

    # ── Step 1: Reuse the same charge classification & avg fee logic ──
    pet_charges = all_charges_df[all_charges_df['charge_code'].isin(selected_codes)].copy()
    pet_charges['_amt'] = pd.to_numeric(pet_charges['charge_amount'], errors='coerce').fillna(0)
    pet_charges['_from'] = pet_charges['charge_from_date'].apply(parse_date)
    pet_charges['_to'] = pet_charges['charge_to_date'].apply(parse_date)

    # Current-state report → match against recently-active charges only
    # (see _recent_paying_charges).
    paying_tc_set, paying_email_set = _build_paying_sets(
        _recent_paying_charges(all_charges_df), selected_codes
    )

    try:
        _user_overrides = st.session_state.get("charge_type_overrides", {})
    except Exception:
        _user_overrides = {}
    code_class = _classify_pet_charge_codes(
        pet_charges, pmc_system, _user_overrides,
        system_by_pid=st.session_state.get("property_system_by_pid"),
    )

    avg_recurring_by_prop, avg_onetime_by_prop = _estimate_property_fees(
        pet_charges, code_class
    )

    # Tenant info for lease dates
    tenant_info = {}
    for _, row in all_charges_df.iterrows():
        key = (str(row['property_name']), str(row['tenant_code']))
        if key in tenant_info:
            continue
        tenant_info[key] = {
            'move_in': parse_date(row.get('move_in', '')),
            'move_out': parse_date(row.get('move_out', '')),
            'lease_from': parse_date(row.get('lease_from', '')),
            'lease_to': parse_date(row.get('lease_to', '')),
            'status': str(row.get('tenant_status', '')).strip().lower(),
        }

    # Portfolio-wide fallback (median across properties)
    _all_recurring = [v for v in avg_recurring_by_prop.values() if v > 0]
    _all_onetime = [v for v in avg_onetime_by_prop.values() if v > 0]
    portfolio_avg_rec = float(np.median(_all_recurring)) if _all_recurring else 0
    portfolio_avg_ot = float(np.median(_all_onetime)) if _all_onetime else 0

    realpage_current_resident_clause = ""
    if pmc_system in ('real_page', 'all'):
        # RealPage current resident list is the freshness anchor for suspected
        # undisclosed pets too; otherwise old f_leases rows can surface prior
        # residents/leases as current opportunities.
        realpage_current_resident_clause = """
      AND (du.unit_source <> 'real_page' OR EXISTS (
          SELECT 1
          FROM PROD.STAGING.STG_PMC_INTEGRATIONS_REALPAGE__GETRESIDENTLIST r
          WHERE r.pmc_id = PARSE_JSON(p.property_source_id):\"pmcid\"::STRING
            AND r.site_id = PARSE_JSON(p.property_source_id):\"siteid\"::STRING
            AND (
                r.lease_id = COALESCE(
                    l.lease_source_external_id:\"lease_id\"::STRING,
                    l.lease_external_id::STRING
                )
                OR LOWER(TRIM(r.email)) = LOWER(TRIM(ue.user_email))
            )
            AND COALESCE(r.lease_status, '') NOT IN ('Former', 'Former Applicant')
            AND (r.move_out_date IS NULL OR r.move_out_date >= CURRENT_DATE)
      ))
        """

    # ── Step 2: Query Snowflake for suspected undisclosed pets ──
    sql_suspected = f"""
    SELECT DISTINCT
        du.property_id,
        p.property_name,
        COALESCE(
            l.lease_source_external_id:tenant_code::STRING,
            l.lease_source_external_id:"customerId"::STRING,
            l.lease_source_external_id:"customer_id"::STRING,
            l.lease_source_external_id:"lease_id"::STRING
        ) AS tenant_code,
        ue.user_first_name,
        ue.user_last_name,
        ue.user_email,
        ue.user_profile_url,
        ue.user_pet_type,
        ue.user_pet_status,
        ue.compliance_status,
        ue.household_profile_started_alltime,
        ue.assistance_profile_started_alltime,
        CASE
            WHEN ue.user_pet_type = 'household'
                 AND ue.user_pet_status IN ('draft','non_responsive','pending','returned')
            THEN 'Abandoned household profile'
            WHEN ue.user_pet_type = 'assistance'
                 AND ue.user_pet_status IN ('draft','non_responsive','declined','not_recommended','returned')
            THEN 'Unresolved assistance request'
            ELSE 'Other suspected'
        END AS suspected_reason
    FROM PROD.common.d_units du
    JOIN PROD.common.d_properties p
        ON du.property_id = p.property_id
    JOIN PROD.petscreening.petscreening__user_enriched ue
        ON ue.unit_id = du.unit_id
    JOIN PROD.common.f_leases l
        ON du.unit_key = l.unit_key AND l.user_key = ue.user_key
    LEFT JOIN PROD.common.f_user_pets up
        ON up.user_key = ue.user_key
    LEFT JOIN PROD.common.d_pet_profiles pp
        ON pp.pet_key = up.pet_key
    WHERE du.unit_source IN ({_unit_sources_sql(pmc_system)})
      AND du.property_id IN ({props_str})
      AND ue.compliance_status IN ('compliant', 'non_compliant')
      /* Exclude recommended AND expired assistance profiles — both at pet_profile and user_enriched level */
      AND NOT (
          COALESCE(pp.pet_profile_kind, '') = 'assistance'
          AND COALESCE(pp.pet_profile_status, '') IN ('recommended', 'expired')
      )
      AND NOT (
          ue.user_pet_type = 'assistance'
          AND ue.user_pet_status IN ('recommended', 'expired')
      )
      AND (
            /* Current unresolved household profile, not any all-time household history */
            (
              ue.user_pet_type = 'household'
              AND ue.user_pet_status IN ('draft', 'non_responsive', 'pending', 'returned')
            )
            OR
            /* Current unresolved/denied assistance profile. Exclude not_pet rows;
               a resident who attested no pet is too weak for the suspected KPI. */
            (
              ue.user_pet_type = 'assistance'
              AND ue.user_pet_status IN (
                'draft', 'non_responsive', 'declined',
                'not_recommended', 'returned'
              )
            )
          )
      AND COALESCE(pp.pet_profile_archive_reason, '') = ''
      AND ue.user_email IS NOT NULL
      AND TRIM(ue.user_email) <> ''
      AND LOWER(TRIM(ue.user_email)) NOT IN ({JUNK_EMAILS})
      {realpage_current_resident_clause}
    """
    cur.execute(sql_suspected)
    profiles_df = pd.DataFrame(cur.fetchall())
    cur.close()

    if profiles_df.empty:
        return {}

    # ── Step 3: Exclude anyone already paying selected charges ──
    # Uses case-insensitive tc + email fallback; propagates across multi-lease users
    profiles_df = _apply_paying_flag(profiles_df, paying_tc_set, paying_email_set)
    # RealPage: keep only profiles matching the LIVE resident roster
    profiles_df = _filter_realpage_profiles_to_live_residents(profiles_df, pmc_system)

    # ── Step 3b: Entrata freshness filter ──
    if pmc_system in ('entrata', 'all') and 'email' in all_charges_df.columns:
        _pid_systems = st.session_state.get("property_system_by_pid", {}) or {}
        api_emails_by_prop = (
            all_charges_df
            .assign(
                _pid=lambda d: d['property_id'].astype(str).str.strip(),
                _em=lambda d: d['email'].astype(str).str.strip().str.lower(),
            )
            .loc[lambda d: d['_em'].ne('') & d['_em'].ne('nan') & d['_em'].ne('none')]
            .groupby('_pid')['_em']
            .apply(set)
            .to_dict()
        )
        def _in_api_susp(row):
            pid = str(row.get('PROPERTY_ID', '')).strip()
            if pmc_system == 'all' and _pid_systems.get(pid, 'entrata') != 'entrata':
                return True  # Entrata freshness applies only to Entrata properties
            em = str(row.get('USER_EMAIL', '')).strip().lower()
            return em in api_emails_by_prop.get(pid, set())
        profiles_df['_in_api'] = profiles_df.apply(_in_api_susp, axis=1)
        profiles_df = profiles_df[profiles_df['_in_api'] | (profiles_df['pet_rent_paid'] == 1)]
        profiles_df = profiles_df.drop(columns=['_in_api'], errors='ignore')

    profiles_df = profiles_df[profiles_df['pet_rent_paid'] == 0].copy()

    if profiles_df.empty:
        return {}

    # ── Step 4: Per-property, per-month missing revenue ──
    m0, mN = months[0], months[-1]
    result = {}
    for pname, grp in profiles_df.groupby('PROPERTY_NAME'):
        avg_rec = avg_recurring_by_prop.get(pname, portfolio_avg_rec)
        avg_ot = avg_onetime_by_prop.get(pname, portfolio_avg_ot)

        has_recurring = avg_rec > 0
        has_onetime = avg_ot > 0
        _is_portfolio_avg = (pname not in avg_recurring_by_prop and pname not in avg_onetime_by_prop)
        _suffix = " (portfolio avg)" if _is_portfolio_avg else ""
        if has_recurring and has_onetime:
            charge_label = f"rent (${avg_rec:,.0f}/mo) + deposit (${avg_ot:,.0f}){_suffix}"
        elif has_recurring:
            charge_label = f"rent (${avg_rec:,.0f}/mo){_suffix}"
        elif has_onetime:
            charge_label = f"deposit (${avg_ot:,.0f} one-time){_suffix}"
        else:
            charge_label = "no fee data available"

        launch_dt = _resolve_launch_dt(launch_dates.get(pname))
        launch_month = datetime(launch_dt.year, launch_dt.month, 1) if launch_dt else None

        monthly_missing = {m: 0.0 for m in months}
        missing_tenants = []

        # One row per person for the dollar estimation (duplicate lease rows
        # would double-count the estimated fee).
        _grp_dedup = _dedup_missing_rows(grp, pname, tenant_info)
        for _, row in _grp_dedup.iterrows():
            tc = str(row['TENANT_CODE'])
            tinfo = tenant_info.get((pname, tc))

            if tinfo:
                t_start = tinfo['move_in'] or tinfo['lease_from']
                t_end = tinfo['move_out'] or tinfo['lease_to']
                t_status = tinfo['status']
            else:
                t_start = None
                t_end = None
                t_status = 'current'

            if t_start is None:
                t_start = launch_dt if launch_dt else m0
            active_from = datetime(t_start.year, t_start.month, 1)

            if t_end is None or t_status == 'current':
                active_to = mN
            else:
                active_to = datetime(t_end.year, t_end.month, 1)

            if launch_month:
                active_from = max(active_from, launch_month)
            active_from = max(active_from, m0)
            active_to = min(active_to, mN)

            first_month_done = False
            for m in months:
                if active_from <= m <= active_to:
                    if has_recurring:
                        monthly_missing[m] += avg_rec
                    if has_onetime and not first_month_done:
                        monthly_missing[m] += avg_ot
                        first_month_done = True

            # Build unit label: prefer unit_address_1, fall back to full_unit_address
            _unit_label_s = str(row.get('UNIT_ADDRESS_2', '') or row.get('UNIT_ADDRESS_1', '') or row.get('FULL_UNIT_ADDRESS', '') or '').strip()

            missing_tenants.append({
                "name": f"{row.get('USER_FIRST_NAME', '')} {row.get('USER_LAST_NAME', '')}".strip(),
                "email": row.get('USER_EMAIL', ''),
                "profile_url": row.get('USER_PROFILE_URL', ''),
                "unit": _unit_label_s,
                "suspected_reason": row.get('SUSPECTED_REASON', 'Unknown'),
                "user_pet_type": row.get('USER_PET_TYPE', ''),
                "user_pet_status": row.get('USER_PET_STATUS', ''),
                "active_from": active_from.strftime('%b %Y'),
                "active_to": active_to.strftime('%b %Y') if active_to < mN else "Current",
            })

        total_missing_in_window = sum(monthly_missing.values())
        months_with_missing = sum(1 for v in monthly_missing.values() if v > 0)
        est_per_month = total_missing_in_window / months_with_missing if months_with_missing > 0 else 0

        # Count unique tenants by email (not raw rows which may have duplicates)
        _unique_suspected = grp['USER_EMAIL'].nunique() if 'USER_EMAIL' in grp.columns else len(grp)

        result[pname] = {
            "missing_count": _unique_suspected,
            "monthly_missing": monthly_missing,
            "avg_recurring": avg_rec,
            "avg_onetime": avg_ot,
            "charge_type_label": charge_label,
            "estimated_missing_per_month": est_per_month,
            "total_missing_in_window": total_missing_in_window,
            "missing_tenants": missing_tenants,
        }
    return result


def generate_suspected_undisclosed_report(all_charges_df, selected_codes, property_ids):
    """
    Generate a downloadable report of suspected undisclosed pets.
    Similar to generate_missing_pet_rent_report but uses the suspected
    undisclosed query criteria and adds suspected_reason column.
    """
    pmc_system = st.session_state.get("pmc_system", "yardi")
    conn = get_snowflake_connection()
    cur = conn.cursor(snowflake.connector.DictCursor)
    props_str = ", ".join(str(int(pid)) for pid in property_ids)

    # Step 1: From live API data, build paying tenant set (normalized).
    # Current-state report → only recently-active charges count as "paying".
    paying_tc_set, paying_email_set = _build_paying_sets(
        _recent_paying_charges(all_charges_df), selected_codes
    )

    realpage_current_resident_clause = ""
    if pmc_system in ('real_page', 'all'):
        realpage_current_resident_clause = """
      AND (du.unit_source <> 'real_page' OR EXISTS (
          SELECT 1
          FROM PROD.STAGING.STG_PMC_INTEGRATIONS_REALPAGE__GETRESIDENTLIST r
          WHERE r.pmc_id = PARSE_JSON(p.property_source_id):\"pmcid\"::STRING
            AND r.site_id = PARSE_JSON(p.property_source_id):\"siteid\"::STRING
            AND (
                r.lease_id = COALESCE(
                    l.lease_source_external_id:\"lease_id\"::STRING,
                    l.lease_external_id::STRING
                )
                OR LOWER(TRIM(r.email)) = LOWER(TRIM(ue.user_email))
            )
            AND COALESCE(r.lease_status, '') NOT IN ('Former', 'Former Applicant')
            AND (r.move_out_date IS NULL OR r.move_out_date >= CURRENT_DATE)
      ))
        """

    # Step 2: Query Snowflake for suspected undisclosed profiles
    sql = f"""
    SELECT DISTINCT
        du.property_id,
        p.property_name,
        COALESCE(
            l.lease_source_external_id:tenant_code::STRING,
            l.lease_source_external_id:"customerId"::STRING,
            l.lease_source_external_id:"customer_id"::STRING,
            l.lease_source_external_id:"lease_id"::STRING
        ) AS tenant_code,
        ue.user_first_name,
        ue.user_last_name,
        ue.user_email,
        ue.user_profile_url,
        ue.user_pet_type,
        ue.user_pet_status,
        ue.compliance_status,
        ue.household_profile_started_alltime,
        ue.assistance_profile_started_alltime,
        du.unit_address_1,
        du.unit_address_2,
        CONCAT_WS(' ', du.unit_address_1, du.unit_address_2) AS full_unit_address,
        l.lease_start_date,
        l.lease_end_date,
        CASE
            WHEN ue.user_pet_type = 'household'
                 AND ue.user_pet_status IN ('draft','non_responsive','pending','returned')
            THEN 'Abandoned household profile'
            WHEN ue.user_pet_type = 'assistance'
                 AND ue.user_pet_status IN ('draft','non_responsive','declined','not_recommended','returned')
            THEN 'Unresolved assistance request'
            ELSE 'Other suspected'
        END AS suspected_reason
    FROM PROD.common.d_units du
    JOIN PROD.common.d_properties p
        ON du.property_id = p.property_id
    JOIN PROD.petscreening.petscreening__user_enriched ue
        ON ue.unit_id = du.unit_id
    JOIN PROD.common.f_leases l
        ON du.unit_key = l.unit_key AND l.user_key = ue.user_key
    LEFT JOIN PROD.common.f_user_pets up
        ON up.user_key = ue.user_key
    LEFT JOIN PROD.common.d_pet_profiles pp
        ON pp.pet_key = up.pet_key
    WHERE du.unit_source IN ({_unit_sources_sql(pmc_system)})
      AND du.property_id IN ({props_str})
      AND ue.compliance_status IN ('compliant', 'non_compliant')
      /* Exclude recommended AND expired assistance profiles — both at pet_profile and user_enriched level */
      AND NOT (
          COALESCE(pp.pet_profile_kind, '') = 'assistance'
          AND COALESCE(pp.pet_profile_status, '') IN ('recommended', 'expired')
      )
      AND NOT (
          ue.user_pet_type = 'assistance'
          AND ue.user_pet_status IN ('recommended', 'expired')
      )
      AND (
            /* Current unresolved household profile, not any all-time household history */
            (
              ue.user_pet_type = 'household'
              AND ue.user_pet_status IN ('draft', 'non_responsive', 'pending', 'returned')
            )
            OR
            /* Current unresolved/denied assistance profile. Exclude not_pet rows;
               a resident who attested no pet is too weak for the suspected export. */
            (
              ue.user_pet_type = 'assistance'
              AND ue.user_pet_status IN (
                'draft', 'non_responsive', 'declined',
                'not_recommended', 'returned'
              )
            )
          )
      AND COALESCE(pp.pet_profile_archive_reason, '') = ''
      AND ue.user_email IS NOT NULL
      AND TRIM(ue.user_email) <> ''
      AND LOWER(TRIM(ue.user_email)) NOT IN ({JUNK_EMAILS})
      {realpage_current_resident_clause}
    """
    cur.execute(sql)
    profiles_df = pd.DataFrame(cur.fetchall())
    cur.close()

    if profiles_df.empty:
        return pd.DataFrame()

    # Step 3: Exclude anyone already paying (case-insensitive tc + email fallback)
    # Propagates across multi-lease users — if any lease row is paying, all are
    profiles_df = _apply_paying_flag(profiles_df, paying_tc_set, paying_email_set)
    # RealPage: keep only profiles matching the LIVE resident roster
    profiles_df = _filter_realpage_profiles_to_live_residents(profiles_df, pmc_system)

    # Step 3b: Entrata freshness filter
    if pmc_system in ('entrata', 'all') and 'email' in all_charges_df.columns:
        _pid_systems = st.session_state.get("property_system_by_pid", {}) or {}
        api_emails_by_prop = (
            all_charges_df
            .assign(
                _pid=lambda d: d['property_id'].astype(str).str.strip(),
                _em=lambda d: d['email'].astype(str).str.strip().str.lower(),
            )
            .loc[lambda d: d['_em'].ne('') & d['_em'].ne('nan') & d['_em'].ne('none')]
            .groupby('_pid')['_em']
            .apply(set)
            .to_dict()
        )
        def _in_api_susp_rpt(row):
            pid = str(row.get('PROPERTY_ID', '')).strip()
            if pmc_system == 'all' and _pid_systems.get(pid, 'entrata') != 'entrata':
                return True  # Entrata freshness applies only to Entrata properties
            em = str(row.get('USER_EMAIL', '')).strip().lower()
            return em in api_emails_by_prop.get(pid, set())
        profiles_df['_in_api'] = profiles_df.apply(_in_api_susp_rpt, axis=1)
        profiles_df = profiles_df[profiles_df['_in_api'] | (profiles_df['pet_rent_paid'] == 1)]
        profiles_df = profiles_df.drop(columns=['_in_api'], errors='ignore')

    report_df = profiles_df[profiles_df['pet_rent_paid'] == 0].copy()

    if report_df.empty:
        return pd.DataFrame()

    report_df['overall_status'] = 'Suspected_Undisclosed'
    return report_df
