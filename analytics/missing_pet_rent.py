"""Missing pet rent analysis — identifies tenants with pets not paying selected charge codes."""

import numpy as np
import pandas as pd
import streamlit as st
from datetime import datetime, timezone, timedelta

from analytics.launch_analysis import parse_date, _resolve_launch_dt
from config import JUNK_EMAILS
from services.snowflake_io import run_query


# ─── Robust tenant matching helpers ──────────────────────────────────

def _build_paying_sets(charges_df, selected_codes):
    """Build normalized paying-tenant sets for robust matching.

    Unit/lease-level expansion: if *any* tenant on a unit has a matching pet
    charge, **all** tenants on that unit are considered paying.  This handles
    shared leases where the charge is tied to one customer but covers the
    whole unit (especially important for Entrata, also relevant for Yardi).

    Returns
    -------
    paying_tc_set : set of (property_id_str, tenant_code_upper_stripped)
        Primary match by tenant code (case-insensitive, trimmed).
    paying_email_set : set of (property_id_str, email_lower_stripped)
        Fallback match by email when tenant code doesn't align
        (e.g. lease renewal changed the code).
    """
    pet_rows = charges_df[charges_df['charge_code'].isin(selected_codes)]

    # Primary: (property_id, tenant_code) — uppercased + stripped
    tc_pairs = pet_rows[['property_id', 'tenant_code']].drop_duplicates()
    paying_tc_set = set(
        zip(
            tc_pairs['property_id'].astype(str).str.strip(),
            tc_pairs['tenant_code'].astype(str).str.strip().str.upper(),
        )
    )

    # Fallback: (property_id, email) — lowercased + stripped
    paying_email_set = set()
    if 'email' in pet_rows.columns:
        email_rows = pet_rows[['property_id', 'email']].drop_duplicates()
        email_rows = email_rows[
            email_rows['email'].astype(str).str.strip().ne('')
            & email_rows['email'].astype(str).str.strip().str.lower().ne('nan')
            & email_rows['email'].astype(str).str.strip().str.lower().ne('none')
        ]
        if not email_rows.empty:
            paying_email_set = set(
                zip(
                    email_rows['property_id'].astype(str).str.strip(),
                    email_rows['email'].astype(str).str.strip().str.lower(),
                )
            )

    # ── Unit-level expansion ───────────────────────────────────────────
    # If ANY tenant on a (property_id, unit_code) has a pet charge, ALL
    # tenants on that same unit are paying.  This prevents roommates from
    # being flagged as "missing pet rent" when the lease already has it.
    if 'unit_code' in charges_df.columns and 'property_id' in charges_df.columns:
        # Identify units that have at least one paying tenant
        pet_unit_keys = set(
            zip(
                pet_rows['property_id'].astype(str).str.strip(),
                pet_rows['unit_code'].astype(str).str.strip().str.upper(),
            )
        )
        # Discard empty unit codes
        pet_unit_keys = {(p, u) for p, u in pet_unit_keys if u and u not in ('', 'NAN', 'NONE')}

        if pet_unit_keys:
            # Find all tenants on those units (from the full charges_df)
            _all = charges_df.copy()
            _all['_pid'] = _all['property_id'].astype(str).str.strip()
            _all['_uc'] = _all['unit_code'].astype(str).str.strip().str.upper()
            _all['_unit_key'] = list(zip(_all['_pid'], _all['_uc']))
            unit_matches = _all[_all['_unit_key'].isin(pet_unit_keys)]

            # Expand paying sets with all tenants on those units
            _tc_extra = unit_matches[['_pid', 'tenant_code']].drop_duplicates()
            paying_tc_set |= set(
                zip(
                    _tc_extra['_pid'],
                    _tc_extra['tenant_code'].astype(str).str.strip().str.upper(),
                )
            )
            if 'email' in unit_matches.columns:
                _em_extra = unit_matches[['_pid', 'email']].drop_duplicates()
                _em_extra = _em_extra[
                    _em_extra['email'].astype(str).str.strip().ne('')
                    & _em_extra['email'].astype(str).str.strip().str.lower().ne('nan')
                    & _em_extra['email'].astype(str).str.strip().str.lower().ne('none')
                ]
                if not _em_extra.empty:
                    paying_email_set |= set(
                        zip(
                            _em_extra['_pid'],
                            _em_extra['email'].astype(str).str.strip().str.lower(),
                        )
                    )

    # ── Lease-level expansion (Entrata) ──────────────────────────────
    # If ANY customer on the same lease_id has a pet charge, ALL customers
    # on that lease are paying.  This handles co-tenants whose charge is
    # recorded under one customer_id but covers the whole lease.
    if 'lease_id' in charges_df.columns:
        pet_lease_keys = set(
            zip(
                pet_rows['property_id'].astype(str).str.strip(),
                pet_rows['lease_id'].astype(str).str.strip(),
            )
        )
        pet_lease_keys = {(p, lid) for p, lid in pet_lease_keys if lid and lid not in ('', 'nan', 'None')}

        if pet_lease_keys:
            _all_l = charges_df.copy()
            _all_l['_pid'] = _all_l['property_id'].astype(str).str.strip()
            _all_l['_lid'] = _all_l['lease_id'].astype(str).str.strip()
            _all_l['_lease_key'] = list(zip(_all_l['_pid'], _all_l['_lid']))
            lease_matches = _all_l[_all_l['_lease_key'].isin(pet_lease_keys)]

            _tc_extra2 = lease_matches[['_pid', 'tenant_code']].drop_duplicates()
            paying_tc_set |= set(
                zip(
                    _tc_extra2['_pid'],
                    _tc_extra2['tenant_code'].astype(str).str.strip().str.upper(),
                )
            )
            if 'email' in lease_matches.columns:
                _em_extra2 = lease_matches[['_pid', 'email']].drop_duplicates()
                _em_extra2 = _em_extra2[
                    _em_extra2['email'].astype(str).str.strip().ne('')
                    & _em_extra2['email'].astype(str).str.strip().str.lower().ne('nan')
                    & _em_extra2['email'].astype(str).str.strip().str.lower().ne('none')
                ]
                if not _em_extra2.empty:
                    paying_email_set |= set(
                        zip(
                            _em_extra2['_pid'],
                            _em_extra2['email'].astype(str).str.strip().str.lower(),
                        )
                    )

    return paying_tc_set, paying_email_set


