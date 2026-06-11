"""Launch analysis and compliance/adoption helpers."""

import streamlit as st
import pandas as pd
from datetime import datetime

from services.snowflake_io import run_query


# ─── Date parsing helpers ─────────────────────────────────────────────

def parse_date(d):
    if pd.isna(d) or not d or str(d).strip() == "":
        return None
    for fmt in ["%m/%d/%Y", "%m-%d-%Y", "%Y-%m-%d"]:
        try:
            return datetime.strptime(str(d).strip(), fmt)
        except ValueError:
            continue
    return None


def _resolve_launch_dt(launch):
    """Parse a launch date value into a datetime or None."""
    if not launch:
        return None
    if isinstance(launch, str):
        try:
            return datetime.strptime(launch[:10], "%Y-%m-%d")
        except Exception:
            return None
    if hasattr(launch, 'year'):
        return launch
    return None


# ─── Launch analysis ──────────────────────────────────────────────────

def compute_launch_analysis(monthly_by_prop, months, launch_dates):
    """
    For each property with a launch date, compute:
      - pre_avg             = average monthly revenue in up to 6 months BEFORE launch
      - post_recent_avg     = average monthly revenue across all completed post-launch months
      - diff_monthly        = cumulative impact ÷ completed post months  (average monthly uplift since launch)
      - diff_total          = actual observed cumulative lift: total_post_revenue − (pre_avg × n_post_months)
      - post_monthly_avg    = average of ALL post months (kept for backward compat / charts)

    Methodology (updated 2026-07-14):
      Pre-baseline:  up to 6 months before launch (uses whatever is available)
      Post-current:  all completed post-launch months (excludes current partial month)
      Monthly lift:  cumulative impact ÷ completed post months
      Total lift:    sum(all post revenue) − (pre_avg × total post months)  [actual observed]

    Returns dict keyed by property name.
    """
    today = datetime.now()
    current_month = datetime(today.year, today.month, 1)

    analysis = {}
    for prop, prop_data in monthly_by_prop.items():
        launch = launch_dates.get(prop)
        if launch is None:
            continue
        # Skip NaT / NaN values
        try:
            if pd.isna(launch):
                continue
        except (TypeError, ValueError):
            pass
        if isinstance(launch, str):
            try:
                launch = datetime.strptime(launch[:10], "%Y-%m-%d")
            except Exception:
                continue
        # Convert pandas Timestamp / numpy datetime to pure Python datetime
        if hasattr(launch, 'to_pydatetime'):
            launch = launch.to_pydatetime()
        # Final guard: make sure year/month are valid numbers
        try:
            yr, mo = int(launch.year), int(launch.month)
        except (ValueError, TypeError, OverflowError):
            continue

        # Snap launch to first-of-month; the launch month itself is "post"
        # (PetScreening was active for at least part of this month)
        launch_month = datetime(yr, mo, 1)
        first_post_month = launch_month  # launch month = first post-launch month

        pre_months = [m for m in months if m < first_post_month]
        post_months = [m for m in months if m >= first_post_month]

        if not post_months:
            continue

        n_post = len(post_months)

        post_total = sum(prop_data.get(m, 0) for m in post_months)

        # Pre-launch avg: use up to 6 OBSERVED months before launch.
        # Important: `months` is the display calendar, not proof that a property
        # has charge data for every month. Treating missing dict keys as $0 would
        # fabricate a fake baseline (e.g. one real $50 pre-PS bar across a 6-month
        # window becomes $8/mo). Only average months that are actually present in
        # the property's revenue series; explicit zero values still count if the
        # data source emitted them.
        pre_baseline_months = pre_months[-6:] if len(pre_months) >= 1 else pre_months
        pre_values = [prop_data[m] for m in pre_baseline_months if m in prop_data]
        n_observed_pre = len(pre_values)
        pre_avg = sum(pre_values) / len(pre_values) if pre_values else 0
        baseline_month_label = (
            f"{n_observed_pre} observed pre month"
            if n_observed_pre == 1
            else f"{n_observed_pre} observed pre months"
        )

        # Post-launch: all-time average (kept for charts and backward compat)
        post_monthly_avg = post_total / n_post if n_post > 0 else 0

        # Completed post months (excludes current partial month so we don't undercount)
        completed_post = [m for m in post_months if m < current_month]
        n_completed_post = len(completed_post)
        completed_post_total = sum(prop_data.get(m, 0) for m in completed_post)

        # Post-launch avg across all completed months (used for display & baseline check)
        post_recent_avg = (
            completed_post_total / n_completed_post
            if n_completed_post > 0 else post_monthly_avg
        )

        # Total lift = actual observed cumulative difference
        # (what they actually collected minus what they would have at the old rate)
        diff_total = post_total - (pre_avg * n_post)

        # Monthly lift = cumulative impact ÷ completed post months
        # (average monthly uplift since launch — mathematically consistent: monthly × months ≈ cumulative)
        diff_monthly = (completed_post_total - (pre_avg * n_completed_post)) / n_completed_post if n_completed_post > 0 else 0

        # Baseline is "meaningful" if pre_avg is >= 2% of post_recent_avg.
        # Properties with near-zero baselines (e.g. $8 vs $5,600 post) weren't
        # really charging pet rent before PS — their "lift" is misleading.
        _baseline_meaningful = (
            post_recent_avg <= 0  # no post data — keep whatever we have
            or pre_avg >= post_recent_avg * 0.02
        )

        # ADDITIONAL GUARD: detect properties whose CHARGE DATA HISTORY
        # starts AFTER their launch date. This is the RealPage case — EKS
        # job started backfilling Nov 2024, but properties may have launched
        # well before that. Their pre-PS "baseline" of $0/mo is an artifact
        # of missing data, not a real zero. Without this guard, those
        # properties would inflate the aggregate lift (e.g. Drift at the
        # Forum: launched Feb 2022, earliest pet charge Nov 2024, lift
        # calculation treats 2021-2022 as $0 baseline = bogus).
        first_data_month = next(
            (m for m in months if prop_data.get(m, 0) > 0),
            None,
        )
        _data_starts_after_launch = (
            first_data_month is not None
            and launch_month is not None
            and first_data_month > launch_month
            and (first_data_month.year - launch_month.year) * 12
                + (first_data_month.month - launch_month.month) >= 3
        )
        if _data_starts_after_launch:
            # The pre-launch "baseline" is fabricated from missing data.
            # Mark the baseline as unmeaningful so downstream display +
            # aggregate code excludes this property from lift attribution.
            _baseline_meaningful = False

        analysis[prop] = {
            "n_post": n_post,
            "n_pre": n_observed_pre,
            "n_pre_calendar": len(pre_baseline_months),
            "baseline_month_label": baseline_month_label,
            "n_recent_post": n_completed_post,
            "all_pre_months": len(pre_months),
            "baseline_reliable": n_observed_pre >= 3,
            "baseline_meaningful": _baseline_meaningful,
            "data_starts_after_launch": _data_starts_after_launch,
            "first_data_month": first_data_month,
            "pre_avg": pre_avg,
            "post_total": post_total,
            "post_monthly_avg": post_monthly_avg,
            "post_recent_avg": post_recent_avg,
            "diff_monthly": diff_monthly,
            "diff_total": diff_total,
            "launch_month": launch_month,
        }
    return analysis