def _is_paying(row, paying_tc_set, paying_email_set):
    """Check if a profile row matches a paying tenant.

    Matches on (property_id, tenant_code) first (case-insensitive),
    then falls back to (property_id, user_email) if available.
    """
    pid = str(row.get('PROPERTY_ID', '')).strip()
    tc = str(row.get('TENANT_CODE', '')).strip().upper()
    if (pid, tc) in paying_tc_set:
        return 1
    # Email fallback
    email = str(row.get('USER_EMAIL', '')).strip().lower()
    if email and email not in ('', 'nan', 'none') and (pid, email) in paying_email_set:
        return 1
    return 0


def _apply_paying_flag(profiles_df, paying_tc_set, paying_email_set):
    """Apply `_is_paying` per row, then propagate across all rows for the
    same user at the same property.

    A user may have multiple f_leases rows — some with a matching
    tenant_code and some with NULL / mismatched codes.  If *any* lease
    row for the same (PROPERTY_ID, USER_EMAIL) pair is paying, we mark
    *all* of that user's rows as paying so they are correctly excluded
    from the "missing pet rent" list.

    Returns the DataFrame with a `pet_rent_paid` column (1 = paying).
    """
    profiles_df = profiles_df.copy()
    profiles_df['pet_rent_paid'] = profiles_df.apply(
        lambda r: _is_paying(r, paying_tc_set, paying_email_set),
        axis=1,
    )

    # Propagate: if ANY row for (property_id, email) is paying → all are
    if 'USER_EMAIL' in profiles_df.columns:
        user_paying = (
            profiles_df
            .groupby(['PROPERTY_ID', 'USER_EMAIL'])['pet_rent_paid']
            .transform('max')
        )
        profiles_df['pet_rent_paid'] = user_paying

    return profiles_df


# ─── Snowflake query helpers ──────────────────────────────────────────

def fetch_property_manager_emails(property_ids, ancestry_id=None, parent_company_name=None):
    """
    Fetch property manager emails from Snowflake.

    Uses parent_company_ancestry_id or parent_company_name on user_enriched
    (more reliable than pm.entity_id which may not match d_properties.property_id).
    Falls back to pm.entity_id IN (property_ids) if neither is available.

    Returns a list of dicts: [{PM_EMAIL, PROPERTY_ID, PROPERTY_NAME}]
    """
    if not property_ids and not ancestry_id and not parent_company_name:
        return []

    # Resolve ancestry_id if not provided — much more reliable than pm.entity_id
    if not ancestry_id and not parent_company_name and property_ids:
        try:
            _rows = run_query(f"""
                SELECT DISTINCT parent_company_ancestry_id
                FROM PROD.common.d_properties
                WHERE property_id IN ({", ".join(str(int(pid)) for pid in property_ids)})
                  AND parent_company_ancestry_id IS NOT NULL
            """)
            _aids = [str(r["PARENT_COMPANY_ANCESTRY_ID"]) for r in _rows if r.get("PARENT_COMPANY_ANCESTRY_ID")]
            if len(_aids) == 1:
                ancestry_id = _aids[0]
        except Exception:
            pass

    # Build the WHERE filter — prefer ancestry_id on user_enriched
    if ancestry_id:
        scope_filter = f"u.parent_company_ancestry_id = '{ancestry_id}'"
    elif parent_company_name:
        safe_name = parent_company_name.replace("'", "''")
        scope_filter = f"u.parent_company_name ILIKE '%{safe_name}%'"
    else:
        props_str = ", ".join(str(int(pid)) for pid in property_ids)
        scope_filter = f"pm.entity_id IN ({props_str})"

    sql = f"""
    SELECT DISTINCT
        CASE
            WHEN u.user_email = 'parkatvenetopm@stylresidential.com'
            THEN 'parkatvenetoteam@stylresidential.com'
            WHEN u.user_email = 'admin@endeavourhsv.com'
            THEN 'contracts@endeavourhsv.com'
            ELSE u.user_email
        END AS PM_EMAIL,
        pm.entity_id AS PROPERTY_ID,
        p.property_name AS PROPERTY_NAME
    FROM PROD.staging.stg_petscreening__property_manager_permissions_only pm
    LEFT JOIN PROD.petscreening.petscreening__user_enriched u
        ON u.user_id = pm.user_id
    LEFT JOIN PROD.common.d_properties p
        ON pm.entity_id = p.property_id
    WHERE u.user_status = 'active'
      AND pm.name IN (
          'manager_admin_permission',
          'manager_view_hipaa_permission',
          'manager_reviewer_permission',
          'manager_view_only_permission'
      )
      AND u.compliance_status != 'n/a|manual|individual_level'
      AND u.user_role = 'property_manager'
      AND {scope_filter}
      AND u.user_email IS NOT NULL
      AND TRIM(u.user_email) <> ''
    ORDER BY PROPERTY_NAME, PM_EMAIL
    """
    try:
        return run_query(sql)
    except Exception as e:
        st.error(f"Error fetching PM emails: {e}")
        return []


# ─── Main analysis functions ──────────────────────────────────────────