def _launch_analysis_is_comparable(a):
    """Return True when a property can contribute to pre/post lift metrics.

    Comparable means we observed at least one real pre-PS revenue month and the
    baseline is meaningful. A short 1-2 month baseline is still included, but it
    remains flagged as low-data/insufficient in charts and tables. This keeps the
    portfolio aggregate aligned with individual property lift badges.
    """
    return bool(a and a.get("n_pre", 0) > 0 and a.get("baseline_meaningful", True))


# ─── Compliance / adoption data from QBR table ───────────────────────

@st.cache_data(ttl=600)
def fetch_compliance_data(property_ids_tuple):
    """
    Fetch per-property, per-month adoption rates from
    PROD.REPORTING.R_QUARTERLY_BUSINESS_REVIEW_REPORTING.

    Returns a dict:
      {
        property_id: {
          datetime(month): {
            "unit_adoption": float 0-1,
            "resident_adoption": float 0-1,
            "active_units": int, "total_units": int,
            "active_users": int, "total_users": int,
          }, ...
        }, ...
      }
    """
    property_ids = list(property_ids_tuple)
    if not property_ids:
        return {}
    ids_str = ", ".join(str(int(pid)) for pid in property_ids)
    rows = run_query(f"""
        SELECT
            PROPERTY_ID,
            PROPERTY_NAME,
            PERIOD_MONTH,
            ACTIVE_UNITS,
            TOTAL_UNITS,
            ACTIVE_USERS,
            TOTAL_USERS
        FROM PROD.REPORTING.R_QUARTERLY_BUSINESS_REVIEW_REPORTING
        WHERE PROPERTY_ID IN ({ids_str})
          AND AFTER_PROPERTY_LAUNCH_FLAG = TRUE
          AND (TOTAL_UNITS > 0 OR TOTAL_USERS > 0)
        ORDER BY PROPERTY_ID, PERIOD_MONTH
    """)
    result = {}
    for r in rows:
        pid = int(r["PROPERTY_ID"])  # normalize to Python int
        pm = r["PERIOD_MONTH"]
        if hasattr(pm, 'to_pydatetime'):
            pm = pm.to_pydatetime()
        elif isinstance(pm, str):
            try:
                pm = datetime.strptime(pm[:10], "%Y-%m-%d")
            except Exception:
                continue
        month_key = datetime(pm.year, pm.month, 1)
        au = r["ACTIVE_UNITS"] or 0
        tu = r["TOTAL_UNITS"] or 0
        ar = r["ACTIVE_USERS"] or 0
        tr = r["TOTAL_USERS"] or 0

        if pid not in result:
            result[pid] = {}
        result[pid][month_key] = {
            "unit_adoption": au / tu if tu > 0 else None,
            "resident_adoption": ar / tr if tr > 0 else None,
            "active_units": au,
            "total_units": tu,
            "active_users": ar,
            "total_users": tr,
        }
    return result


def _build_property_id_lookup(parsed_charges):
    """Build a property_name → property_id mapping from parsed charges."""
    lookup = {}
    for rec in parsed_charges:
        pname = rec.get("property_name")
        pid = rec.get("property_id")
        if pname and pid and pname not in lookup:
            try:
                lookup[pname] = int(pid)
            except (ValueError, TypeError):
                lookup[pname] = pid
    return lookup