def generate_missing_pet_rent_report(all_charges_df, selected_codes, property_ids):
    """
    Generate the 'Missing Pet Rent' report using LIVE API rent roll data.

    Uses the **same** profile identification as fetch_missing_pet_rent_by_property
    so that counts match across all tabs (Charts, Summary, Report).

    Flow:
      1. Live API data → identify tenants paying selected charge codes
      2. Snowflake → PetScreening profiles with household pets (+ tenant_code)
         ** Same filters as Charts tab: compliant, household, active **
      3. Python join → profile with pet + no selected charge = Profile_No_Rent
      4. Snowflake → R_MONTHLY_EXECUTIVE_SUMMARY for detailed pet/profile columns
      5. Python join → final report (per-pet granularity for download)
    """
    pmc_system = st.session_state.get("pmc_system", "yardi")

    # ── Scope to properties that actually have PET-RELATED charge data ──
    # Only include properties with at least 1 charge matching selected codes.
    # Without this, properties with other charges but no pet charges would
    # have ALL their PetScreening profiles flagged as "missing," inflating
    # the count with unknowns.
    _pet_charges = all_charges_df[all_charges_df['charge_code'].isin(selected_codes)]
    _charge_pids = set(_pet_charges['property_id'].astype(str).str.strip().unique())
    property_ids = [pid for pid in property_ids if str(pid).strip() in _charge_pids]
    if not property_ids:
        return pd.DataFrame()

    props_str = ", ".join(str(int(pid)) for pid in property_ids)

    # ── Step 1: From LIVE API data, build set of tenants paying selected charges ──
    paying_tc_set, paying_email_set = _build_paying_sets(all_charges_df, selected_codes)

    realpage_current_resident_clause = ""
    if pmc_system == 'real_page':
        # RealPage resident/charge data is a current snapshot. Require the
        # PetScreening profile's lease/email to still exist in the latest
        # RealPage resident list; otherwise old f_leases rows make past tenants
        # look like current missing pet-rent opportunities.
        realpage_current_resident_clause = """
      AND EXISTS (
          SELECT 1
          FROM PROD.STAGING.STG_PMC_INTEGRATIONS_REALPAGE__GETRESIDENTLIST r
          WHERE r.pmc_id = PARSE_JSON(p.property_source_id):"pmcid"::STRING
            AND r.site_id = PARSE_JSON(p.property_source_id):"siteid"::STRING
            AND (
                r.lease_id = COALESCE(
                    l.lease_source_external_id:"lease_id"::STRING,
                    l.lease_external_id::STRING
                )
                OR LOWER(TRIM(r.email)) = LOWER(TRIM(ue.user_email))
            )
            AND COALESCE(r.lease_status, '') NOT IN ('Former', 'Former Applicant')
            AND (r.move_out_date IS NULL OR r.move_out_date >= CURRENT_DATE)
      )
        """

    # ── Step 2: Query Snowflake for PetScreening household profiles ──
    # ** MUST match fetch_missing_pet_rent_by_property exactly: compliant + household + active **
    sql_profiles = f"""
    SELECT DISTINCT
        du.property_id,
        COALESCE(
            l.lease_source_external_id:tenant_code::STRING,
            l.lease_source_external_id:"customerId"::STRING,
            l.lease_source_external_id:"customer_id"::STRING,
            l.lease_source_external_id:"lease_id"::STRING
        ) AS tenant_code,
        ue.user_email,
        ue.user_first_name,
        ue.user_last_name,
        ue.compliance_status,
        ue.user_pet_type,
        ue.user_pet_status,
        ue.user_profile_url,
        p.property_name,
        du.unit_address_1,
        du.unit_address_2,
        CONCAT_WS(' ', du.unit_address_1, du.unit_address_2) AS full_unit_address
    FROM PROD.common.d_units du
    JOIN PROD.common.d_properties p
        ON du.property_id = p.property_id
    JOIN PROD.petscreening.petscreening__user_enriched ue
        ON ue.unit_id = du.unit_id
    JOIN PROD.common.f_leases l
        ON du.unit_key = l.unit_key AND l.user_key = ue.user_key
    WHERE du.unit_source = '{pmc_system}'
      AND du.property_id IN ({props_str})
      AND ue.compliance_status = 'compliant'
      AND ue.user_pet_type = 'household'
      AND ue.user_pet_status = 'active'
      AND ue.user_email IS NOT NULL
      AND TRIM(ue.user_email) <> ''
      AND LOWER(TRIM(ue.user_email)) NOT IN ({JUNK_EMAILS})
      {realpage_current_resident_clause}
    """
    profiles_df = pd.DataFrame(run_query(sql_profiles))

    if profiles_df.empty:
        return pd.DataFrame()

    # ── Step 3: Match live charges to profiles ──
    profiles_df = _apply_paying_flag(profiles_df, paying_tc_set, paying_email_set)

    # ── Step 3b: Entrata freshness filter — exclude profiles not in API data ──
    if pmc_system == 'entrata' and 'email' in all_charges_df.columns:
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
        def _in_api(row):
            pid = str(row.get('PROPERTY_ID', '')).strip()
            em = str(row.get('USER_EMAIL', '')).strip().lower()
            return em in api_emails_by_prop.get(pid, set())
        profiles_df['_in_api'] = profiles_df.apply(_in_api, axis=1)
        _n_before = len(profiles_df)
        profiles_df = profiles_df[profiles_df['_in_api'] | (profiles_df['pet_rent_paid'] == 1)]
        _n_excluded = _n_before - len(profiles_df)
        if _n_excluded > 0:
            st.info(f"Entrata freshness filter: excluded {_n_excluded:,} profiles not found in current API data")
        profiles_df = profiles_df.drop(columns=['_in_api'], errors='ignore')

    # Tenants with household pets who are NOT paying any selected charge
    missing_profiles = profiles_df[profiles_df['pet_rent_paid'] == 0].copy()

    if missing_profiles.empty:
        return pd.DataFrame()

    # Build lookup set: (email_lower, property_id_str) for these missing tenants
    missing_lookup = set(
        zip(
            missing_profiles['USER_EMAIL'].str.lower().str.strip(),
            missing_profiles['PROPERTY_ID'].astype(str),
        )
    )

    # ── Step 4: Query R_MONTHLY_EXECUTIVE_SUMMARY for detailed pet-level output ──
    # No extra restrictive filters — the profile identification in Step 2–3 is
    # already canonical.  We only need pet_profile_type/status to get the right
    # pet records from the exec summary.
    sql_exec = f"""
    SELECT DISTINCT
        m.pet_profile_url,
        m.pet_id,
        m.pet_name,
        m.breed,
        m.species,
        m.user_first_name,
        m.user_last_name,
        m.user_email,
        m.unit_id,
        m.lease_start_date,
        m.lease_end_date,
        m.resident_type,
        m.unit_address_1,
        m.unit_address_2,
        CONCAT_WS(' ', m.unit_address_1, m.unit_address_2) AS full_unit_address,
        m.property_name,
        m.property_id,
        m.pet_level_compliance_status,
        m.user_level_compliance_status,
        m.pet_profile_type,
        m.pet_profile_status,
        m.property_source_name
    FROM PROD.REPORTING.R_MONTHLY_EXECUTIVE_SUMMARY AS m
    WHERE m.property_id IN ({props_str})
      AND m.pet_profile_type = 'household'
      AND m.pet_profile_status = 'active'
      AND m.property_source_name = '{pmc_system}'
    """
    exec_df = pd.DataFrame(run_query(sql_exec))

    if exec_df.empty:
        return pd.DataFrame()

    # Deduplicate: a tenant can have multiple exec summary rows (current + past lease).
    # Keep 'current' rows preferentially; if a tenant only has non-current rows, keep them
    # (profile matching in Step 2-3 already validated they're active).
    if 'RESIDENT_TYPE' in exec_df.columns:
        _rt_lower = exec_df['RESIDENT_TYPE'].astype(str).str.strip().str.lower()
        exec_df['_is_current'] = _rt_lower.str.contains('current', na=False)
        # Sort so current rows come first, then drop duplicates per tenant+property+pet
        exec_df = exec_df.sort_values('_is_current', ascending=False)
        _dedup_cols = ['USER_EMAIL', 'PROPERTY_ID', 'PET_ID']
        _dedup_cols = [c for c in _dedup_cols if c in exec_df.columns]
        if _dedup_cols:
            exec_df = exec_df.drop_duplicates(subset=_dedup_cols, keep='first')
        exec_df = exec_df.drop(columns=['_is_current'], errors='ignore')

    # ── Step 5: Keep only exec summary records whose (email, property) is missing ──
    report_df = exec_df[
        exec_df.apply(
            lambda r: (
                str(r['USER_EMAIL']).lower().strip(),
                str(r['PROPERTY_ID']),
            ) in missing_lookup,
            axis=1,
        )
    ].copy()

    # Add the status columns to match the original query output
    report_df['has_petscreening_pets'] = 1
    report_df['pet_rent_paid'] = 0
    report_df['overall_status'] = 'Profile_No_Rent'

    return report_df


def fetch_missing_pet_rent_by_property(
    all_charges_df, selected_codes, property_ids, launch_dates, months,
):
    """
    Per-property, per-month estimated uncollected pet rent, accounting for:
      • Each missing tenant's actual lease dates (move_in/lease_from → move_out/lease_to)
      • Property launch date (only count months on or after launch)
      • Recurring rent vs one-time deposit classification

    Returns
    -------
    dict  – {property_name: {
        "missing_count": int,               # total missing tenants
        "monthly_missing": {month: float},   # estimated missing $ per month
        "avg_recurring": float,              # avg recurring pet fee/mo
        "avg_onetime": float,                # avg one-time deposit
        "charge_type_label": str,            # "rent", "deposit", or "rent + deposit"
        "missing_tenants": [list of dicts],
    }}
    """
    pmc_system = st.session_state.get("pmc_system", "yardi")

    # ── Scope to properties that actually have PET-RELATED charge data ──
    # Only include properties with at least 1 charge matching selected codes.
    # Without this, properties with other charges but no pet charges would
    # have ALL their PetScreening profiles flagged as "missing," inflating
    # the count with unknowns.
    pet_charges = all_charges_df[all_charges_df['charge_code'].isin(selected_codes)].copy()
    _charge_pids = set(pet_charges['property_id'].astype(str).str.strip().unique())
    property_ids = [pid for pid in property_ids if str(pid).strip() in _charge_pids]
    if not property_ids:
        return {}

    props_str = ", ".join(str(int(pid)) for pid in property_ids)

    # ── Step 1: From LIVE API data, classify charge codes and compute avg fees ──
    pet_charges['_amt'] = pd.to_numeric(pet_charges['charge_amount'], errors='coerce').fillna(0)
    pet_charges['_from'] = pet_charges['charge_from_date'].apply(parse_date)
    pet_charges['_to'] = pet_charges['charge_to_date'].apply(parse_date)

    paying_tc_set, paying_email_set = _build_paying_sets(all_charges_df, selected_codes)
    properties_with_payers = set(pet_charges['property_id'].astype(str).unique())

    # Classify each charge code at each property as recurring or one-time.
    # Entrata: use the explicit `frequency` field when available.
    # Yardi: infer from median date span (> 60 days → recurring).
    _has_frequency = 'frequency' in pet_charges.columns
    code_class = {}   # {(property_name, charge_code): "recurring" | "onetime"}
    for (pname, code), grp in pet_charges.groupby(['property_name', 'charge_code']):
        if pmc_system in ("real_page", "appfolio"):
            # RealPage rows are monthly scheduled-charge snapshots keyed by
            # post_month. AppFolio getrecurringcharges rows are recurring by endpoint semantics.
            # but pet rent is still recurring for missing-rent estimation.
            code_class[(pname, code)] = "recurring"
        elif _has_frequency:
            # Entrata: use the frequency field directly
            freqs = grp['frequency'].dropna().str.strip().str.lower()
            onetime_count = (freqs == 'one-time').sum()
            monthly_count = (freqs.isin(['monthly', 'recurring'])).sum()
            if onetime_count > monthly_count:
                code_class[(pname, code)] = "onetime"
            else:
                code_class[(pname, code)] = "recurring"
        else:
            # Yardi: infer from median date span
            spans = []
            for _, row in grp.iterrows():
                f, t = row['_from'], row['_to']
                if f and t and not pd.isna(f) and not pd.isna(t):
                    spans.append((t - f).days)
            # No valid date spans → assume recurring (missing dates ≠ one-time)
            median_span = float(np.median(spans)) if spans else None
            if median_span is None:
                code_class[(pname, code)] = "recurring"
            else:
                code_class[(pname, code)] = "recurring" if median_span > 60 else "onetime"

    # Apply user overrides (from the charge type classification UI)
    try:
        _user_overrides = st.session_state.get("charge_type_overrides", {})
        if _user_overrides:
            for (pname, code) in list(code_class.keys()):
                if code in _user_overrides:
                    code_class[(pname, code)] = _user_overrides[code]
    except Exception:
        pass

    # Avg fee per property, split by recurring vs one-time
    avg_recurring_by_prop = {}   # avg monthly recurring fee per tenant
    avg_onetime_by_prop = {}     # avg one-time deposit per tenant
    for pname, pgrp in pet_charges.groupby('property_name'):
        rec_amts, ot_amts = [], []
        for tc, tgrp in pgrp.groupby('tenant_code'):
            for _, row in tgrp.iterrows():
                cls = code_class.get((pname, row['charge_code']), 'recurring')
                if cls == 'recurring':
                    rec_amts.append(row['_amt'])
                else:
                    ot_amts.append(row['_amt'])
        # De-dup per tenant (avg per tenant, then avg across tenants)
        if rec_amts:
            avg_recurring_by_prop[pname] = float(np.mean(rec_amts))
        if ot_amts:
            avg_onetime_by_prop[pname] = float(np.mean(ot_amts))

    # Build tenant_info lookup from ALL API data (every tenant, all charge codes)
    # so we can get lease dates for any tenant, even those without pet charges.
    tenant_info = {}  # {(property_name, tenant_code): {move_in, move_out, lease_from, lease_to, status}}
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

    realpage_current_resident_clause = ""
    if pmc_system == 'real_page':
        # RealPage is current-snapshot data. Do not let stale f_leases rows from
        # previous residents inflate current missing pet-rent estimates.
        realpage_current_resident_clause = """
      AND EXISTS (
          SELECT 1
          FROM PROD.STAGING.STG_PMC_INTEGRATIONS_REALPAGE__GETRESIDENTLIST r
          WHERE r.pmc_id = PARSE_JSON(p.property_source_id):"pmcid"::STRING
            AND r.site_id = PARSE_JSON(p.property_source_id):"siteid"::STRING
            AND (
                r.lease_id = COALESCE(
                    l.lease_source_external_id:"lease_id"::STRING,
                    l.lease_external_id::STRING
                )
                OR LOWER(TRIM(r.email)) = LOWER(TRIM(ue.user_email))
            )
            AND COALESCE(r.lease_status, '') NOT IN ('Former', 'Former Applicant')
            AND (r.move_out_date IS NULL OR r.move_out_date >= CURRENT_DATE)
      )
        """

    # ── Step 2: Snowflake → PetScreening household profiles with tenant_code ──
    sql_profiles = f"""
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
        du.unit_address_1,
        du.unit_address_2,
        CONCAT_WS(' ', du.unit_address_1, du.unit_address_2) AS full_unit_address
    FROM PROD.common.d_units du
    JOIN PROD.common.d_properties p
        ON du.property_id = p.property_id
    JOIN PROD.petscreening.petscreening__user_enriched ue
        ON ue.unit_id = du.unit_id
    JOIN PROD.common.f_leases l
        ON du.unit_key = l.unit_key AND l.user_key = ue.user_key
    WHERE du.unit_source = '{pmc_system}'
      AND du.property_id IN ({props_str})
      AND ue.compliance_status = 'compliant'
      AND ue.user_pet_type = 'household'
      AND ue.user_pet_status = 'active'
      AND ue.user_email IS NOT NULL
      AND TRIM(ue.user_email) <> ''
      AND LOWER(TRIM(ue.user_email)) NOT IN ({JUNK_EMAILS})
      {realpage_current_resident_clause}
    """
    profiles_df = pd.DataFrame(run_query(sql_profiles))

    if profiles_df.empty:
        return {}

    # ── Step 3: Match to paying set (case-insensitive tc + email fallback) ──
    # If a user has multiple leases, paying on ANY lease counts for all rows.
    profiles_df = _apply_paying_flag(profiles_df, paying_tc_set, paying_email_set)

    # ── Step 3b: Entrata freshness filter ──
    if pmc_system == 'entrata' and 'email' in all_charges_df.columns:
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
        def _in_api_check(row):
            pid = str(row.get('PROPERTY_ID', '')).strip()
            em = str(row.get('USER_EMAIL', '')).strip().lower()
            return em in api_emails_by_prop.get(pid, set())
        profiles_df['_in_api'] = profiles_df.apply(_in_api_check, axis=1)
        profiles_df = profiles_df[profiles_df['_in_api'] | (profiles_df['pet_rent_paid'] == 1)]
        profiles_df = profiles_df.drop(columns=['_in_api'], errors='ignore')

    # Compute a portfolio-wide average fee as fallback for properties with no payers
    _all_recurring = [v for v in avg_recurring_by_prop.values() if v > 0]
    _all_onetime = [v for v in avg_onetime_by_prop.values() if v > 0]
    portfolio_avg_rec = float(np.mean(_all_recurring)) if _all_recurring else 0
    portfolio_avg_ot = float(np.mean(_all_onetime)) if _all_onetime else 0

    # ── Step 4: Per-property, per-month missing revenue ──
    m0, mN = months[0], months[-1]
    result = {}
    for pname, grp in profiles_df.groupby('PROPERTY_NAME'):
        missing = grp[grp['pet_rent_paid'] == 0]
        if missing.empty:
            continue

        # Property launch date — only count missing rent on or after launch
        launch_dt = _resolve_launch_dt(launch_dates.get(pname))
        launch_month = datetime(launch_dt.year, launch_dt.month, 1) if launch_dt else None

        # Use property-specific avg if available, otherwise fall back to portfolio avg
        avg_rec = avg_recurring_by_prop.get(pname, portfolio_avg_rec)
        avg_ot = avg_onetime_by_prop.get(pname, portfolio_avg_ot)

        # Determine what charge types are active at this property
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

        monthly_missing = {m: 0.0 for m in months}
        missing_tenants = []

        for _, row in missing.iterrows():
            tc = str(row['TENANT_CODE'])
            tinfo = tenant_info.get((pname, tc))

            # Determine tenant's active period
            if tinfo:
                t_start = tinfo['move_in'] or tinfo['lease_from']
                t_end = tinfo['move_out'] or tinfo['lease_to']
                t_status = tinfo['status']
            else:
                t_start = None
                t_end = None
                t_status = 'current'

            # Default start: if we have no date, assume they were active at launch
            if t_start is None:
                t_start = launch_dt if launch_dt else m0
            active_from = datetime(t_start.year, t_start.month, 1)

            # Default end: if current tenant or no end date, they're still active
            if t_end is None or t_status == 'current':
                active_to = mN
            else:
                active_to = datetime(t_end.year, t_end.month, 1)

            # Only count months on or after property launch
            if launch_month:
                active_from = max(active_from, launch_month)

            # Clamp to display window
            active_from = max(active_from, m0)
            active_to = min(active_to, mN)

            first_month_done = False
            for m in months:
                if active_from <= m <= active_to:
                    # Recurring rent: add every active month
                    if has_recurring:
                        monthly_missing[m] += avg_rec
                    # One-time deposit: add only in tenant's first active month
                    if has_onetime and not first_month_done:
                        monthly_missing[m] += avg_ot
                        first_month_done = True

            # Build unit label: prefer unit_address_1, fall back to full_unit_address
            _unit_label = str(row.get('UNIT_ADDRESS_1', '') or row.get('FULL_UNIT_ADDRESS', '') or '').strip()

            missing_tenants.append({
                "name": f"{row.get('USER_FIRST_NAME', '')} {row.get('USER_LAST_NAME', '')}".strip(),
                "email": row.get('USER_EMAIL', ''),
                "profile_url": row.get('USER_PROFILE_URL', ''),
                "unit": _unit_label,
                "active_from": active_from.strftime('%b %Y'),
                "active_to": active_to.strftime('%b %Y') if active_to < mN else "Current",
            })

        # Compute summary stats
        total_missing_in_window = sum(monthly_missing.values())
        months_with_missing = sum(1 for v in monthly_missing.values() if v > 0)
        est_per_month = total_missing_in_window / months_with_missing if months_with_missing > 0 else 0

        # Count unique tenants by email (not raw rows which may have duplicates
        # from multiple lease rows for the same person)
        _unique_missing = missing['USER_EMAIL'].nunique() if 'USER_EMAIL' in missing.columns else len(missing)

        result[pname] = {
            "missing_count": _unique_missing,
            "monthly_missing": monthly_missing,
            "avg_recurring": avg_rec,
            "avg_onetime": avg_ot,
            "charge_type_label": charge_label,
            "estimated_missing_per_month": est_per_month,
            "total_missing_in_window": total_missing_in_window,
            "missing_tenants": missing_tenants,
        }
    return result
