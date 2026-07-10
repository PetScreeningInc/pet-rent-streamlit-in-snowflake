"""
Pet Rent Analysis — Streamlit Application
Connects to Snowflake, fetches Yardi GetRentroll data, and visualizes
pet/fee collection over time per property and parent company.
"""

import os
import io
import json
import xml.etree.ElementTree as ET
import urllib.parse
from datetime import datetime, timezone, timedelta
from collections import defaultdict, Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
from plotly.subplots import make_subplots
import numpy as np
import requests
import snowflake.connector
from dotenv import load_dotenv

load_dotenv()

# ─── Page config ─────────────────────────────────────────────────────
st.set_page_config(
    page_title="PetScreening Value Report",
    layout="wide",
    initial_sidebar_state="expanded",
)



from analytics.launch_analysis import _build_property_id_lookup, _launch_analysis_is_comparable, _run_data_health_checks, compute_launch_analysis, fetch_compliance_data, parse_date
from analytics.missing_pet_rent import _apply_paying_flag, _build_paying_sets, _classify_pet_charge_codes, _estimate_property_fees, _recent_paying_charges, fetch_missing_pet_rent_by_property, fetch_property_manager_emails, generate_missing_pet_rent_report
from analytics.suspected_undisclosed import fetch_suspected_undisclosed_by_property, generate_suspected_undisclosed_report
from components.charts import _latest_observed_revenue_month, build_current_snapshot_chart, build_individual_property_charts
from components.ui_helpers import _PS_LOGO_DARK_URI, _PS_LOGO_WHITE_URI, _auto_export_dir, _render_property_funnel, _render_table, inject_brand_css, require_app_password
from config import JUNK_EMAILS
from reports.html_report import generate_exec_summary_html, generate_html_report
from reports.pdf_report import _n_props, generate_tranche_pdf
from services.appfolio import fetch_appfolio_for_properties, load_appfolio_all_properties, load_appfolio_parent_companies, load_appfolio_properties_for_selection
from services.entrata import fetch_entrata_for_properties, load_entrata_all_properties, load_entrata_parent_companies, load_entrata_properties_for_selection
from services.realpage import load_realpage_all_properties, load_realpage_parent_companies, load_realpage_properties_for_selection
from services.snowflake_io import get_snowflake_connection
from services.yardi import fetch_rentroll_for_properties, load_all_properties, load_parent_companies, load_properties_for_selection

# Re-exports for the batch scripts and export tools, which import these via
# `from app import ...` (kept so those scripts stay identical to the
# actively developed pet-rent repo). Not used by the app UI directly.
from services.yardi import (  # noqa: F401
    SOAP_HEADERS, YARDI_LICENSE_TOKEN, build_soap_payload,
    extract_charges_from_property, extract_property, xml_to_dict,
)
from services.entrata import (  # noqa: F401
    ENTRATA_API_KEY, ENTRATA_BASE_URL,
    _extract_ar_charges_from_entrata_lease,
    _extract_charges_from_entrata_lease,
    _fetch_entrata_leases_for_property,
)
from services.realpage import (  # noqa: F401
    fetch_realpage_for_properties, fetch_realpage_hybrid,
)

require_app_password()
inject_brand_css()

# ═════════════════════════════════════════════════════════════════════
# STREAMLIT UI
# ═════════════════════════════════════════════════════════════════════


# ─── PMC system routing (for the 3-way Yardi/Entrata/RealPage switch) ──
# AppFolio intentionally excluded while API access (MFA) is unresolved —
# provider code kept as placeholder.
ALL_PMC_SYSTEMS = ("yardi", "entrata", "real_page")


def _scoped_systems(pmc_system=None):
    """The concrete source systems a pmc_system value stands for."""
    if pmc_system is None:
        pmc_system = st.session_state.get("pmc_system", "yardi")
    return list(ALL_PMC_SYSTEMS) if pmc_system == "all" else [pmc_system]


def _load_parent_companies_for(pmc_system):
    if pmc_system == "all":
        # Union across systems: a big parent (e.g. Greystar) can run Yardi,
        # Entrata AND RealPage properties. Merge by parent name, sum counts.
        merged = {}
        for _sys in ALL_PMC_SYSTEMS:
            for r in _load_parent_companies_for(_sys):
                name = r["PARENT_COMPANY_NAME"]
                m = merged.get(name)
                if m is None:
                    merged[name] = dict(r)
                    merged[name]["_SYSTEMS"] = [_sys]
                else:
                    m["TOTAL_PROPS"] = (m.get("TOTAL_PROPS") or 0) + (r.get("TOTAL_PROPS") or 0)
                    m["API_PROPS"] = (m.get("API_PROPS") or 0) + (r.get("API_PROPS") or 0)
                    if not m.get("ANCESTRY_ID") and r.get("ANCESTRY_ID"):
                        m["ANCESTRY_ID"] = r.get("ANCESTRY_ID")
                    m["_SYSTEMS"].append(_sys)
        return sorted(merged.values(), key=lambda r: r["PARENT_COMPANY_NAME"])
    if pmc_system == "entrata":
        return load_entrata_parent_companies()
    if pmc_system == "real_page":
        return load_realpage_parent_companies()
    if pmc_system == "appfolio":
        return load_appfolio_parent_companies()
    return load_parent_companies()

def _load_all_properties_for(pmc_system):
    if pmc_system == "all":
        out = []
        for _sys in ALL_PMC_SYSTEMS:
            for r in _load_all_properties_for(_sys):
                row = dict(r)
                row["_PMC_SYSTEM"] = _sys
                out.append(row)
        return out
    if pmc_system == "entrata":
        return load_entrata_all_properties()
    if pmc_system == "real_page":
        return load_realpage_all_properties()
    if pmc_system == "appfolio":
        return load_appfolio_all_properties()
    return load_all_properties()

def _load_properties_for_selection_for(pmc_system, **kwargs):
    if pmc_system == "all":
        out = []
        for _sys in ALL_PMC_SYSTEMS:
            try:
                rows = _load_properties_for_selection_for(_sys, **kwargs)
            except Exception:
                rows = []
            for r in rows or []:
                row = dict(r)
                row["_PMC_SYSTEM"] = _sys
                out.append(row)
        return out
    if pmc_system == "entrata":
        return load_entrata_properties_for_selection(**kwargs)
    if pmc_system == "real_page":
        return load_realpage_properties_for_selection(**kwargs)
    if pmc_system == "appfolio":
        return load_appfolio_properties_for_selection(**kwargs)
    return load_properties_for_selection(**kwargs)

# ─── Branded header ──────────────────────────────────────────────────
if _PS_LOGO_DARK_URI:
    st.markdown(
        f'<div style="display:flex;align-items:center;gap:14px;padding:4px 0 8px 0">'
        f'<img src="{_PS_LOGO_DARK_URI}" height="28" alt="PetScreening">'
        f'<div style="height:28px;width:1px;background:#D3CEBD"></div>'
        f'<span style="font-family:Poppins,Arial,sans-serif;font-size:22px;font-weight:600;'
        f'color:#1F2257;letter-spacing:-0.3px">Value Report</span>'
        f'</div>'
        f'<p style="color:#636569;font-size:13px;margin:0 0 8px 0;font-family:Poppins,Arial,sans-serif">'
        f'Fetch rent roll data · Select charge codes · Visualize collection trends</p>',
        unsafe_allow_html=True,
    )
else:
    st.title("Value Report")
    st.markdown("Fetch rent roll data, select fee charge codes, and visualize collection trends over time.")

# ─── Persistent selection banner ────────────────────────────────────
# Shows the currently selected parent company / property at the top
# so users always know what data they're viewing.
_sel_label = st.session_state.get("selection_label", "")
_sel_system = st.session_state.get("pmc_system", "yardi")
if _sel_label:
    _sys_badge = {"all": "All Systems", "yardi": "Yardi", "entrata": "Entrata", "real_page": "RealPage", "appfolio": "AppFolio"}.get(_sel_system, "Yardi")
    _badge_color = {"all": "#1F2257", "yardi": "#7D9BC1", "entrata": "#677848", "real_page": "#8F6A3E", "appfolio": "#5B7C99"}.get(_sel_system, "#7D9BC1")
    st.markdown(
        f'<div style="background:#F9F4E6;border-left:4px solid {_badge_color};padding:8px 16px;'
        f'border-radius:0 6px 6px 0;margin:0 0 12px 0;display:flex;align-items:center;gap:10px">'
        f'<span style="background:{_badge_color};color:white;font-size:11px;font-weight:600;'
        f'padding:2px 8px;border-radius:4px;font-family:Poppins,Arial,sans-serif">{_sys_badge}</span>'
        f'<span style="font-family:Poppins,Arial,sans-serif;font-size:15px;font-weight:600;'
        f'color:#1F2257">{_sel_label}</span>'
        f'</div>',
        unsafe_allow_html=True,
    )

# ─── Early session state (needed before sidebar renders) ─────────────
if "pmc_system" not in st.session_state:
    st.session_state.pmc_system = "yardi"

# ─── Sidebar: Selection ──────────────────────────────────────────────
with st.sidebar:
    if _PS_LOGO_WHITE_URI:
        st.markdown(
            f'<div style="background:#1F2257;padding:14px 18px;border-radius:8px;'
            f'text-align:center;margin-bottom:16px">'
            f'<img src="{_PS_LOGO_WHITE_URI}" height="20" alt="PetScreening">'
            f'</div>',
            unsafe_allow_html=True,
        )

    # ── PMC System selector ──────────────────────────────────────────
    st.header("PMC System")
    # 4-way PMC switch. Internal session value is the canonical lower-case
    # string used as `pmc_system` everywhere downstream and as
    # `du.unit_source` / `m.property_source_name` in SQL substitution.
    #   Yardi    -> 'yardi'
    #   Entrata  -> 'entrata'
    #   RealPage -> 'real_page'   <-- with underscore, matches D_PROPERTIES
    #   AppFolio -> 'appfolio'
    # AppFolio is hidden for now: live API access is blocked by MFA on the
    # account and the Snowflake-staged data is not trusted. The provider
    # code (fetch_appfolio_for_properties etc.) stays as a placeholder —
    # re-add "AppFolio"/"appfolio" here once API access is sorted.
    _pmc_labels  = ["All Systems", "Yardi", "Entrata", "RealPage"]
    _pmc_values  = ["all", "yardi", "entrata", "real_page"]
    _curr_idx = _pmc_values.index(st.session_state.pmc_system) if st.session_state.pmc_system in _pmc_values else 0
    _pmc_choice = st.radio(
        "Data Source:",
        _pmc_labels,
        index=_curr_idx,
        help=(
            "Select the property management system to fetch charge data from. "
            "All Systems combines every source a parent company uses (e.g. a "
            "large parent running Yardi, Entrata AND RealPage properties) "
            "into one dataset, one report, and one set of CSVs."
        ),
        horizontal=True,
    )
    _pmc_system = _pmc_values[_pmc_labels.index(_pmc_choice)]
    # If user switches PMC system, clear stale data
    if _pmc_system != st.session_state.pmc_system:
        st.session_state.pmc_system = _pmc_system
        st.session_state.all_charges_df = None
        st.session_state.ar_charges_df = None
        st.session_state.raw_lease_arrays_df = None
        st.session_state.fetch_log = None
        st.session_state.chart_data = None
        st.session_state.selection_label = ""
        st.session_state.property_ids = []
        for _sk in list(st.session_state.keys()):
            if (_sk.startswith("missing_rent_") and isinstance(st.session_state[_sk], dict)) or _sk in ("export_html", "exec_html", "exec_pdf", "exec_missing_csv", "exec_suspected_csv"):
                del st.session_state[_sk]

    _is_entrata  = _pmc_system == "entrata"
    _is_realpage = _pmc_system in ("real_page", "all")
    _is_appfolio = _pmc_system == "appfolio"
    _system_label = {"all": "All Systems", "yardi": "Yardi", "entrata": "Entrata", "real_page": "RealPage", "appfolio": "AppFolio"}[_pmc_system]

    st.divider()
    st.header("Select Properties")

    search_by = st.radio(
        "Search by:",
        ["Parent Company Name", "Parent Company Ancestry ID", "Property Name / ID"],
        index=0,
    )

    selected_parent = None
    selected_property_id = None
    selected_ancestry_id = None

    if search_by == "Parent Company Name":
        parents = _load_parent_companies_for(_pmc_system)
        parent_options = {
            f"{r['PARENT_COMPANY_NAME']} ({r['TOTAL_PROPS']} props)": r
            for r in parents
        }
        choice = st.selectbox("Parent Company:", [""] + list(parent_options.keys()))
        if choice:
            row = parent_options[choice]
            selected_parent = row['PARENT_COMPANY_NAME']
            st.caption(f"Ancestry ID: `{row['ANCESTRY_ID']}`  ·  "
                        f"{row['API_PROPS']} of {row['TOTAL_PROPS']} have {_system_label} API access")

    elif search_by == "Parent Company Ancestry ID":
        parents = _load_parent_companies_for(_pmc_system)
        ancestry_options = {}
        for r in parents:
            aid = r.get('ANCESTRY_ID') or ''
            if aid:
                label = f"{aid} — {r['PARENT_COMPANY_NAME']} ({r['TOTAL_PROPS']} props)"
                ancestry_options[label] = r
        choice = st.selectbox(
            "Ancestry ID:",
            [""] + sorted(ancestry_options.keys()),
            help="parent_company_ancestry_id from PROD.COMMON.D_PROPERTIES",
        )
        if choice:
            row = ancestry_options[choice]
            selected_ancestry_id = str(row['ANCESTRY_ID'])
            st.caption(f"Parent Company: `{row['PARENT_COMPANY_NAME']}`  ·  "
                        f"{row['API_PROPS']} of {row['TOTAL_PROPS']} have {_system_label} API access")

    else:
        all_props = _load_all_properties_for(_pmc_system)
        prop_options = {
            f"{r['PROPERTY_NAME']} (ID: {r['PROPERTY_ID']})": r['PROPERTY_ID']
            for r in all_props
        }
        choice = st.selectbox("Property:", [""] + list(prop_options.keys()))
        if choice:
            selected_property_id = prop_options[choice]

        # Multi-ID input: paste comma-separated or one-per-line from CSV
        _multi_ids_raw = st.text_area(
            "Or paste Property IDs (comma or newline separated):",
            height=68,
            placeholder="e.g. 12345, 67890, 11111  or one per line",
            key="multi_property_ids_input",
        )
        # Optional custom label for the report (e.g. asset owner name)
        _multi_label_input = st.text_input(
            "Report label (optional):",
            placeholder="e.g. TruAmerica, AEW Capital — leave blank to use property count",
            key="multi_property_label_input",
            help="When pasting property IDs, give the report a custom title "
                 "(asset owner, fund, custom group). Used as the PDF title and "
                 "download filename. Leave blank to default to '<N> Properties'.",
        )
        _multi_ids_parsed = []
        if _multi_ids_raw and _multi_ids_raw.strip():
            import re
            _tokens = re.split(r'[,\n\r\t;]+', _multi_ids_raw)
            for _tok in _tokens:
                _tok = _tok.strip()
                if _tok and _tok.isdigit():
                    _multi_ids_parsed.append(int(_tok))
            if _multi_ids_parsed:
                _detected_caption = f"{len(_multi_ids_parsed)} property IDs detected"
                if _multi_label_input and _multi_label_input.strip():
                    _detected_caption += f"  ·  label: “{_multi_label_input.strip()}”"
                st.caption(_detected_caption)

    # Display window is now derived from the data (no slider).
    # Set a large default; the actual window will be clamped to the
    # earliest charge date once data is loaded.
    lookback_months = 120  # 10 years — effectively "all data"

    st.divider()

    # ── Total Portfolio Units (for value extrapolation in PDF) ──
    _total_portfolio_units = st.number_input(
        "Total Portfolio Units (optional)",
        min_value=0, value=st.session_state.get("_total_portfolio_units", 0), step=100,
        help="Enter the total units across the full portfolio for value extrapolation in the PDF report."
    )
    st.session_state["_total_portfolio_units"] = _total_portfolio_units

    st.divider()

    # ── Check for saved exports ──
    _auto_dir = _auto_export_dir()
    _saved_files = []
    if os.path.isdir(_auto_dir):
        _saved_files = sorted(
            [f for f in os.listdir(_auto_dir) if f.startswith("charges_") and f.endswith(".csv")],
            reverse=True,
        )

    # ── View mode: clean client-facing view vs developer view ───────
    st.toggle(
        "Developer view",
        key="dev_view",
        value=False,
        help=(
            "Show validation and debug widgets: raw data exports, "
            "scheduled-vs-actual AR reconciliation, detailed breakdown "
            "tables, and the property data explorer. Leave off for a "
            "clean view when presenting or for non-technical users."
        ),
    )

    # ── Fetch cache controls (all systems) ──────────────────────────
    with st.expander("Data reuse (cache)"):
        st.toggle(
            "Reuse recent fetches",
            key="use_fetch_cache",
            value=True,
            help=(
                "Reuses per-property charge data fetched within the max age "
                "below instead of re-hitting the Yardi/Entrata/RealPage APIs. "
                "Charge data changes on a monthly cadence, so a re-pull within "
                "a few days rarely adds anything — and skipping it avoids "
                "burning shared API rate limits. Turn off (or lower the max "
                "age) when you need to see changes made in the PMS today; "
                "fresh pulls always update the cache either way."
            ),
        )
        st.selectbox(
            "Max age (days)",
            options=[1, 3, 7, 14, 30],
            index=2,
            key="fetch_cache_ttl_days",
        )
        try:
            from fetch_cache import cache_stats, clear_cache
            _fc_stats = cache_stats()
            _fc_backend = _fc_stats.get("backend", "local")
            if _fc_backend == "snowflake":
                st.caption(
                    f"Backend: Snowflake `{_fc_stats.get('location', '')}` — "
                    f"{_fc_stats.get('total_entries', 0):,} properties, "
                    f"{_fc_stats.get('total_records', 0):,} rows (append-only; "
                    f"doubles as our own fetch-history record). Toggle off "
                    f"'Reuse recent fetches' to bypass."
                )
            elif _fc_stats.get("total_entries"):
                st.caption(
                    f"Local cache: {_fc_stats['total_entries']:,} properties "
                    f"({_fc_stats.get('total_bytes', 0) / 1e6:.1f} MB)"
                )
                if st.button("Clear local cache", use_container_width=True):
                    _n = clear_cache()
                    st.success(f"Removed {_n} cached properties.")
        except Exception:
            pass

    # RealPage: live API, current-state only. No mode toggle by design \u2014
    # the 2026-07 accuracy audit (docs/realpage-accuracy-audit.md) showed
    # the API cannot return history (`postmonth` ignored, Former residents
    # unreachable) and Snowflake staging has its own gaps, so RealPage data
    # is presented as what it truly is: today's snapshot. Missing/suspected
    # reports and current revenue are accurate; before/after lift is not
    # attributed until RealPage authorizes the `getresidentledger` endpoint.
    if _is_realpage:
        st.caption(
            "RealPage is a live current-state snapshot: current revenue "
            "and missing/suspected reports only \u2014 no before/after lift "
            "(resident-ledger access pending with RealPage)."
        )

    # AppFolio starts Snowflake-backed; keep the live API path visible but
    # disabled until Developer Space/MFA access is available.
    if _is_appfolio:
        st.toggle(
            "\U0001F534 Use Live AppFolio API (coming soon — currently Snowflake only)",
            key="appfolio_use_live_api",
            disabled=True,
            help=(
                "AppFolio live API is not enabled yet. For now this uses the "
                "Snowflake-backed getrecurringcharges/gettenants/getunits tables. "
                "Enable this later after MFA/API access is available."
            ),
        )

    fetch_btn = st.button(f"Fetch {_system_label} Data", type="primary", use_container_width=True)

    if _saved_files:
        _show_saved = st.checkbox("Load from saved export", key="show_saved_exports")
        if _show_saved:
            _selected_export = st.selectbox(
                "Select a saved file:",
                options=_saved_files,
                key="load_export_select",
                label_visibility="collapsed",
            )
            if st.button("Load", key="load_export_btn", use_container_width=True):
                _load_path = os.path.join(_auto_dir, _selected_export)
                _loaded_df = pd.read_csv(_load_path, low_memory=False)

                # ── Fix CSV type coercion ──
                # Pandas reads integer columns with NaN as float (e.g. 12345 → 12345.0).
                # Downstream matching compares string IDs ("12345" vs "12345.0") and fails.
                # Normalize ID-like columns back to clean strings.
                for _id_col in ['property_id', 'tenant_code', 'lease_id', 'unit_code']:
                    if _id_col in _loaded_df.columns:
                        _loaded_df[_id_col] = (
                            _loaded_df[_id_col]
                            .astype(str)
                            .str.replace(r'\.0$', '', regex=True)
                            .replace({'nan': '', 'None': '', 'none': ''})
                        )

                st.session_state.all_charges_df = _loaded_df
                # Try to load matching fetch log file
                _log_name = _selected_export.replace("charges_", "fetch_log_", 1)
                _log_path = os.path.join(_auto_dir, _log_name)
                if os.path.isfile(_log_path):
                    st.session_state.fetch_log = pd.read_csv(_log_path, low_memory=False)
                else:
                    st.session_state.fetch_log = None
                # Try to load matching AR file
                _ar_name = _selected_export.replace("charges_", "ar_charges_", 1)
                _ar_path = os.path.join(_auto_dir, _ar_name)
                if os.path.isfile(_ar_path):
                    _ar_df = pd.read_csv(_ar_path, low_memory=False)
                    for _id_col in ['property_id', 'tenant_code', 'lease_id', 'unit_code']:
                        if _id_col in _ar_df.columns:
                            _ar_df[_id_col] = (
                                _ar_df[_id_col]
                                .astype(str)
                                .str.replace(r'\.0$', '', regex=True)
                                .replace({'nan': '', 'None': '', 'none': ''})
                            )
                    st.session_state.ar_charges_df = _ar_df
                else:
                    st.session_state.ar_charges_df = None
                st.session_state.raw_lease_arrays_df = None
                # Recover property_ids from the loaded data
                if 'property_id' in _loaded_df.columns:
                    st.session_state.property_ids = _loaded_df['property_id'].dropna().unique().tolist()
                # Recover selection_label from parent_company column if not already set
                if not st.session_state.get("selection_label") and 'parent_company' in _loaded_df.columns:
                    _pc_vals = _loaded_df['parent_company'].dropna().unique()
                    if len(_pc_vals) > 0:
                        st.session_state.selection_label = str(_pc_vals[0]).strip()
                st.success(f"Loaded **{len(_loaded_df):,}** charges from `{_selected_export}`")
                st.rerun()

# ─── Session state ───────────────────────────────────────────────────
if "all_charges_df" not in st.session_state:
    st.session_state.all_charges_df = None
if "ar_charges_df" not in st.session_state:
    st.session_state.ar_charges_df = None
if "raw_lease_arrays_df" not in st.session_state:
    st.session_state.raw_lease_arrays_df = None
if "fetch_log" not in st.session_state:
    st.session_state.fetch_log = None
if "selection_label" not in st.session_state:
    st.session_state.selection_label = ""
if "property_ids" not in st.session_state:
    st.session_state.property_ids = []
if "total_parent_props" not in st.session_state:
    st.session_state.total_parent_props = 0
if "api_props_count" not in st.session_state:
    st.session_state.api_props_count = 0
if "chart_data" not in st.session_state:
    st.session_state.chart_data = None

# ─── Fetch data ──────────────────────────────────────────────────────
if fetch_btn:
    # Support multi-ID paste input
    _use_multi_ids = bool(_multi_ids_parsed) if '_multi_ids_parsed' in dir() else False
    if not selected_parent and not selected_property_id and not selected_ancestry_id and not _use_multi_ids:
        st.warning("Please select a parent company, ancestry ID, or property first.")
    else:
        if _use_multi_ids:
            # Load all integrated properties, then filter to the pasted IDs
            _all_integrated = _load_properties_for_selection_for(_pmc_system)
            _multi_id_set = set(_multi_ids_parsed)
            properties = [p for p in _all_integrated if int(p['PROPERTY_ID']) in _multi_id_set]
            if not properties:
                # Show which IDs weren't found
                _found_ids = {int(p['PROPERTY_ID']) for p in _all_integrated}
                _missing = [str(pid) for pid in _multi_ids_parsed if pid not in _found_ids]
                st.warning(f"None of the pasted IDs have active {_system_label} integrations. "
                           f"Missing/inactive: {', '.join(_missing[:10])}")
        else:
            # Route to correct property loader based on PMC system
            properties = _load_properties_for_selection_for(
                _pmc_system,
                parent_company_name=selected_parent,
                property_id=selected_property_id,
                ancestry_id=selected_ancestry_id,
            )

        if not properties:
            if not _use_multi_ids:  # multi-ID already showed its own warning
                st.error(f"No {_system_label} properties with active integrations found for that selection.")
        else:
            if _use_multi_ids:
                _custom_label = (_multi_label_input or "").strip() if '_multi_label_input' in dir() else ""
                label = _custom_label if _custom_label else f"{len(properties)} Properties"
            else:
                label = selected_parent or (f"Ancestry {selected_ancestry_id}" if selected_ancestry_id else f"Property {selected_property_id}")
            st.session_state.selection_label = label
            st.session_state.property_ids = [p['PROPERTY_ID'] for p in properties]

            # Store selection context for downstream queries (e.g., PM email lookup)
            # Always try to resolve the ancestry_id, even when searching by name
            _resolved_ancestry_id = selected_ancestry_id
            if not _resolved_ancestry_id and (selected_parent or selected_ancestry_id):
                try:
                    _pc_list = _load_parent_companies_for(_pmc_system)
                    for r in _pc_list:
                        if (selected_parent and r['PARENT_COMPANY_NAME'] == selected_parent) or \
                           (selected_ancestry_id and str(r.get('ANCESTRY_ID', '')) == selected_ancestry_id):
                            _resolved_ancestry_id = str(r.get('ANCESTRY_ID', ''))
                            break
                except Exception:
                    pass
            st.session_state.selected_ancestry_id = _resolved_ancestry_id
            st.session_state.selected_parent_company = selected_parent

            # Get total property count for context
            total_count = None
            if selected_parent or selected_ancestry_id:
                _pc_list2 = _load_parent_companies_for(_pmc_system)
                for r in _pc_list2:
                    if (selected_parent and r['PARENT_COMPANY_NAME'] == selected_parent) or \
                       (selected_ancestry_id and str(r.get('ANCESTRY_ID', '')) == selected_ancestry_id):
                        total_count = r['TOTAL_PROPS']
                        break

            api_count = len(properties)
            st.session_state.total_parent_props = total_count if total_count else api_count
            st.session_state.api_props_count = api_count
            if total_count and total_count > api_count:
                st.info(
                    f"**{label}** has **{total_count}** total properties in `d_properties`.  \n"
                    f"**{api_count}** have active {_system_label} API integrations (credentials to fetch).  \n"
                    f"**{total_count - api_count}** properties are missing integration records and will be skipped."
                )
            else:
                st.info(f"Found **{api_count}** {_system_label}-integrated properties for **{label}**.")

            # Clear ALL stale data from previous selection
            st.session_state.chart_data = None
            st.session_state.pm_emails_cache = None
            st.session_state.all_charges_df = None
            st.session_state.ar_charges_df = None
            st.session_state.raw_lease_arrays_df = None
            st.session_state.fetch_log = None
            # Clear cached missing rent, suspected, and charge type data
            for _stale_key in list(st.session_state.keys()):
                if (_stale_key.startswith("missing_rent_") or _stale_key.startswith("suspected_") or
                    _stale_key in ("export_html", "exec_html", "exec_pdf", "exec_missing_csv", "exec_suspected_csv", "charge_type_overrides")):
                    del st.session_state[_stale_key]
            st.markdown(f"Fetching {_system_label} data (full history — display window: **{lookback_months} months**)...")
            progress_bar = st.progress(0)
            status_text = st.empty()

            # Route to correct API fetch — wrapped in the per-property cache
            # so repeat pulls within the TTL skip the APIs entirely. In
            # "All Systems" mode, properties are grouped by source system and
            # each group goes through its own fetcher; results are combined
            # into one dataset.
            from fetch_cache import fetch_with_cache

            def _fetch_fn_for(_sys):
                """(fetch_fn, cache_mode) for one concrete source system."""
                if _sys == "entrata":
                    return (lambda props: fetch_entrata_for_properties(props, progress_bar, status_text, lookback_months)), ""
                if _sys == "real_page":
                    # Live current-state only (see sidebar note / audit doc).
                    # Returns the 5-tuple form so the live resident roster is
                    # persisted in the cache — cache-hit runs then filter the
                    # missing/suspected reports identically to fresh runs.
                    import realpage_live_api as _rp_api

                    def _rp_fetch(props):
                        _c, _l = _rp_api.fetch_realpage_live(
                            props, progress_bar, status_text, lookback_months,
                            junk_emails={e.strip().strip("'").lower() for e in JUNK_EMAILS.split(",")},
                        )
                        _extras = {
                            _pid: {"residents": _res}
                            for _pid, _res in _rp_api.LAST_RESIDENTS.items()
                        }
                        return _c, _l, [], [], _extras

                    return _rp_fetch, "live"
                if _sys == "appfolio":
                    if st.session_state.get("appfolio_use_live_api"):
                        st.warning("AppFolio live API is not enabled yet — using Snowflake data for now.")
                    return (lambda props: fetch_appfolio_for_properties(props, progress_bar, status_text, lookback_months)), ""
                return (lambda props: fetch_rentroll_for_properties(props, progress_bar, status_text, lookback_months)), ""

            if _pmc_system == "all":
                _sys_groups = {}
                for _p in properties:
                    _sys_groups.setdefault(_p.get("_PMC_SYSTEM", "yardi"), []).append(_p)
            else:
                _sys_groups = {_pmc_system: properties}

            # pid → source system, used by the report builders (missing /
            # suspected SQL scoping) when systems are combined.
            st.session_state["property_system_by_pid"] = {
                str(_p.get("PROPERTY_ID")): _p.get("_PMC_SYSTEM", _pmc_system)
                for _p in properties
            }

            all_charges, fetch_log, ar_charges, raw_lease_arrays = [], [], [], []
            _cache_hits = _cache_misses = 0
            _cache_oldest = None
            for _sys, _sys_props in _sys_groups.items():
                if _pmc_system == "all":
                    status_text.text(f"Fetching {_sys} ({len(_sys_props)} properties)...")
                _fn, _mode = _fetch_fn_for(_sys)
                _c, _l, _a, _r, _ci = fetch_with_cache(
                    _sys,
                    _sys_props,
                    _fn,
                    ttl_days=float(st.session_state.get("fetch_cache_ttl_days", 7)),
                    force_refresh=not st.session_state.get("use_fetch_cache", True),
                    mode=_mode,
                )
                all_charges.extend(_c)
                _sys_label_map = {"yardi": "Yardi", "entrata": "Entrata", "real_page": "RealPage", "appfolio": "AppFolio"}
                for _lr in _l:
                    _lr["system"] = _sys_label_map.get(_sys, _sys)
                fetch_log.extend(_l)
                ar_charges.extend(_a)
                raw_lease_arrays.extend(_r)
                if _sys == "real_page":
                    # Restore live resident rosters from cached payloads so
                    # report filtering behaves the same on cache hits.
                    import realpage_live_api as _rp_api2
                    for _pid, _ex in (_ci.get("extras_by_pid") or {}).items():
                        if _ex and _ex.get("residents") is not None:
                            _rp_api2.LAST_RESIDENTS[str(_pid)] = _ex["residents"]
                _cache_hits += _ci["hits"]
                _cache_misses += _ci["misses"]
                if _ci["oldest"] is not None and (_cache_oldest is None or _ci["oldest"] < _cache_oldest):
                    _cache_oldest = _ci["oldest"]

            if len(_sys_groups) > 1:
                _grp_summary = ", ".join(f"{s}: {len(p)}" for s, p in sorted(_sys_groups.items()))
                st.caption(f"Combined {len(_sys_groups)} source systems — {_grp_summary}")
            if _cache_hits:
                _oldest_txt = f" (oldest from {_cache_oldest.strftime('%b %d %H:%M')})" if _cache_oldest else ""
                st.caption(
                    f"Reused cached data for {_cache_hits} of "
                    f"{len(properties)} properties{_oldest_txt}; fetched "
                    f"{_cache_misses} fresh. Disable in the sidebar "
                    f"cache panel to force a full re-pull."
                )

            success_count = sum(1 for l in fetch_log if l['status'].startswith('Success'))
            error_count = sum(1 for l in fetch_log if l['status'].startswith('Error'))
            warn_count = sum(1 for l in fetch_log if l['status'].startswith('Warning'))

            if all_charges:
                st.session_state.all_charges_df = pd.DataFrame(all_charges)
                st.session_state.fetch_log = pd.DataFrame(fetch_log)
                if ar_charges:
                    st.session_state.ar_charges_df = pd.DataFrame(ar_charges)
                else:
                    st.session_state.ar_charges_df = None
                if raw_lease_arrays:
                    st.session_state.raw_lease_arrays_df = pd.DataFrame(raw_lease_arrays)
                else:
                    st.session_state.raw_lease_arrays_df = None

                # ── Auto-export to disk (survives crashes / memory kills) ──
                # In Streamlit-in-Snowflake the app stage is read-only, so
                # this falls back to the temp dir (and the Snowflake fetch
                # cache is the real persistence there anyway).
                try:
                    _auto_dir = _auto_export_dir()
                    os.makedirs(_auto_dir, exist_ok=True)
                    _ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                    _label_safe = st.session_state.get("selection_label", "export").replace(" ", "_")[:60]
                    _auto_path = os.path.join(_auto_dir, f"charges_{_label_safe}_{_ts}.csv")
                    st.session_state.all_charges_df.to_csv(_auto_path, index=False)
                    _auto_msg = f"Auto-saved {len(all_charges):,} charges → `{os.path.basename(_auto_path)}`"
                    if ar_charges and st.session_state.ar_charges_df is not None:
                        _ar_path = os.path.join(_auto_dir, f"ar_charges_{_label_safe}_{_ts}.csv")
                        st.session_state.ar_charges_df.to_csv(_ar_path, index=False)
                        _auto_msg += f" + AR transactions"
                    st.info(f"💾 {_auto_msg} (in `{os.path.basename(_auto_dir)}/` folder)")
                except Exception as _auto_err:  # noqa: BLE001 — never break a fetch over a local save
                    st.caption(f"Auto-export skipped: {str(_auto_err)[:100]}")

                # Build summary message
                summary_parts = [f"Fetched **{len(all_charges):,}** charges across **{success_count}** properties"]
                if error_count:
                    summary_parts.append(f"**{error_count}** failed (timeout/HTTP error)")
                if warn_count:
                    summary_parts.append(f"**{warn_count}** had no data or access issues")
                st.success("  ·  ".join(summary_parts))
            else:
                st.session_state.all_charges_df = None
                st.session_state.ar_charges_df = None
                st.session_state.raw_lease_arrays_df = None
                st.session_state.fetch_log = pd.DataFrame(fetch_log)
                st.warning("No charge data returned from any property.")

# ─── Show fetch results ──────────────────────────────────────────────
if st.session_state.fetch_log is not None:
    with st.expander("Fetch Results (click to expand)", expanded=False):
        _fl_df = st.session_state.fetch_log
        if _fl_df is not None and "system" in getattr(_fl_df, "columns", []):
            _fl_cols = ["system"] + [c for c in _fl_df.columns if c != "system"]
            _fl_df = _fl_df[_fl_cols]
        _render_table(_fl_df, height=300)

# ─── Charge code selection ───────────────────────────────────────────
if st.session_state.all_charges_df is not None:
    df = st.session_state.all_charges_df

    st.divider()
    st.header("Select Fee Charge Codes")
    st.markdown("Below are all unique charge codes found. **Select the ones you believe are pet-related fees / pet rent.**")

    # Show charge code summary
    code_counts = df['charge_code'].value_counts().reset_index()
    code_counts.columns = ['Charge Code', 'Count']

    # Pre-select codes that look pet-related
    pet_keywords = ['pet', 'animal', 'petnr', 'concpet']
    default_selected = [
        code for code in code_counts['Charge Code'].tolist()
        if any(kw in code.lower() for kw in pet_keywords)
    ]

    col1, col2 = st.columns([1, 2])

    with col1:
        st.markdown("**Charge Code Summary:**")
        _render_table(code_counts, height=400)

    with col2:
        selected_codes = st.multiselect(
            "Select charge codes to analyze:",
            options=code_counts['Charge Code'].tolist(),
            default=default_selected,
            help="Pick the charge codes that represent pet fees, pet rent, etc."
        )

        if selected_codes:
            filtered = df[df['charge_code'].isin(selected_codes)]
            st.metric("Matching Charge Lines", f"{len(filtered):,}",
                      help="Unique charge line items from the API matching your selected codes. Each line has a from/to date range — it contributes to every month it was active in the charts below.")

            # Summary of selected codes: count + total amount per code
            filtered_with_amt = filtered.copy()
            filtered_with_amt['amount'] = pd.to_numeric(filtered_with_amt['charge_amount'], errors='coerce').fillna(0)
            code_summary = (
                filtered_with_amt.groupby('charge_code')
                .agg(count=('charge_code', 'size'), total_amount=('amount', 'sum'))
                .sort_values('count', ascending=False)
                .reset_index()
            )
            code_summary.columns = ['Charge Code', 'Count', 'Total Amount']
            code_summary['Total Amount'] = code_summary['Total Amount'].apply(lambda x: f"${x:,.2f}")
            _render_table(code_summary)

            # Sample rows: take up to 3 rows from each selected code so all codes are represented
            sample_cols = ['property_name', 'tenant_code', 'charge_code', 'charge_amount',
                           'charge_from_date', 'charge_to_date', 'tenant_status']
            sample_rows = filtered.groupby('charge_code').head(3).sort_values('charge_code')
            with st.expander(f"Preview rows ({len(sample_rows)} shown, up to 3 per code)"):
                _render_table(sample_rows[sample_cols])

    if not selected_codes:
        st.info("Select at least one charge code above to continue.")
    else:
        # ─── Charge Type Classification (Recurring vs One-Time) ──────
        with st.expander("Charge type classification — recurring vs one-time", expanded=False):
            st.markdown(
                "Each charge code is auto-classified as **recurring** (monthly pet rent) or "
                "**one-time** (pet deposit/fee). One-time charges only count in their start month "
                "on the revenue charts — they don't spread across months.\n\n"
                "**Override** any classification below if the auto-detection is wrong."
            )

            # Auto-detect classification for selected codes
            _has_freq = 'frequency' in df.columns
            _auto_class = {}  # {charge_code: "recurring" | "onetime"}
            _class_reason = {}  # {charge_code: explanation}

            _pet_df = df[df['charge_code'].isin(selected_codes)].copy()
            _pet_df['_amt'] = pd.to_numeric(_pet_df['charge_amount'], errors='coerce').fillna(0)

            for code in selected_codes:
                code_rows = _pet_df[_pet_df['charge_code'] == code]
                if _has_freq:
                    freqs = code_rows['frequency'].dropna().str.strip().str.lower()
                    ot = (freqs == 'one-time').sum()
                    mo = freqs.isin(['monthly', 'recurring']).sum()
                    if ot > mo:
                        _auto_class[code] = "onetime"
                        _class_reason[code] = f"Entrata frequency field: {ot} one-time vs {mo} recurring"
                    else:
                        _auto_class[code] = "recurring"
                        _class_reason[code] = f"Entrata frequency field: {mo} recurring vs {ot} one-time"
                else:
                    spans = []
                    for _, row in code_rows.iterrows():
                        fd = parse_date(row.get('charge_from_date'))
                        td = parse_date(row.get('charge_to_date'))
                        if fd and td:
                            try:
                                spans.append((td - fd).days)
                            except (TypeError, AttributeError):
                                pass
                    med = float(np.median(spans)) if spans else None
                    if med is None:
                        _auto_class[code] = "recurring"
                        _class_reason[code] = "No date spans available — defaulting to recurring"
                    elif med > 60:
                        _auto_class[code] = "recurring"
                        _class_reason[code] = f"Median date span: {med:.0f} days (>60 = recurring)"
                    else:
                        _auto_class[code] = "onetime"
                        _class_reason[code] = f"Median date span: {med:.0f} days (≤60 = one-time)"

            # Build UI: one row per charge code with override dropdown
            _override_key = "charge_type_overrides"
            if _override_key not in st.session_state:
                st.session_state[_override_key] = {}

            _class_options = ["Auto-detect", "Recurring", "One-Time"]
            _override_cols = st.columns([3, 2, 2, 3])
            _override_cols[0].markdown("**Charge Code**")
            _override_cols[1].markdown("**Auto-Detected**")
            _override_cols[2].markdown("**Override**")
            _override_cols[3].markdown("**Reason**")

            for code in selected_codes:
                auto = _auto_class.get(code, "recurring")
                auto_label = "Recurring" if auto == "recurring" else "One-Time"
                reason = _class_reason.get(code, "")
                avg_amt = _pet_df[_pet_df['charge_code'] == code]['_amt'].mean()

                c1, c2, c3, c4 = st.columns([3, 2, 2, 3])
                c1.markdown(f"`{code}` (avg ${avg_amt:,.0f})")
                c2.markdown(f"{'🔄 Recurring' if auto == 'recurring' else '1️⃣ One-Time'}")
                override = c3.selectbox(
                    "Override",
                    options=_class_options,
                    index=0,
                    key=f"ct_override_{code}",
                    label_visibility="collapsed",
                )
                c4.caption(reason)

                if override == "Recurring":
                    st.session_state[_override_key][code] = "recurring"
                elif override == "One-Time":
                    st.session_state[_override_key][code] = "onetime"
                elif code in st.session_state[_override_key]:
                    del st.session_state[_override_key][code]

            if st.session_state.get(_override_key):
                _n_overrides = len(st.session_state[_override_key])
                st.info(f"{_n_overrides} manual override{'s' if _n_overrides > 1 else ''} active. "
                        f"These override the auto-detection for revenue charts and lift calculations.")

        st.divider()

        # ─── Raw Data Exports (for Snowflake validation) ─────────────
        # Developer-view only — hidden in the default client-facing view
        if st.session_state.get("dev_view", False):
            with st.expander("Raw Data Exports — Download API data for Snowflake validation", expanded=False):
                st.markdown(
                    "Download the raw data that the app uses so you can upload it to "
                    "Snowflake and independently recreate/validate the numbers with SQL.  \n"
                    "**Date columns are auto-fixed** (century correction applied) — upload directly, no manual cleanup needed."
                )

                pmc_sys = st.session_state.get("pmc_system", "yardi")
                props_str_export = ", ".join(str(int(pid)) for pid in st.session_state.get("property_ids", []))

                # ── Fix dates before export ──────────────────────────────
                # Yardi API returns dates like "07/01/0025" instead of "07/01/2025".
                # Fix all date columns so the CSV can be uploaded to Snowflake directly.
                _date_cols = ['launch_date', 'lease_from', 'lease_to', 'move_in', 'move_out',
                              'charge_from_date', 'charge_to_date']

                def _fix_dates_for_export(export_df):
                    """Fix century-shifted dates (0025 → 2025) in all date columns."""
                    fixed = export_df.copy()
                    for col in _date_cols:
                        if col not in fixed.columns:
                            continue
                        def _fix_date_val(val):
                            if val is None or (isinstance(val, float) and pd.isna(val)):
                                return val
                            s = str(val).strip()
                            if not s or s.lower() in ('nan', 'nat', 'none', ''):
                                return None
                            # Try parsing and fix year if < 1000
                            for fmt in ["%m/%d/%Y", "%m-%d-%Y", "%Y-%m-%d"]:
                                try:
                                    dt = datetime.strptime(s[:10], fmt)
                                    if dt.year < 1000:
                                        dt = dt.replace(year=dt.year + 2000)
                                    return dt.strftime("%Y-%m-%d")
                                except (ValueError, TypeError):
                                    continue
                            # If it's already a datetime/Timestamp object
                            try:
                                if hasattr(val, 'year') and val.year < 1000:
                                    val = val.replace(year=val.year + 2000)
                                return str(val)[:10]
                            except Exception:
                                return val
                        fixed[col] = fixed[col].apply(_fix_date_val)
                    return fixed

                exp_c1, exp_c2, exp_c3, exp_c4 = st.columns(4)

                with exp_c1:
                    st.markdown("**1. All API Charges**")
                    st.caption("Every charge line from the API (all tenants, all codes). Dates are auto-fixed.")
                    _export_all = _fix_dates_for_export(df)
                    st.download_button(
                        "Download all_charges.csv",
                        data=_export_all.to_csv(index=False),
                        file_name=f"all_charges_{pmc_sys}_{datetime.now().strftime('%Y%m%d')}.csv",
                        mime="text/csv",
                        key="dl_all_charges",
                    )

                with exp_c2:
                    st.markdown("**2. Pet Charges Only**")
                    st.caption("Filtered to your selected pet charge codes only. Dates are auto-fixed.")
                    _pet_only = df[df['charge_code'].isin(selected_codes)]
                    _export_pet = _fix_dates_for_export(_pet_only)
                    st.download_button(
                        "Download pet_charges.csv",
                        data=_export_pet.to_csv(index=False),
                        file_name=f"pet_charges_{pmc_sys}_{datetime.now().strftime('%Y%m%d')}.csv",
                        mime="text/csv",
                        key="dl_pet_charges",
                    )

                with exp_c3:
                    st.markdown("**3. Paying Tenant Sets**")
                    st.caption("Every (property_id, tenant_code) and (property_id, email) pair identified as paying after unit/lease expansion.")
                    _pay_tc, _pay_em = _build_paying_sets(df, selected_codes)
                    _pay_tc_df = pd.DataFrame(list(_pay_tc), columns=["property_id", "tenant_code"])
                    _pay_em_df = pd.DataFrame(list(_pay_em), columns=["property_id", "email"])
                    _pay_combined = pd.concat([
                        _pay_tc_df.assign(match_type="tenant_code", match_key=_pay_tc_df["tenant_code"]).drop(columns=["tenant_code"]),
                        _pay_em_df.assign(match_type="email", match_key=_pay_em_df["email"]).drop(columns=["email"]),
                    ], ignore_index=True)
                    st.download_button(
                        "Download paying_tenants.csv",
                        data=_pay_combined.to_csv(index=False),
                        file_name=f"paying_tenants_{pmc_sys}_{datetime.now().strftime('%Y%m%d')}.csv",
                        mime="text/csv",
                        key="dl_paying_sets",
                    )

                with exp_c4:
                    st.markdown("**4. PS Profiles + Paying Flag**")
                    st.caption("PetScreening profiles from Snowflake with the paying flag applied — the raw join result before final filtering.")
                    if st.button("Generate Profiles Export", key="gen_profiles_export"):
                        with st.spinner("Querying Snowflake for profiles..."):
                            _conn = get_snowflake_connection()
                            _cur = _conn.cursor(snowflake.connector.DictCursor)
                            _sql = f"""
                            SELECT DISTINCT
                                du.property_id,
                                p.property_name,
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
                                ue.user_profile_url
                            FROM PROD.common.d_units du
                            JOIN PROD.common.d_properties p ON du.property_id = p.property_id
                            JOIN PROD.petscreening.petscreening__user_enriched ue ON ue.unit_id = du.unit_id
                            JOIN PROD.common.f_leases l ON du.unit_key = l.unit_key AND l.user_key = ue.user_key
                            WHERE du.unit_source = '{pmc_sys}'
                              AND du.property_id IN ({props_str_export})
                              AND ue.compliance_status = 'compliant'
                              AND ue.user_pet_type = 'household'
                              AND ue.user_pet_status = 'active'
                              AND ue.user_email IS NOT NULL
                              AND TRIM(ue.user_email) <> ''
                              AND LOWER(TRIM(ue.user_email)) NOT IN ({JUNK_EMAILS})
                            """
                            _cur.execute(_sql)
                            _profiles = pd.DataFrame(_cur.fetchall())
                            if not _profiles.empty:
                                _profiles = _apply_paying_flag(_profiles, _pay_tc, _pay_em)
                                st.session_state["_export_profiles_df"] = _profiles
                                st.success(f"Loaded {len(_profiles):,} profiles. {(_profiles['pet_rent_paid'] == 1).sum():,} paying, {(_profiles['pet_rent_paid'] == 0).sum():,} not paying.")
                            else:
                                st.warning("No profiles found.")

                    if "_export_profiles_df" in st.session_state and st.session_state["_export_profiles_df"] is not None:
                        _prof_df = st.session_state["_export_profiles_df"]
                        st.download_button(
                            "Download profiles_with_flag.csv",
                            data=_prof_df.to_csv(index=False),
                            file_name=f"profiles_with_paying_flag_{pmc_sys}_{datetime.now().strftime('%Y%m%d')}.csv",
                            mime="text/csv",
                            key="dl_profiles_flag",
                        )

                # ── 5. AR Transactions Export (Entrata only) ────────────
                _ar_export_df = st.session_state.get("ar_charges_df")
                if pmc_sys == "entrata" and _ar_export_df is not None and not _ar_export_df.empty:
                    st.markdown("---")
                    exp_ar1, exp_ar2, _ = st.columns([1, 1, 2])
                    with exp_ar1:
                        st.markdown("**5. AR Transactions (Entrata)**")
                        st.caption(
                            "Actual posted AR transactions (pet-related only). "
                            "These are what was *actually billed* vs the scheduled charges above."
                        )
                        st.download_button(
                            "Download ar_transactions.csv",
                            data=_ar_export_df.to_csv(index=False),
                            file_name=f"ar_transactions_entrata_{datetime.now().strftime('%Y%m%d')}.csv",
                            mime="text/csv",
                            key="dl_ar_transactions",
                        )
                    with exp_ar2:
                        st.markdown("**6. Scheduled + AR Flag**")
                        st.caption(
                            "All pet charges with an `ar_match` column: YES if a matching AR transaction "
                            "exists for the same lease + charge code, NO otherwise."
                        )
                        # Build the flagged export
                        _pet_sched = df[df['charge_code'].isin(selected_codes)].copy()
                        _ar_keys = set()
                        if 'lease_id' in _pet_sched.columns:
                            for _, _ar_row in _ar_export_df.iterrows():
                                _ar_keys.add((
                                    str(_ar_row.get('lease_id', '')).strip(),
                                    str(_ar_row.get('charge_code_name', '')).strip().lower(),
                                ))
                            _pet_sched['ar_match'] = _pet_sched.apply(
                                lambda r: 'YES' if (
                                    str(r.get('lease_id', '')).strip(),
                                    str(r.get('charge_code', '')).strip().lower(),
                                ) in _ar_keys else 'NO',
                                axis=1,
                            )
                        else:
                            _pet_sched['ar_match'] = 'N/A'
                        _export_flagged = _fix_dates_for_export(_pet_sched)
                        st.download_button(
                            "Download pet_charges_with_ar_flag.csv",
                            data=_export_flagged.to_csv(index=False),
                            file_name=f"pet_charges_ar_flag_{pmc_sys}_{datetime.now().strftime('%Y%m%d')}.csv",
                            mime="text/csv",
                            key="dl_pet_ar_flag",
                        )

                # ── 7. Raw Lease Arrays Export (Entrata only) ────────────
                _raw_arrays_df = st.session_state.get("raw_lease_arrays_df")
                if pmc_sys == "entrata" and _raw_arrays_df is not None and not _raw_arrays_df.empty:
                    st.markdown("---")
                    _rac1, _rac2, _ = st.columns([1, 1, 2])
                    with _rac1:
                        st.markdown("**7. Raw Lease Arrays (Entrata)**")
                        st.caption(
                            "One row per lease with the raw `scheduledCharges` and `arTransactions` "
                            "JSON arrays exactly as returned by the Entrata API. "
                            "Open the CSV and expand the JSON columns to see every individual "
                            "charge and transaction."
                        )
                        st.download_button(
                            "Download raw_lease_arrays.csv",
                            data=_raw_arrays_df.to_csv(index=False),
                            file_name=f"raw_lease_arrays_entrata_{datetime.now().strftime('%Y%m%d')}.csv",
                            mime="text/csv",
                            key="dl_raw_lease_arrays",
                        )
                    with _rac2:
                        _n_with_sched = (_raw_arrays_df['n_scheduled_charges'] > 0).sum()
                        _n_with_ar = (_raw_arrays_df['n_ar_transactions'] > 0).sum()
                        st.metric("Leases with scheduled charges", f"{_n_with_sched:,}")
                        st.metric("Leases with AR transactions", f"{_n_with_ar:,}")

                st.divider()
                st.markdown("##### Snowflake Upload Instructions")
                st.code(
                    "-- 1. Create a stage and file format\n"
                    "CREATE OR REPLACE FILE FORMAT my_csv_format TYPE = 'CSV' FIELD_OPTIONALLY_ENCLOSED_BY = '\"' SKIP_HEADER = 1;\n"
                    "CREATE OR REPLACE STAGE my_validation_stage FILE_FORMAT = my_csv_format;\n\n"
                    "-- 2. Upload via SnowSQL or Snowsight UI\n"
                    "PUT file:///path/to/all_charges_*.csv @my_validation_stage;\n\n"
                    "-- 3. Create table and load\n"
                    "CREATE OR REPLACE TABLE sandbox.validation.api_charges AS\n"
                    "SELECT $1 as parent_company, $2 as property_id, $3 as property_name,\n"
                    "       $4 as property_code, $5 as launch_date, $6 as unit_code,\n"
                    "       $7 as unit_type, $8 as market_rent, $9 as tenant_code,\n"
                    "       $10 as first_name, $11 as last_name, $12 as tenant_status,\n"
                    "       $13 as lease_from, $14 as lease_to, $15 as move_in,\n"
                    "       $16 as move_out, $17 as email, $18 as charge_code,\n"
                    "       $19 as charge_type, $20 as charge_amount,\n"
                    "       $21 as charge_from_date, $22 as charge_to_date\n"
                    "FROM @my_validation_stage/all_charges_*.csv;\n\n"
                    "-- 4. Validate: count pet charges\n"
                    "SELECT charge_code, COUNT(*) as cnt, SUM(charge_amount::float) as total\n"
                    "FROM sandbox.validation.api_charges\n"
                    "WHERE charge_code ILIKE '%pet%'\n"
                    "GROUP BY 1 ORDER BY 2 DESC;",
                    language="sql",
                )

        st.divider()

        # ═══════════════════════════════════════════════════════════════
        # TABS: Charts  |  Missing Pet Rent Report
        # ═══════════════════════════════════════════════════════════════
        tab_charts, tab_next, tab_report, tab_docs = st.tabs([
            "Fee Collection Charts",
            "Summary & Reports",
            "Missing Pet Rent",
            "Documentation & SQL",
        ])

        # ─── TAB 1: Charts ───────────────────────────────────────────
        with tab_charts:
            analyze_btn = st.button("Analyze & Visualize", type="primary", use_container_width=True, key="analyze_btn")

            # On button click: parse the raw charge data and store it.
            # The time-series aggregation is done BELOW using the live slider
            # value so changing the display window never requires re-fetching.
            if analyze_btn:
                # Clear cached data on new analysis
                for _key in list(st.session_state.keys()):
                    if _key.startswith("missing_rent_") or _key in ("export_html", "exec_html", "exec_pdf", "exec_missing_csv", "exec_suspected_csv"):
                        del st.session_state[_key]

                label = st.session_state.selection_label
                filtered = df[df['charge_code'].isin(selected_codes)].copy()

                # Parse dates & amounts once
                filtered["from_date"] = filtered["charge_from_date"].apply(parse_date)
                filtered["_raw_to_date"] = filtered["charge_to_date"].apply(parse_date)
                filtered["_move_out_dt"] = filtered["move_out"].apply(parse_date)
                filtered["_lease_to_dt"] = filtered["lease_to"].apply(parse_date)
                filtered["amount"] = pd.to_numeric(filtered["charge_amount"], errors="coerce").fillna(0)

                # Effective to_date: coalesce(charge_to_date, move_out, lease_to)
                # For Past tenants with no charge end date, use move_out or lease end
                # so their charges don't count in months after they left.
                def _effective_to_date(row):
                    charge_end = row["_raw_to_date"]
                    if charge_end is not None and not pd.isna(charge_end):
                        return charge_end
                    # Current tenants: no coalescing — they're still active
                    status = str(row.get("tenant_status", "")).strip().lower()
                    if status == "current":
                        return None
                    # Non-current: coalesce move_out → lease_to
                    move_out_dt = row["_move_out_dt"]
                    if move_out_dt is not None and not pd.isna(move_out_dt):
                        return move_out_dt
                    lease_to_dt = row["_lease_to_dt"]
                    if lease_to_dt is not None and not pd.isna(lease_to_dt):
                        return lease_to_dt
                    # Past with no dates at all — treat as active (shouldn't happen often)
                    return None
                filtered["to_date"] = filtered.apply(_effective_to_date, axis=1)

                # ── Entrata: deduplicate charges (same charge × multiple customers) ──
                # Charges belong to the lease, not individual customers.  Every
                # customer row for the same lease gets identical charges.  For
                # revenue aggregation we keep only one copy per unique charge.
                if "_entrata_charge_dedup_key" in filtered.columns:
                    _pre = len(filtered)

                    # Sort so active customers (current/past/notice) come first.
                    # After dedup (keep="first"), the surviving row will represent
                    # an active customer if one exists on the lease.  If ALL
                    # customers are cancelled/applicant, the surviving row will
                    # have that status → we filter it out below.
                    _ACTIVE_STATUSES = {"current", "past", "notice"}
                    filtered["_status_sort"] = filtered["tenant_status"].apply(
                        lambda s: 0 if str(s).strip().lower() in _ACTIVE_STATUSES else 1
                    )
                    filtered = filtered.sort_values("_status_sort", kind="stable")

                    # Drop rows where the dedup key is non-empty and duplicated
                    mask_has_key = filtered["_entrata_charge_dedup_key"].astype(str).str.strip().ne("")
                    dedup_rows = filtered[mask_has_key].drop_duplicates(
                        subset=["_entrata_charge_dedup_key"], keep="first"
                    )
                    non_dedup_rows = filtered[~mask_has_key]
                    filtered = pd.concat([dedup_rows, non_dedup_rows], ignore_index=True)
                    filtered = filtered.drop(columns=["_status_sort"], errors="ignore")
                    _deduped = _pre - len(filtered)

                    # Exclude charges where ALL customers on the lease are
                    # cancelled/applicant — these aren't real revenue.
                    _INACTIVE_STATUSES = {"cancelled", "applicant", "denied", "future"}
                    _pre_inactive = len(filtered)
                    filtered = filtered[
                        ~filtered["tenant_status"].apply(
                            lambda s: str(s).strip().lower() in _INACTIVE_STATUSES
                        )
                    ]
                    _dropped_inactive = _pre_inactive - len(filtered)

                    _info_parts = []
                    if _deduped > 0:
                        _info_parts.append(
                            f"Deduplicated {_deduped:,} shared-lease charge rows"
                        )
                    if _dropped_inactive > 0:
                        _info_parts.append(
                            f"Excluded {_dropped_inactive:,} charges from fully cancelled/applicant leases"
                        )
                    if _info_parts:
                        st.caption(" · ".join(_info_parts) + ".")

                # Extract launch dates per property (once)
                launch_dates = {}
                for _, row in filtered.iterrows():
                    prop = row['property_name']
                    launch = row.get('launch_date')
                    if prop not in launch_dates and launch is not None:
                        try:
                            if pd.isna(launch):
                                continue
                        except (TypeError, ValueError):
                            pass
                        if isinstance(launch, str) and launch.strip():
                            try:
                                launch_dates[prop] = datetime.strptime(str(launch)[:10], "%Y-%m-%d")
                            except:
                                pass
                        elif hasattr(launch, 'year'):
                            try:
                                # Verify year/month are real numbers (not NaN)
                                int(launch.year)
                                launch_dates[prop] = launch
                            except (ValueError, TypeError, OverflowError):
                                pass

                # Store the parsed rows — aggregation happens dynamically below
                charge_cols = [
                    "property_id", "property_name", "from_date", "to_date", "amount",
                    "tenant_code", "first_name", "last_name",
                    "tenant_status", "charge_code", "charge_type", "frequency",
                    "email", "move_in", "move_out", "lease_from", "lease_to",
                    "_raw_to_date", "_move_out_dt", "_lease_to_dt",
                ]
                # Only keep columns that exist (defensive)
                charge_cols = [c for c in charge_cols if c in filtered.columns]
                st.session_state.chart_data = {
                    "parsed_charges": filtered[charge_cols].to_dict("records"),
                    "launch_dates": launch_dates,
                    "label": label,
                    "filtered_csv": filtered.to_csv(index=False),
                }

            # ── Dynamically aggregate using the CURRENT slider value ────
            if "chart_data" in st.session_state and st.session_state.chart_data is not None:
                cd = st.session_state.chart_data
                launch_dates = cd["launch_dates"]
                label = cd["label"]

                # Build the display window from the actual data range
                today = datetime.now()
                window_end = datetime(today.year, today.month, 1)

                # Find the earliest charge date in the data
                _earliest = None
                for rec in cd["parsed_charges"]:
                    fd = rec.get("from_date")
                    if fd is not None and not pd.isna(fd):
                        fd_clean = datetime(int(fd.year), int(fd.month), 1)
                        if _earliest is None or fd_clean < _earliest:
                            _earliest = fd_clean
                window_start = _earliest if _earliest else datetime(today.year - 5, today.month, 1)
                # IMPORTANT: strip time components so month keys are midnight-aligned
                window_start = datetime(window_start.year, window_start.month, 1)
                months = [m.to_pydatetime() for m in pd.date_range(start=window_start, end=window_end, freq='MS')]

                # ── Classify charge codes as recurring vs one-time ──────────
                # Entrata: use explicit `frequency` field when available.
                # Yardi: infer from median date span per (property, charge_code).
                #   Median span > 60 days → recurring (charged monthly)
                #   Median span ≤ 60 days → one-time (deposit/fee)
                # One-time charges only count in their from_date month.
                _code_class = {}  # {(property_name, charge_code): "recurring" | "onetime"}

                # Group records by (property_name, charge_code)
                _by_prop_code = defaultdict(list)
                for rec in cd["parsed_charges"]:
                    key = (rec["property_name"], rec["charge_code"])
                    _by_prop_code[key].append(rec)

                # Per-property source system (matters in All Systems mode:
                # each property is classified by its own system's rules).
                _chart_sys_by_pid = st.session_state.get("property_system_by_pid", {}) or {}
                _session_sys = st.session_state.get("pmc_system")

                for (pname, code), recs in _by_prop_code.items():
                    _grp_sys = _session_sys
                    if _session_sys == "all":
                        _grp_sys = _chart_sys_by_pid.get(str(recs[0].get("property_id", "")), "yardi")
                    _grp_freqs = [str(r.get('frequency', '')).strip().lower() for r in recs if r.get('frequency') and not pd.isna(r.get('frequency'))]
                    if _grp_sys in ("real_page", "appfolio"):
                        # RealPage uses one row per observed post_month. Treat
                        # selected pet-rent codes as recurring even though each
                        # row's normalized date span is one month.
                        _code_class[(pname, code)] = "recurring"
                    elif _grp_freqs:
                        onetime_count = sum(1 for f in _grp_freqs if f == 'one-time')
                        monthly_count = sum(1 for f in _grp_freqs if f in ('monthly', 'recurring'))
                        _code_class[(pname, code)] = "onetime" if onetime_count > monthly_count else "recurring"
                    else:
                        spans = []
                        for r in recs:
                            f, t = r.get("from_date"), r.get("_raw_to_date") if "_raw_to_date" in r else r.get("to_date")
                            if f and t and not pd.isna(f) and not pd.isna(t):
                                try:
                                    spans.append((t - f).days)
                                except (TypeError, AttributeError):
                                    pass
                        # No valid date spans → assume recurring (missing dates ≠ one-time)
                        median_span = float(np.median(spans)) if spans else None
                        if median_span is None:
                            _code_class[(pname, code)] = "recurring"
                        else:
                            _code_class[(pname, code)] = "recurring" if median_span > 60 else "onetime"

                # Apply user overrides (from the charge type classification UI)
                _user_overrides = st.session_state.get("charge_type_overrides", {})
                if _user_overrides:
                    for (pname, code) in list(_code_class.keys()):
                        if code in _user_overrides:
                            _code_class[(pname, code)] = _user_overrides[code]

                # Aggregate charges into monthly buckets
                monthly_portfolio = defaultdict(float)
                monthly_portfolio_count = defaultdict(int)
                monthly_by_prop = defaultdict(lambda: defaultdict(float))

                _neg_rows = 0
                _neg_total = 0.0
                for rec in cd["parsed_charges"]:
                    prop = rec["property_name"]
                    amt = rec["amount"]
                    from_dt = rec["from_date"]
                    to_dt = rec["to_date"]

                    if from_dt is None or pd.isna(from_dt):
                        continue
                    if amt <= 0:
                        # Negative rows are usually concessions/adjustments
                        # (e.g. CONCPET). They are excluded from revenue, but
                        # surface the total so nobody thinks they vanished.
                        if amt < 0:
                            _neg_rows += 1
                            _neg_total += amt
                        continue

                    charge_start = datetime(int(from_dt.year), int(from_dt.month), 1)

                    # One-time charges (deposits/fees): only count in the from_date month
                    charge_type = _code_class.get((prop, rec.get("charge_code")), "recurring")
                    if charge_type == "onetime":
                        charge_end = charge_start
                    elif to_dt is not None and not pd.isna(to_dt) and isinstance(to_dt, datetime):
                        charge_end = datetime(int(to_dt.year), int(to_dt.month), 1)
                    else:
                        charge_end = window_end

                    for month in months:
                        if charge_start <= month <= charge_end:
                            monthly_portfolio[month] += amt
                            monthly_portfolio_count[month] += 1
                            monthly_by_prop[prop][month] += amt

                # Convert defaultdicts to regular dicts for consistency
                monthly_portfolio = dict(monthly_portfolio)
                monthly_portfolio_count = dict(monthly_portfolio_count)
                monthly_by_prop = {p: dict(v) for p, v in monthly_by_prop.items()}

                if _neg_rows:
                    st.caption(
                        f"{_neg_rows:,} negative charge rows totaling "
                        f"${_neg_total:,.0f} (concessions/adjustments, e.g. "
                        f"CONCPET) were excluded from revenue."
                    )

                # Compute pre/post launch analysis
                launch_analysis = compute_launch_analysis(monthly_by_prop, months, launch_dates)

                st.header("Fee Collection Analysis")

                latest_month = _latest_observed_revenue_month(monthly_by_prop, months) or months[-1]

                # ── Property funnel — consistent cascade ──────────────
                _launch_in_data_funnel = {p: d for p, d in launch_dates.items() if p in monthly_by_prop}
                _funnel_comparable = {
                    p: a for p, a in launch_analysis.items()
                    if _launch_analysis_is_comparable(a)
                } if launch_analysis else {}
                _render_property_funnel(
                    n_total=st.session_state.get("total_parent_props") or None,
                    n_api=st.session_state.get("api_props_count") or None,
                    n_with_charges=len(monthly_by_prop),
                    n_with_launch=len(_launch_in_data_funnel),
                    n_comparable=len(_funnel_comparable),
                )

                # Compute a SINGLE sort order (by total revenue, descending)
                # shared across Fee Revenue, Unit Adoption, and Resident Adoption
                prop_totals = {p: sum(monthly_by_prop[p].values()) for p in monthly_by_prop}
                sorted_props = sorted(prop_totals.keys(), key=lambda p: prop_totals[p], reverse=True)

                # Pre-fetch compliance data once (shared by KPIs and charts)
                pid_lookup = _build_property_id_lookup(cd["parsed_charges"])
                all_pids = list(set(pid_lookup.values()))
                comp_data = {}
                if all_pids:
                    comp_data = fetch_compliance_data(tuple(sorted(all_pids)))

                # ── Chart overlay controls ──────────────────────────────
                overlay_col1, overlay_col2 = st.columns([3, 2])
                with overlay_col1:
                    adoption_overlay = st.radio(
                        "Adoption overlay:",
                        ["None", "Unit Adoption %", "Resident Adoption %"],
                        index=0,
                        horizontal=True,
                        key="adoption_overlay",
                        help="Overlay an adoption trend line (purple) on the revenue bars to see the correlation "
                             "between adoption going up and revenue going up.",
                    )
                with overlay_col2:
                    show_missing_rent = st.toggle(
                        "Show uncollected pet rent",
                        value=False,
                        key="show_missing_rent",
                        help="Show estimated uncollected pet rent (orange) from confirmed tenants with active pet screening "
                             "stacked on top of collected revenue.",
                    )
                    show_suspected = st.toggle(
                        "Show suspected undisclosed",
                        value=False,
                        key="show_suspected",
                        help="Show estimated revenue from suspected undisclosed pets (red, stacked). "
                             "These are residents who started a profile but abandoned it, or had an "
                             "unresolved assistance request — signals they likely have a pet.",
                    )

                # Derive overlay mode
                _overlay = None
                _overlay_mode_label = None
                if adoption_overlay == "Unit Adoption %":
                    _overlay = "unit"
                    _overlay_mode_label = "Unit"
                elif adoption_overlay == "Resident Adoption %":
                    _overlay = "resident"
                    _overlay_mode_label = "Resident"

                # ── Fetch missing pet rent data if toggle is on ──
                # We cache the Snowflake profiles, but recompute monthly missing
                # whenever the slider changes (months change) so the bars are correct.
                _missing_rent_data = {}
                if show_missing_rent:
                    prop_ids = st.session_state.get("property_ids", [])
                    if prop_ids and selected_codes:
                        # Cache the profiles from Snowflake; recompute monthly when slider changes
                        cache_key = f"missing_rent_{hash(tuple(sorted(selected_codes)))}"
                        if cache_key not in st.session_state:
                            with st.spinner("Matching PetScreening tenants to charge data..."):
                                _missing_rent_data = fetch_missing_pet_rent_by_property(
                                    df, selected_codes, prop_ids, launch_dates, months,
                                )
                            st.session_state[cache_key] = _missing_rent_data
                        else:
                            # Re-use cached result but recalculate if display window changed
                            cached = st.session_state[cache_key]
                            # Check if months match; if not, refetch with new window
                            sample_key = next(iter(cached), None)
                            if sample_key and set(cached[sample_key].get("monthly_missing", {}).keys()) != set(months):
                                with st.spinner("Updating missing rent for new time window..."):
                                    _missing_rent_data = fetch_missing_pet_rent_by_property(
                                        df, selected_codes, prop_ids, launch_dates, months,
                                    )
                                st.session_state[cache_key] = _missing_rent_data
                            else:
                                _missing_rent_data = cached
                else:
                    # Clear cached data when toggle is off
                    for k in list(st.session_state.keys()):
                        if k.startswith("missing_rent_") and isinstance(st.session_state[k], dict):
                            del st.session_state[k]

                # ── Fetch suspected undisclosed data if toggle is on ──
                _suspected_data = {}
                if show_suspected:
                    prop_ids = st.session_state.get("property_ids", [])
                    if prop_ids and selected_codes:
                        cache_key_s = f"suspected_{hash(tuple(sorted(selected_codes)))}"
                        if cache_key_s not in st.session_state:
                            with st.spinner("Identifying suspected undisclosed pets..."):
                                _suspected_data = fetch_suspected_undisclosed_by_property(
                                    df, selected_codes, prop_ids, launch_dates, months,
                                )
                            st.session_state[cache_key_s] = _suspected_data
                        else:
                            cached_s = st.session_state[cache_key_s]
                            sample_key_s = next(iter(cached_s), None)
                            if sample_key_s and set(cached_s[sample_key_s].get("monthly_missing", {}).keys()) != set(months):
                                with st.spinner("Updating suspected data for new time window..."):
                                    _suspected_data = fetch_suspected_undisclosed_by_property(
                                        df, selected_codes, prop_ids, launch_dates, months,
                                    )
                                st.session_state[cache_key_s] = _suspected_data
                            else:
                                _suspected_data = cached_s
                else:
                    for k in list(st.session_state.keys()):
                        if k.startswith("suspected_") and isinstance(st.session_state[k], dict):
                            del st.session_state[k]

                # ═══════════════════════════════════════════════════════════
                # KPI ROW 1: Launch impact metrics (always shown)
                # ═══════════════════════════════════════════════════════════

                _rp_in_scope = (st.session_state.get("pmc_system") == "real_page") or (
                    st.session_state.get("pmc_system") == "all"
                    and any(v == "real_page" for v in (st.session_state.get("property_system_by_pid") or {}).values())
                )
                if _rp_in_scope:
                    st.info(
                        "**RealPage properties are current-state snapshots**: they "
                        "contribute current collected revenue and the missing / "
                        "suspected opportunity numbers, but **no before/after lift** "
                        "— the RealPage API cannot return payment history "
                        "(resident-ledger access pending with RealPage)."
                    )

                # ── Data health panel ──
                try:
                    _health = _run_data_health_checks(df, monthly_by_prop, months, launch_dates)
                    _n_pass = sum(1 for h in _health if h["status"] == "pass")
                    with st.expander(f"Data health — {_n_pass}/{len(_health)} checks passed"):
                        for h in _health:
                            _hi = "Pass" if h["status"] == "pass" else "Warning"
                            st.markdown(f"- **{_hi}** · {h['label']} — {h['detail']}")
                except Exception:
                    pass

                if launch_analysis:
                    comparable = {p: a for p, a in launch_analysis.items()
                                  if _launch_analysis_is_comparable(a)}
                    n_no_pre = len(launch_analysis) - len(comparable)

                    # Read toggle state (same as Summary tab)
                    _fc_use_avg = st.session_state.get("use_avg_lift_toggle", False)

                    # Avg lift (property-by-property average)
                    agg_diff_mo = sum(a["diff_monthly"] for a in comparable.values()) if comparable else 0
                    agg_diff = sum(a["diff_total"] for a in comparable.values()) if comparable else 0

                    # Simple lift (latest observed revenue month - pre baseline)
                    _fc_pre_baseline = sum(a["pre_avg"] for a in comparable.values()) if comparable else 0
                    _fc_latest = _latest_observed_revenue_month(monthly_by_prop, months, comparable.keys()) if comparable else latest_month
                    _fc_current_rev = sum(monthly_by_prop[p].get(_fc_latest, 0) for p in comparable.keys()) if comparable and _fc_latest else 0
                    _fc_simple_lift = _fc_current_rev - _fc_pre_baseline if _fc_pre_baseline > 0 else 0

                    # Display based on toggle
                    _fc_display_lift = agg_diff_mo if _fc_use_avg else _fc_simple_lift
                    _fc_lift_label = "Average Monthly Lift" if _fc_use_avg else "Monthly Lift"
                    sign_mo = "+" if _fc_display_lift >= 0 else ""
                    sign_t = "+" if agg_diff >= 0 else ""

                    # Only count launch dates for properties that actually have charge data
                    _launch_in_data = {p: d for p, d in launch_dates.items() if p in monthly_by_prop}
                    n_with_launch = len(_launch_in_data)
                    n_in_analysis = len(launch_analysis)
                    n_comparable = len(comparable)
                    n_future = max(0, n_with_launch - n_in_analysis)
                    n_no_launch = max(0, len(monthly_by_prop) - n_with_launch)

                    lcol1, lcol2, lcol3 = st.columns(3)
                    # Show current rev or post avg based on toggle
                    if _fc_use_avg:
                        _fc_post_avg = sum(a.get("post_recent_avg", a.get("post_monthly_avg", 0)) for a in comparable.values()) if comparable else 0
                        lcol1.metric(
                            "Post-PS Avg Pet Revenue",
                            f"${_fc_post_avg:,.0f}/mo",
                            help="Sum of each comparable property's post-launch average revenue."
                        )
                    else:
                        lcol1.metric(
                            "Current Pet Revenue",
                            f"${_fc_current_rev:,.0f}/mo",
                            help=f"Sum of pet fee charges for comparable properties in {latest_month.strftime('%b %Y')}."
                        )
                    lcol2.metric(
                        _fc_lift_label,
                        f"{sign_mo}${_fc_display_lift:,.0f}/mo",
                        help="Average monthly lift (property averages)" if _fc_use_avg else f"Current month ({latest_month.strftime('%b %Y')}) minus pre-PS baseline."
                    )

                    launch_detail = f"{n_comparable} comparable (pre & post data)"
                    if n_no_pre:
                        launch_detail += f"\n{n_no_pre} live before window (no pre data)"
                    if n_future:
                        launch_detail += f"\n{n_future} launched recently (no post data yet)"
                    if n_no_launch:
                        launch_detail += f"\n{n_no_launch} no launch date"

                    _total_props = len(st.session_state.get("property_ids", []))
                    lcol3.metric(
                        "Comparable Properties",
                        f"{n_comparable} of {_total_props}",
                        help=f"{n_comparable} properties with pre & post data (used for lift calculation).\n{launch_detail}",
                    )

                    caption_parts = [f"**{n_comparable}** properties have pre & post data for comparison."]
                    if n_no_pre:
                        caption_parts.append(f"**{n_no_pre}** launched before the window (excluded — no baseline).")
                    if n_future:
                        caption_parts.append(f"**{n_future}** launched too recently (no post-launch months yet).")
                    if n_no_launch:
                        caption_parts.append(f"**{n_no_launch}** have no launch date.")
                    st.caption(" ".join(caption_parts))

                    _is_entrata = st.session_state.get("pmc_system", "yardi") == "entrata"
                    if _is_entrata and launch_analysis:
                        n_unreliable = sum(1 for a in launch_analysis.values() if not a.get("baseline_reliable", True))
                        if n_unreliable > len(launch_analysis) / 2:
                            st.warning(
                                f"**Entrata data note:** {n_unreliable} of {len(launch_analysis)} properties "
                                f"have fewer than 3 months of pre-launch charge data. The Entrata API may not "
                                f"return full historical scheduled charges, making the before/after revenue "
                                f"comparison unreliable for this PMC. Post-launch trends are still accurate."
                            )

                # ── PetScreening impact methodology (always right below KPI row 1) ──
                with st.expander("How we calculate PetScreening impact — methodology & chart legend", expanded=False):
                    st.markdown("""
**Methodology**

For each property with a PetScreening launch date, we calculate the revenue impact by comparing
the average monthly pet fee revenue **before** and **after** launch.

| Metric | Formula | What it tells you |
|--------|---------|-------------------|
| **Before PetScreening (avg/mo)** | Average of up to 6 months before launch (uses whatever pre-launch data is available) | The property's baseline monthly pet fee collection before PetScreening was active — uses more data when available to smooth seasonal noise |
| **After PetScreening (avg/mo)** | Average of all completed post-launch months (excludes current partial month) | The property's average monthly pet fee collection since PetScreening launched |
| **Monthly Change** | Cumulative impact ÷ completed post months | Average monthly uplift since launch — mathematically consistent with cumulative total |
| **Total Change** | Sum(all post revenue) − (Pre avg × post months) | The actual observed cumulative revenue above the pre-launch baseline — real dollars, not projected |

**Reading the charts**

- <span style="color:#7D9BC1">**Blue bars**</span> = months **before** PetScreening was active
- <span style="color:#677848">**Green bars**</span> = months **after** PetScreening went live (including the launch month)
- <span style="color:#E2AB58">**Gold dotted line**</span> = the pre-launch monthly average (baseline from up to 6 months)
- <span style="color:#CF5A3F">**Red dashed line**</span> = the PetScreening launch date
- <span style="color:rgba(156,39,176,0.85)">**Purple line**</span> = adoption % overlay (when enabled)
- <span style="color:#DD7B45">**Orange bars (stacked)**</span> = estimated uncollected pet rent from confirmed tenants (when "Show uncollected pet rent" is on)
- <span style="color:#CF5A3F">**Red bars (stacked)**</span> = estimated uncollected from suspected undisclosed tenants (when "Show suspected undisclosed" is on)
- **Live** = property launched before the selected lookback window (all months are post-launch)
- **Grey bars** = no launch date available for this property

**Understanding the two toggles**

**Show uncollected pet rent (orange):** Shows revenue you're definitely not collecting from tenants
who have completed their PetScreening household pet screening but aren't being charged pet rent.

**Show suspected undisclosed (red):** Shows potential additional revenue from tenants who show signals
of having an undisclosed pet — abandoned screening, unresolved assistance requests, or suspicious no-pet
declarations. These are lower confidence but worth investigating.

**How we calculate both sets of bars (per month)**

1. We identify the relevant tenants (confirmed screening or suspected behavioral signals) not paying any of the selected charge codes
2. For each resident, we use their **actual lease dates** (move-in / move-out or lease start / lease end from the API) to determine which months they were living at the property
3. We only count months **on or after** the property's PetScreening launch date
4. We classify the selected charge codes as **recurring rent** or **one-time deposit** based on how paying tenants are being charged:
   - **Yardi:** inferred from median date span (> 60 days = recurring, ≤ 60 days = one-time)
   - **Entrata:** uses the explicit `frequency` field on each charge
   - **Recurring rent**: added to every month the missing tenant was active
   - **One-time deposit**: added only to the tenant's first active month
5. The estimated amount per missing tenant uses the average fee from tenants **who are paying** at that property

Both sets of bars **vary month to month** — they grow as more unpaid tenants move in, and shrink as tenants move out.
The subtitle on each chart shows "X unpaid" and/or "X suspected" badges.

**How launch month is handled**

The **launch month itself is counted as post-launch** (green). For example, if PetScreening launched
on Nov 19, the November bar is green because PetScreening was active for part of that month.
The pre-launch baseline uses up to 6 months *before* the launch month (e.g., May–Oct for a Nov launch).

**Important notes**

- The pre-launch baseline uses **up to 6 months** before the launch month (whatever data is available),
  giving a robust "business as usual" average that smooths out seasonal variation.
- The post-launch comparison uses **all completed post-launch months** (excluding the current partial month),
  reflecting the property's average performance since PetScreening launched.
- The cumulative impact ("Total Change") uses **actual observed revenue** — total post-launch revenue minus
  what the property would have collected at the pre-launch rate. This is real dollars, not a projection.
- Revenue changes may reflect factors beyond PetScreening (e.g., new units, rent adjustments, seasonal variation).
  This analysis measures the **observed change** in pet fee revenue coinciding with PetScreening's launch.
- Properties that launched before the lookback window have no pre-launch baseline available and are
  excluded from the impact calculation.
                    """, unsafe_allow_html=True)

                # ═══════════════════════════════════════════════════════════
                # KPI ROW 2: Revenue Opportunity at 100% (when overlay active)
                # ═══════════════════════════════════════════════════════════

                projected_100 = {}
                if _overlay and _overlay_mode_label:
                    _adoption_key = "unit_adoption" if _overlay == "unit" else "resident_adoption"
                    if comp_data:
                        for pname in sorted_props:
                            pid = pid_lookup.get(pname)
                            if not pid:
                                continue
                            prop_comp = comp_data.get(pid, {})
                            latest_adoption = None
                            for m in reversed(months):
                                entry = prop_comp.get(m)
                                if entry and entry.get(_adoption_key) is not None:
                                    latest_adoption = entry[_adoption_key] * 100
                                    break
                            latest_rev = monthly_by_prop.get(pname, {}).get(months[-1], 0) if months else 0
                            if latest_adoption and latest_adoption > 0 and latest_rev > 0:
                                projected_rev = latest_rev / (latest_adoption / 100)
                                projected_100[pname] = {
                                    "current_rev": latest_rev,
                                    "current_adoption": latest_adoption,
                                    "projected_rev_100": projected_rev,
                                    "additional_rev": projected_rev - latest_rev,
                                }

                    if projected_100:
                        total_current = sum(p["current_rev"] for p in projected_100.values())
                        total_projected = sum(p["projected_rev_100"] for p in projected_100.values())
                        total_additional = total_projected - total_current
                        avg_adoption = (
                            sum(p["current_adoption"] for p in projected_100.values())
                            / len(projected_100)
                        )

                        st.divider()
                        ic1, ic2, ic3 = st.columns(3)
                        ic1.metric(
                            "Current Monthly Pet-Related Revenue",
                            f"${total_current:,.0f}",
                            help="Sum of latest-month pet fee revenue for properties with adoption data.",
                        )
                        ic2.metric(
                            f"Projected at 100% {_overlay_mode_label} Adoption",
                            f"${total_projected:,.0f}",
                            delta=f"+${total_additional:,.0f}/mo",
                            delta_color="normal",
                            help="What the total monthly revenue would be if every property reached 100% adoption. "
                                 "Calculated as: current revenue ÷ current adoption rate.",
                        )
                        ic3.metric(
                            f"Avg {_overlay_mode_label} Adoption Now",
                            f"{avg_adoption:.1f}%",
                            help=f"Average {_overlay_mode_label.lower()}-level adoption "
                                 f"across {len(projected_100)} properties with data.",
                        )
                        st.caption(
                            f"Based on **{len(projected_100)}** properties with both pet fee revenue "
                            f"and {_overlay_mode_label.lower()}-level adoption data in the latest month."
                        )
                    elif comp_data:
                        st.info(f"No properties currently have both pet fee revenue **and** "
                                f"{_overlay_mode_label.lower()} adoption data to project.")

                if _overlay and _overlay_mode_label:
                    with st.expander(f"How we calculate Revenue Opportunity at 100% {_overlay_mode_label} Compliance", expanded=False):
                        st.markdown(f"""
**What is {_overlay_mode_label} Adoption?**

{'**Unit Adoption** = the percentage of units at a property that have at least one active PetScreening profile.' if _overlay == 'unit' else '**Resident Adoption** = the percentage of residents at a property that have created a PetScreening profile.'}

This data comes from the **Quarterly Business Review (QBR) reporting table** (`R_QUARTERLY_BUSINESS_REVIEW_REPORTING`),
which tracks adoption month-by-month for every property after their PetScreening launch date.

**How we calculate the projection**

The revenue projection answers: **"If this property reached 100% adoption, how much pet fee revenue could they collect?"**

For each property, we take two numbers from the **latest month**:

| Input | Source | Example |
|-------|--------|---------|
| **Current Monthly Pet-Related Revenue** | Sum of selected pet fee charge codes from Yardi (this app) | $5,000/mo |
| **Current {_overlay_mode_label} Adoption** | {'Active units ÷ Total units' if _overlay == 'unit' else 'Active users ÷ Total users'} from QBR table | 65% |

Then we calculate:

```
Projected Revenue at 100% = Current Revenue ÷ (Current Adoption / 100)
                          = $5,000 ÷ 0.65
                          = $7,692/mo

Additional Revenue = Projected − Current
                   = $7,692 − $5,000
                   = +$2,692/mo
```

**Why this works**: If a property earns $5,000/mo when only 65% of {'units' if _overlay == 'unit' else 'residents'} have completed screening,
then the average revenue per compliant {'unit' if _overlay == 'unit' else 'resident'} is $5,000 ÷ 65% = ~$76.92.
At 100% adoption, that same per-{'unit' if _overlay == 'unit' else 'resident'} rate would yield ~$7,692/mo.

**Important notes**

- This is a **linear extrapolation** — it assumes revenue scales proportionally with adoption.
  In reality, the last {'units' if _overlay == 'unit' else 'residents'} to comply may have fewer or no pets, so actual revenue may be lower.
- Only properties with **both** pet fee revenue **and** adoption data in the latest month are included.
- The <span style="color:rgba(156,39,176,0.85)">**purple line**</span> on each chart shows the adoption trend (right y-axis, 0–100%).
  When adoption goes up, you should see revenue (bars) go up too.
                        """, unsafe_allow_html=True)

                st.divider()

                # ═══════════════════════════════════════════════════════════
                # CHARTS — scrollable container
                # ═══════════════════════════════════════════════════════════

                # ── 3. Individual Property Trend Charts ─────────────────
                fig4 = None  # keep in scope for export
                if len(monthly_by_prop) > 0:
                    n_display_props = len(monthly_by_prop)
                    st.subheader("Individual Property Trends" if n_display_props > 1 else "Property Trend")

                    fig4 = build_individual_property_charts(
                        monthly_by_prop, months, launch_dates, label,
                        launch_analysis=launch_analysis,
                        overlay_mode=_overlay,
                        compliance_data=comp_data if _overlay else None,
                        prop_id_lookup=pid_lookup if _overlay else None,
                        missing_rent_data=_missing_rent_data,
                        show_missing_rent=show_missing_rent,
                        suspected_data=_suspected_data,
                        show_suspected=show_suspected,
                    )

                    # Scrollable container for many properties; plain for 1–4
                    use_scroll = n_display_props > 4
                    chart_container = st.container(height=900) if use_scroll else st.container()

                    with chart_container:
                        if fig4:
                            st.plotly_chart(fig4, use_container_width=True)

                    # ── Per-property projected revenue table (below charts) ──
                    if projected_100:
                        st.subheader(f"Projected Revenue by Property at 100% {_overlay_mode_label} Adoption")
                        st.caption(
                            f"Sorted by additional revenue opportunity. Based on latest-month revenue "
                            f"and {_overlay_mode_label.lower()}-level adoption for **{len(projected_100)}** properties."
                        )
                        proj_rows = []
                        for prop, p in sorted(projected_100.items(), key=lambda x: -x[1]["additional_rev"]):
                            short = prop.split(" - ", 1)[-1] if " - " in prop else prop
                            proj_rows.append({
                                "Property": short,
                                f"Current {_overlay_mode_label} Adoption": f"{p['current_adoption']:.1f}%",
                                "Current Revenue": f"${p['current_rev']:,.0f}",
                                "Projected (100%)": f"${p['projected_rev_100']:,.0f}",
                                "Additional Rev": f"+${p['additional_rev']:,.0f}/mo",
                            })
                        _render_table(pd.DataFrame(proj_rows))

                # ── 3b. Uncollected Pet Rent Summary (confirmed + suspected) ──
                _has_confirmed = show_missing_rent and _missing_rent_data
                _has_suspected = show_suspected and _suspected_data
                if _has_confirmed or _has_suspected:
                    st.divider()
                    latest_month = months[-1]
                    _total_fetched = len(st.session_state.get("property_ids", []))

                    # ── Confirmed stats ──
                    c_total = sum(v["missing_count"] for v in _missing_rent_data.values()) if _has_confirmed else 0
                    c_props = sum(1 for v in _missing_rent_data.values() if v["missing_count"] > 0) if _has_confirmed else 0
                    c_mo = sum(v["monthly_missing"].get(latest_month, 0) for v in _missing_rent_data.values()) if _has_confirmed else 0

                    # Split recurring vs one-time for confirmed
                    c_recurring_mo = 0
                    c_onetime_total = 0
                    if _has_confirmed:
                        for _v in _missing_rent_data.values():
                            _cnt = _v.get("missing_count", 0)
                            if _cnt == 0:
                                continue
                            _rec = _v.get("avg_recurring", 0)
                            _ot = _v.get("avg_onetime", 0)
                            c_recurring_mo += _cnt * _rec
                            c_onetime_total += _cnt * _ot

                    # ── Suspected stats ──
                    s_total = sum(v["missing_count"] for v in _suspected_data.values()) if _has_suspected else 0
                    s_props = sum(1 for v in _suspected_data.values() if v["missing_count"] > 0) if _has_suspected else 0
                    s_mo = sum(v["monthly_missing"].get(latest_month, 0) for v in _suspected_data.values()) if _has_suspected else 0

                    # ── Combined ──
                    combined_total = c_total + s_total
                    combined_mo = c_mo + s_mo
                    combined_props = len(set(
                        [p for p, v in _missing_rent_data.items() if v["missing_count"] > 0] if _has_confirmed else []
                    ) | set(
                        [p for p, v in _suspected_data.items() if v["missing_count"] > 0] if _has_suspected else []
                    ))

                    # ── Forward-looking impact ──
                    _annual_recurring = c_recurring_mo * 12
                    _annual_impact = _annual_recurring + c_onetime_total
                    _cap_rate = 0.05
                    _value_impact = _annual_impact / _cap_rate if _annual_impact > 0 else 0

                    st.subheader("Uncollected Pet Rent Summary")

                    # ── Confirmed: the billing correction ──
                    if _has_confirmed and c_total > 0:
                        st.markdown("**Confirmed** — tenants with active PetScreening profiles who aren't paying pet rent")
                        cc1, cc2, cc3 = st.columns(3)
                        cc1.metric(
                            "Tenants Not Paying",
                            f"{c_total:,}",
                            help=f"Active tenants with household profiles not being charged pet rent, across {c_props} properties.",
                        )
                        cc2.metric(
                            "Missing Monthly Pet Rent",
                            f"${c_recurring_mo:,.0f}/mo",
                            help=f"Recurring monthly pet rent not collected. Based on each property's avg fee from paying tenants.",
                        )
                        cc3.metric(
                            "Annual Revenue Impact",
                            f"${_annual_impact:,.0f}/yr",
                            help=f"Pet rent: ${c_recurring_mo:,.0f}/mo x 12 = ${_annual_recurring:,.0f}/yr"
                                 + (f"  |  One-time fees: ${c_onetime_total:,.0f}" if c_onetime_total > 0 else "")
                                 + f"  |  Total: ${_annual_impact:,.0f}/yr",
                        )
                        if _value_impact > 0:
                            st.caption(
                                f"At a 5% cap rate, this ${_annual_impact:,.0f}/yr represents "
                                f"**${_value_impact:,.0f} in unrealized property value**. "
                                f"This is a billing correction, not a sales effort."
                            )
                    elif _has_confirmed:
                        st.markdown("**Confirmed** — all profiled tenants are paying pet rent ✓")

                    # ── Suspected: the undisclosed opportunity ──
                    if _has_suspected and s_total > 0:
                        st.markdown("---")
                        st.markdown("**Suspected Undisclosed** — tenants who started screening but abandoned, had unresolved assistance requests, or declared no-pet after starting an assistance profile")

                        # Compute suspected recurring using SAME avg fee as
                        # confirmed missing rent (tenants x portfolio avg fee).
                        # This keeps the numbers apples-to-apples and avoids
                        # suspected showing higher revenue than confirmed.
                        _confirmed_avg_fee = (c_recurring_mo / c_total) if (_has_confirmed and c_total > 0 and c_recurring_mo > 0) else 0
                        if _confirmed_avg_fee <= 0:
                            # Fall back to per-property avg if no confirmed data
                            _all_s_rec = [_v.get("avg_recurring", 0) for _v in _suspected_data.values() if _v.get("avg_recurring", 0) > 0]
                            _confirmed_avg_fee = sum(_all_s_rec) / len(_all_s_rec) if _all_s_rec else 0
                        s_recurring_mo = s_total * _confirmed_avg_fee
                        s_onetime_total = 0  # One-time fees excluded for suspected
                        s_annual_recurring = s_recurring_mo * 12
                        s_annual_impact = s_annual_recurring

                        sc1, sc2, sc3 = st.columns(3)
                        sc1.metric(
                            "Suspected Tenants",
                            f"{s_total:,}",
                            help=f"Tenants showing signals of undisclosed pets, across {s_props} properties.",
                        )
                        sc2.metric(
                            "Potential Pet Rent",
                            f"${s_recurring_mo:,.0f}/mo",
                            help=f"{s_total:,} tenants x ${_confirmed_avg_fee:,.0f}/mo avg fee = ${s_recurring_mo:,.0f}/mo (same avg fee as confirmed missing rent).",
                        )
                        sc3.metric(
                            "Potential Annual Impact",
                            f"${s_annual_impact:,.0f}/yr",
                            help=f"Estimated annual revenue if suspected undisclosed pets are confirmed and charged.",
                        )

                    # ── Combined: total opportunity ──
                    if _has_confirmed and c_total > 0 and _has_suspected and s_total > 0:
                        st.markdown("---")
                        _combined_recurring_mo = c_recurring_mo + s_recurring_mo
                        _combined_onetime = c_onetime_total + s_onetime_total
                        _combined_annual = _combined_recurring_mo * 12 + _combined_onetime
                        _combined_value = _combined_annual / _cap_rate if _combined_annual > 0 else 0
                        st.markdown(
                            f"**Combined opportunity:** If all confirmed and suspected tenants were charged, "
                            f"that would be **${_combined_recurring_mo:,.0f}/mo** in pet rent"
                            + (f" + ${_combined_onetime:,.0f} in one-time fees" if _combined_onetime > 0 else "")
                            + f" — **${_combined_annual:,.0f}/yr** in total revenue, "
                            f"adding **${_combined_value:,.0f}** in property value at a 5% cap rate."
                        )

                    # ── "Show the Math" transparency section ──
                    if _has_confirmed and c_total > 0 and c_recurring_mo > 0:
                        _avg_fee_overall = c_recurring_mo / c_total if c_total > 0 else 0

                        # Gather per-property fee details
                        _fee_ranges = []
                        for _p, _v in _missing_rent_data.items():
                            _rec = _v.get("avg_recurring", 0)
                            if _rec > 0 and _v.get("missing_count", 0) > 0:
                                _fee_ranges.append(_rec)
                        _fee_min = min(_fee_ranges) if _fee_ranges else 0
                        _fee_max = max(_fee_ranges) if _fee_ranges else 0

                        with st.expander("How we calculate these numbers", expanded=False):
                            st.markdown(f"""
**Monthly uncollected pet rent:**

| | |
|---|---|
| Tenants with pets, not paying selected charges | **{c_total:,}** |
| x Avg pet rent at their property | **${_avg_fee_overall:,.0f}/mo** |
| **= Missing pet rent** | **${c_recurring_mo:,.0f}/mo** |
""")
                            if c_onetime_total > 0:
                                _avg_ot_overall = c_onetime_total / c_total if c_total > 0 else 0
                                st.markdown(f"""
**One-time fees not collected:**

| | |
|---|---|
| Same {c_total:,} tenants | x ${_avg_ot_overall:,.0f} avg one-time fee |
| **= One-time fees** | **${c_onetime_total:,.0f}** |
""")
                            st.markdown(f"""
**Annual revenue impact:**

| | |
|---|---|
| Monthly pet rent | ${c_recurring_mo:,.0f} x 12 = **${_annual_recurring:,.0f}/yr** |
| One-time fees | **${c_onetime_total:,.0f}** |
| **Total annual impact** | **${_annual_impact:,.0f}** |

**Unrealized property value** (at 5% cap rate):

| | |
|---|---|
| Annual impact | ${_annual_impact:,.0f} |
| / 5% cap rate | |
| **= Unrealized value** | **${_value_impact:,.0f}** |

> The avg fee varies by property (${_fee_min:,.0f} -- ${_fee_max:,.0f}/mo). We use each property's actual average from paying tenants, not a flat number.
""")
                            # Show a few example properties
                            _example_rows = []
                            for _p in sorted(
                                _missing_rent_data.keys(),
                                key=lambda p: _missing_rent_data[p].get("missing_count", 0),
                                reverse=True,
                            )[:5]:
                                _v = _missing_rent_data[_p]
                                if _v.get("missing_count", 0) == 0:
                                    continue
                                _short = _p.split(" - ", 1)[-1] if " - " in _p else _p
                                _rec = _v.get("avg_recurring", 0)
                                _ot = _v.get("avg_onetime", 0)
                                _cnt = _v["missing_count"]
                                _mo_val = _cnt * _rec
                                _ot_val = _cnt * _ot
                                _row = {
                                    "Property": _short,
                                    "Missing Tenants": _cnt,
                                    "Avg Pet Rent": f"${_rec:,.0f}/mo",
                                    "= Monthly": f"${_mo_val:,.0f}/mo",
                                }
                                if c_onetime_total > 0:
                                    _row["Avg One-Time"] = f"${_ot:,.0f}"
                                    _row["= One-Time"] = f"${_ot_val:,.0f}"
                                _example_rows.append(_row)
                            if _example_rows:
                                st.markdown("**Example properties:**")
                                _render_table(pd.DataFrame(_example_rows))

                    # Per-property breakdown table
                    with st.expander(f"Uncollected pet rent by property ({combined_props} properties)", expanded=False):
                        mr_rows = []
                        all_pnames = set(
                            list(_missing_rent_data.keys() if _has_confirmed else []) +
                            list(_suspected_data.keys() if _has_suspected else [])
                        )
                        for pname in sorted(all_pnames, key=lambda p: -(
                            # Sort by annual impact (recurring*12 + one-time)
                            (lambda ci, si: (
                                ci.get("missing_count", 0) * ci.get("avg_recurring", 0) * 12
                                + ci.get("missing_count", 0) * ci.get("avg_onetime", 0)
                                + si.get("missing_count", 0) * si.get("avg_recurring", 0) * 12
                                + si.get("missing_count", 0) * si.get("avg_onetime", 0)
                            ))(
                                _missing_rent_data.get(p, {}) if _has_confirmed else {},
                                _suspected_data.get(p, {}) if _has_suspected else {},
                            )
                        )):
                            c_info = _missing_rent_data.get(pname, {}) if _has_confirmed else {}
                            s_info = _suspected_data.get(pname, {}) if _has_suspected else {}
                            c_cnt = c_info.get("missing_count", 0)
                            s_cnt = s_info.get("missing_count", 0)
                            if c_cnt == 0 and s_cnt == 0:
                                continue
                            short = pname.split(" - ", 1)[-1] if " - " in pname else pname
                            _row = {"Property": short}
                            if _has_confirmed:
                                _row["Confirmed"] = c_cnt
                            if _has_suspected:
                                _row["Suspected"] = s_cnt
                            _row["Total Tenants"] = c_cnt + s_cnt
                            # Pet rent per month
                            _p_rec_mo = c_cnt * c_info.get("avg_recurring", 0) + s_cnt * s_info.get("avg_recurring", 0)
                            _row["Pet Rent/Mo"] = f"${_p_rec_mo:,.0f}"
                            # One-time fees
                            _p_ot = c_cnt * c_info.get("avg_onetime", 0) + s_cnt * s_info.get("avg_onetime", 0)
                            if c_onetime_total > 0:
                                _row["One-Time Fees"] = f"${_p_ot:,.0f}"
                            # Annual impact
                            _p_annual = _p_rec_mo * 12 + _p_ot
                            _row["Annual Impact"] = f"${_p_annual:,.0f}"
                            # Property value at 5% cap
                            _p_value = _p_annual / 0.05 if _p_annual > 0 else 0
                            _row["Value Impact"] = f"${_p_value:,.0f}"
                            mr_rows.append(_row)
                        if mr_rows:
                            _render_table(pd.DataFrame(mr_rows))

                # ── 4. Current Snapshot Bar Chart ───────────────────────
                fig3 = build_current_snapshot_chart(monthly_by_prop, months, label)
                if fig3:
                    st.divider()
                    st.plotly_chart(fig3, use_container_width=True)

                # ── 5. Export Interactive Report ──────────────────────
                st.divider()
                export_col1, export_col2, export_col3 = st.columns([3, 1, 1])
                with export_col1:
                    st.markdown("**Export Interactive Report** — Share a fully interactive HTML report "
                                "with clients. Charts are scrollable, hoverable, and zoomable.")
                with export_col2:
                    if st.button("Generate Report", use_container_width=True,
                                 help="Build the HTML report with current charts & overlay settings"):
                        report_html = generate_html_report(
                            label=label,
                            fig_individual=fig4,
                            fig_snapshot=fig3,
                            launch_analysis=launch_analysis,
                            monthly_by_prop=monthly_by_prop,
                            months=months,
                            launch_dates=launch_dates,
                            projected_100=projected_100 if projected_100 else None,
                            overlay_mode_label=_overlay_mode_label,
                            missing_rent_data=_missing_rent_data if show_missing_rent else None,
                            show_missing_rent=show_missing_rent,
                            total_properties_fetched=len(st.session_state.get("property_ids", [])),
                            use_avg_lift=_fc_use_avg,
                        )
                        st.session_state["export_html"] = report_html
                        st.session_state["export_label"] = label
                        st.toast("Report ready for download!")
                with export_col3:
                    if "export_html" in st.session_state and st.session_state["export_html"]:
                        safe_name = st.session_state.get("export_label", "report").replace(" ", "_").replace("/", "-")
                        st.download_button(
                            label="Download HTML",
                            data=st.session_state["export_html"],
                            file_name=f"PetScreening_Report_{safe_name}_{datetime.now().strftime('%Y%m%d')}.html",
                            mime="text/html",
                            use_container_width=True,
                        )
                    else:
                        st.button("Download HTML", disabled=True, use_container_width=True,
                                  help="Click 'Generate Report' first")

                # ── 5b. AR Transactions: Scheduled vs Actual (Entrata only) ──
                _is_entrata_view = st.session_state.get("pmc_system", "yardi") == "entrata"
                ar_df = st.session_state.get("ar_charges_df")
                if _is_entrata_view and ar_df is not None and not ar_df.empty:
                    st.divider()
                    # Developer-view only — hidden in the default client-facing view
                    if st.session_state.get("dev_view", False):
                        with st.expander("Scheduled vs Actual Revenue (AR Transactions)", expanded=False):
                            st.caption(
                                "Compares **scheduled charges** (what the lease says should be billed) "
                                "with **AR transactions** (what was actually posted to the ledger). "
                                "AR data comes from Entrata's `includeArTransactions` response."
                            )

                            ar_df_work = ar_df.copy()
                            ar_df_work['_amt'] = pd.to_numeric(ar_df_work['amount'], errors='coerce').fillna(0)
                            ar_df_work['_paid'] = pd.to_numeric(ar_df_work['amount_paid'], errors='coerce').fillna(0)

                            _ar_post_dates = pd.to_datetime(ar_df_work['post_month'], errors='coerce', format='mixed')
                            ar_df_work['_post_month_dt'] = _ar_post_dates.apply(
                                lambda d: datetime(d.year, d.month, 1) if pd.notna(d) else None
                            )
                            ar_in_window = ar_df_work[
                                ar_df_work['_post_month_dt'].notna()
                                & ar_df_work['_post_month_dt'].between(months[0], months[-1])
                            ]

                            ar_monthly_portfolio = defaultdict(float)
                            ar_monthly_by_prop = defaultdict(lambda: defaultdict(float))
                            for _, row in ar_in_window.iterrows():
                                m = row['_post_month_dt']
                                ar_monthly_portfolio[m] += row['_amt']
                                ar_monthly_by_prop[row['property_name']][m] += row['_amt']

                            # Summary metrics
                            total_ar = ar_in_window['_amt'].sum()
                            total_ar_paid = ar_in_window['_paid'].sum()
                            total_scheduled = sum(monthly_portfolio.get(m, 0) for m in months)

                            mc1, mc2, mc3, mc4 = st.columns(4)
                            mc1.metric("Scheduled Revenue", f"${total_scheduled:,.0f}",
                                       help="Total from scheduled charges over the display window")
                            mc2.metric("Actual Posted (AR)", f"${total_ar:,.0f}",
                                       help="Total from AR transactions posted over the display window")
                            mc3.metric("AR Collected", f"${total_ar_paid:,.0f}",
                                       help="Of the posted amount, how much has been paid")
                            _diff_pct = ((total_ar / total_scheduled - 1) * 100) if total_scheduled > 0 else 0
                            mc4.metric("Variance", f"{_diff_pct:+.1f}%",
                                       help="AR posted vs Scheduled: positive means actual > scheduled")

                            # Monthly comparison table
                            compare_rows = []
                            for m in months:
                                sched = monthly_portfolio.get(m, 0)
                                actual = ar_monthly_portfolio.get(m, 0)
                                compare_rows.append({
                                    "Month": m.strftime("%b %Y"),
                                    "Scheduled ($)": f"{sched:,.2f}",
                                    "Actual AR ($)": f"{actual:,.2f}",
                                    "Difference ($)": f"{(actual - sched):,.2f}",
                                    "Variance (%)": f"{((actual / sched - 1) * 100):+.1f}%" if sched > 0 else "N/A",
                                })
                            compare_df = pd.DataFrame(compare_rows)
                            st.markdown("**Monthly Comparison**")
                            st.table(compare_df)

                            # Per-property breakdown
                            if ar_monthly_by_prop:
                                st.subheader("Per-Property: Scheduled vs Actual")
                                prop_compare = []
                                for prop in sorted(monthly_by_prop.keys()):
                                    sched_total = sum(monthly_by_prop.get(prop, {}).get(m, 0) for m in months)
                                    ar_total = sum(ar_monthly_by_prop.get(prop, {}).get(m, 0) for m in months)
                                    short = prop.split(" - ", 1)[-1] if " - " in prop else prop
                                    prop_compare.append({
                                        "Property": short,
                                        "Scheduled ($)": f"{sched_total:,.2f}",
                                        "Actual AR ($)": f"{ar_total:,.2f}",
                                        "Difference ($)": f"{(ar_total - sched_total):,.2f}",
                                        "Variance (%)": f"{((ar_total / sched_total - 1) * 100):+.1f}%" if sched_total > 0 else ("N/A" if ar_total == 0 else "New"),
                                    })
                                prop_compare_df = pd.DataFrame(prop_compare)
                                st.table(prop_compare_df)

                            st.caption(
                                f"AR data: **{len(ar_in_window):,}** pet-related transactions in window. "
                                f"Total AR rows (all time): **{len(ar_df):,}**"
                            )

                            # ── Raw AR Transactions (individual posted charges) ──
                            st.markdown("---")
                            st.subheader("AR Transactions (Individual Posted Charges)")
                            st.caption(
                                "Each row = one actual charge posted to the ledger. "
                                "This is what the property management system actually billed, "
                                "not what the lease says *should* be billed."
                            )

                            # Build a clean display table from ar_df
                            _ar_display = ar_df.copy()
                            _ar_display['_amt'] = pd.to_numeric(_ar_display['amount'], errors='coerce').fillna(0)
                            _ar_display['_paid'] = pd.to_numeric(_ar_display['amount_paid'], errors='coerce').fillna(0)
                            _ar_display['_bal'] = pd.to_numeric(_ar_display['balance_due'], errors='coerce').fillna(0)

                            _ar_table_rows = []
                            for _, _r in _ar_display.iterrows():
                                _ar_table_rows.append({
                                    "Unit": _r.get('unit', ''),
                                    "Lease ID": _r.get('lease_id', ''),
                                    "Charge": _r.get('charge_code_name', ''),
                                    "Post Date": str(_r.get('post_date', ''))[:10],
                                    "Post Month": str(_r.get('post_month', ''))[:7],
                                    "Amount": f"${_r['_amt']:,.2f}",
                                    "Paid": f"${_r['_paid']:,.2f}",
                                    "Balance": f"${_r['_bal']:,.2f}",
                                    "Description": str(_r.get('description', ''))[:50],
                                })

                            if _ar_table_rows:
                                _ar_table_df = pd.DataFrame(_ar_table_rows)

                                # Month filter
                                _ar_months = sorted(set(r["Post Month"] for r in _ar_table_rows if r["Post Month"]))
                                if _ar_months:
                                    _sel_ar_month = st.selectbox(
                                        "Filter by post month:",
                                        ["All"] + _ar_months,
                                        index=0,
                                        key="ar_month_filter",
                                    )
                                    if _sel_ar_month != "All":
                                        _ar_table_df = _ar_table_df[_ar_table_df["Post Month"] == _sel_ar_month]

                                st.markdown(f"**{len(_ar_table_df)}** transactions shown")
                                _render_table(_ar_table_df, height=400)

                                st.download_button(
                                    "Download AR transactions",
                                    data=_ar_display.to_csv(index=False),
                                    file_name=f"ar_transactions_{datetime.now().strftime('%Y%m%d')}.csv",
                                    mime="text/csv",
                                    key="dl_ar_inline",
                                )
                            else:
                                st.info("No AR transactions found for this property.")

                # ── 6. Collapsible Tables Section ───────────────────────
                st.divider()
                # Developer-view only — hidden in the default client-facing view
                if st.session_state.get("dev_view", False):
                    with st.expander("Show detailed tables (PetScreening impact breakdown & monthly revenue)", expanded=False):

                        # -- Impact Breakdown Table --
                        if launch_analysis:
                            st.subheader("PetScreening Impact by Property")
                            st.caption(
                                "Sorted by biggest monthly revenue increase after PetScreening launch. "
                                "Properties launched before the lookback window show 'Live before window' (no pre-launch data to compare)."
                            )

                            la_rows = []
                            for prop, a in launch_analysis.items():
                                short = prop.split(" - ", 1)[-1] if " - " in prop else prop

                                # Properties need observed pre-launch data and a meaningful baseline to compare.
                                # A 1-2 month baseline is marked as low-data, but still contributes
                                # so the table matches the top aggregate and individual chart lift badges.
                                has_comparison = _launch_analysis_is_comparable(a)

                                if has_comparison:
                                    sign_m = "+" if a["diff_monthly"] >= 0 else ""
                                    sign_t = "+" if a["diff_total"] >= 0 else ""
                                    arrow = "↑" if a["diff_monthly"] >= 0 else "↓"
                                    la_rows.append({
                                        "Property": short,
                                        "Launch": a["launch_month"].strftime("%b %Y"),
                                        "Pre-PS Avg ($/mo)": f"${a['pre_avg']:,.0f}",
                                        "Current Avg ($/mo)": f"${a['post_recent_avg']:,.0f}",
                                        "Monthly Lift": f"{arrow} {sign_m}${a['diff_monthly']:,.0f}/mo",
                                        "Cumulative Impact": f"{sign_t}${a['diff_total']:,.0f}",
                                        "Window": f"{a['n_pre']}mo pre · {a.get('n_recent_post', 0)}mo completed post · {a['n_post']}mo total",
                                        "_sort": a["diff_monthly"],
                                        "_comparable": True,
                                    })
                                else:
                                    la_rows.append({
                                        "Property": short,
                                        "Launch": a["launch_month"].strftime("%b %Y"),
                                        "Pre-PS Avg ($/mo)": "Live before window",
                                        "Current Avg ($/mo)": f"${a['post_monthly_avg']:,.0f}",
                                        "Monthly Lift": "—",
                                        "Cumulative Impact": "—",
                                        "Window": f"{a['n_post']}mo after (no pre data)",
                                        "_sort": -999999,
                                        "_comparable": False,
                                    })

                            # Sort: comparable properties first (by monthly change desc),
                            # then non-comparable at the bottom
                            la_rows.sort(key=lambda r: (not r["_comparable"], -r["_sort"] if r["_comparable"] else 0))
                            for r in la_rows:
                                del r["_sort"]
                                del r["_comparable"]

                            la_df = pd.DataFrame(la_rows)
                            _render_table(la_df, height=500)

                            # Count how many are excluded from the aggregate
                            n_no_pre = sum(1 for a in launch_analysis.values() if a["n_pre"] == 0)
                            if n_no_pre > 0:
                                st.caption(
                                    f"**{n_no_pre}** propert{'y' if n_no_pre == 1 else 'ies'} launched before the "
                                    f"lookback window — shown as 'Live before window' and **excluded** from "
                                    f"aggregate impact numbers above (no pre-launch baseline to compare)."
                                )

                        # -- Monthly Revenue Table --
                        st.divider()
                        st.subheader("Monthly Revenue Table")
                        monthly_table = []
                        for month in months:
                            row = {"Month": month.strftime("%b %Y"),
                                   "Total Revenue": monthly_portfolio.get(month, 0),
                                   "# Charges": monthly_portfolio_count.get(month, 0)}
                            for prop in sorted(monthly_by_prop.keys()):
                                short = prop.split(" - ", 1)[-1] if " - " in prop else prop
                                row[short] = monthly_by_prop[prop].get(month, 0)
                            monthly_table.append(row)

                        monthly_df = pd.DataFrame(monthly_table)
                        _render_table(monthly_df, height=500)

                        st.download_button(
                            "Download Selected Charges CSV",
                            data=cd["filtered_csv"],
                            file_name=f"pet_rent_{label.replace(' ', '_')}.csv",
                            mime="text/csv",
                        )

                # ── 6. Per-Property Data Explorer ───────────────────────
                st.divider()
                # Developer-view only — hidden in the default client-facing view
                if st.session_state.get("dev_view", False):
                    with st.expander("Property Data Explorer (debug / compare individual charge rows)", expanded=False):
                        st.caption(
                            "**to_date** = coalesce(charge_to_date, move_out, lease_to) for past tenants. "
                            "Current tenants show ACTIVE. **to_date_src** shows which date field was used."
                        )
                        all_props_sorted = sorted(monthly_by_prop.keys())
                        short_to_full = {(p.split(" - ", 1)[-1] if " - " in p else p): p for p in all_props_sorted}
                        explorer_prop = st.selectbox(
                            "Select a property to inspect:",
                            [""] + sorted(short_to_full.keys()),
                            key="explorer_prop",
                        )
                        if explorer_prop:
                            full_prop = short_to_full[explorer_prop]

                            # Filter charges for this property
                            prop_charges = [
                                r for r in cd["parsed_charges"]
                                if r["property_name"] == full_prop and r.get("amount", 0) > 0
                            ]
                            raw_df = pd.DataFrame(prop_charges) if prop_charges else pd.DataFrame()

                            if not raw_df.empty:
                                # ── Summary by charge code ──
                                st.markdown("##### Charge Code Breakdown")
                                code_summary = (
                                    raw_df.groupby("charge_code")
                                    .agg(
                                        tenants=("tenant_code", "nunique"),
                                        rows=("charge_code", "size"),
                                        total=("amount", "sum"),
                                        avg_amt=("amount", "mean"),
                                        min_amt=("amount", "min"),
                                        max_amt=("amount", "max"),
                                    )
                                    .sort_values("total", ascending=False)
                                    .reset_index()
                                )
                                code_summary.columns = [
                                    "Charge Code", "Unique Tenants", "Rows",
                                    "Total $", "Avg $/charge", "Min $", "Max $",
                                ]
                                for col in ["Total $", "Avg $/charge", "Min $", "Max $"]:
                                    code_summary[col] = code_summary[col].apply(lambda x: f"${x:,.2f}")
                                _render_table(code_summary)

                                # ── Monthly breakdown ──
                                st.markdown("##### Monthly Revenue")
                                prop_monthly = monthly_by_prop.get(full_prop, {})
                                prop_table = []
                                for m in months:
                                    # Count charges active in this month
                                    active_in_month = [
                                        rec for rec in prop_charges
                                        if rec["from_date"] is not None
                                        and not pd.isna(rec["from_date"])
                                        and datetime(int(rec["from_date"].year), int(rec["from_date"].month), 1) <= m
                                        and (
                                            datetime(int(rec["to_date"].year), int(rec["to_date"].month), 1) >= m
                                            if rec["to_date"] is not None and not pd.isna(rec["to_date"]) and isinstance(rec["to_date"], datetime)
                                            else window_end >= m
                                        )
                                    ]
                                    # Break down by charge code
                                    code_breakdown = defaultdict(float)
                                    for rec in active_in_month:
                                        code_breakdown[rec.get("charge_code", "?")] += rec["amount"]
                                    breakdown_str = ", ".join(
                                        f"{code}: ${val:,.0f}" for code, val in sorted(code_breakdown.items())
                                    ) if code_breakdown else ""

                                    prop_table.append({
                                        "Month": m.strftime("%b %Y"),
                                        "Revenue": f"${prop_monthly.get(m, 0):,.2f}",
                                        "# Charges": len(active_in_month),
                                        "# Tenants": len(set(r.get("tenant_code", "") for r in active_in_month)),
                                        "By Code": breakdown_str,
                                    })
                                st.caption(f"**{explorer_prop}** — monthly revenue for display window ({len(months)} months)")
                                _render_table(pd.DataFrame(prop_table), height=400)

                                # ── Full charge rows with tenant info ──
                                st.markdown("##### All Charge Line Items")

                                # Show which globally-selected codes exist for this property
                                available_codes = sorted(raw_df["charge_code"].dropna().unique().tolist())
                                missing_codes = [c for c in selected_codes if c not in available_codes]
                                if missing_codes:
                                    st.info(
                                        f"This property only has **{', '.join(available_codes)}** from your selected codes. \n"
                                        f"Not found here: {', '.join(f'`{c}`' for c in missing_codes)} "
                                        f"(those codes may not apply to this property)."
                                    )

                                # Optional: filter by charge code within explorer
                                filter_code = st.multiselect(
                                    "Filter by charge code:",
                                    available_codes,
                                    default=available_codes,
                                    key="explorer_code_filter",
                                )
                                display_df = raw_df[raw_df["charge_code"].isin(filter_code)].copy() if filter_code else raw_df.copy()

                                display_df = display_df.sort_values(
                                    ["charge_code", "tenant_code", "from_date"],
                                    ascending=[True, True, False],
                                )
                                # Format dates
                                display_df["from_date"] = display_df["from_date"].apply(
                                    lambda d: d.strftime("%Y-%m-%d") if d is not None and not pd.isna(d) else "")
                                display_df["to_date"] = display_df["to_date"].apply(
                                    lambda d: d.strftime("%Y-%m-%d") if d is not None and not pd.isna(d) and isinstance(d, datetime) else "ACTIVE")
                                display_df["amount"] = display_df["amount"].apply(lambda x: f"${x:,.2f}")

                                # Build tenant name column
                                display_df["tenant"] = (
                                    display_df.get("first_name", pd.Series(dtype=str)).fillna("")
                                    + " "
                                    + display_df.get("last_name", pd.Series(dtype=str)).fillna("")
                                ).str.strip()

                                # to_date source indicator: shows where the end date came from
                                def _to_date_source(row):
                                    raw = row.get("_raw_to_date")
                                    mo = row.get("_move_out_dt")
                                    lt = row.get("_lease_to_dt")
                                    if raw is not None and not pd.isna(raw):
                                        return "charge"
                                    if mo is not None and not pd.isna(mo):
                                        return "move_out"
                                    if lt is not None and not pd.isna(lt):
                                        return "lease_end"
                                    return ""
                                if "_raw_to_date" in display_df.columns:
                                    display_df["to_date_src"] = display_df.apply(_to_date_source, axis=1)
                                else:
                                    display_df["to_date_src"] = ""

                                show_cols = ["tenant_code", "tenant", "tenant_status",
                                             "charge_code", "charge_type", "amount",
                                             "from_date", "to_date", "to_date_src"]
                                # Only show columns that exist
                                show_cols = [c for c in show_cols if c in display_df.columns]

                                st.caption(
                                    f"**{explorer_prop}** — {len(display_df)} charge line items "
                                    f"({display_df['tenant_code'].nunique() if 'tenant_code' in display_df.columns else '?'} unique tenants)"
                                )
                                _render_table(display_df[show_cols], height=500)

                                # Download with all columns
                                st.download_button(
                                    f"Download {explorer_prop} charges",
                                    data=display_df[show_cols].to_csv(index=False),
                                    file_name=f"charges_{explorer_prop.replace(' ', '_')}.csv",
                                    mime="text/csv",
                                    key="explorer_dl",
                                )
                            else:
                                st.info("No charge rows found for this property.")

        # ─── TAB 2: Summary ──────────────────────────────────────
        with tab_next:
            _cd = st.session_state.get("chart_data")
            if _cd is None:
                st.info("**Analyze your data first.** Go to the **Fee Collection Charts** tab, "
                        "select charge codes, and click **Analyze & Visualize** to populate this summary.")
            else:
                _label = st.session_state.selection_label
                _prop_ids = st.session_state.get("property_ids", [])

                # ── Rebuild the same aggregates used in the charts tab ──
                _today = datetime.now()
                _we = datetime(_today.year, _today.month, 1)
                # Derive window from data (same as charts tab)
                _earliest = None
                for rec in _cd["parsed_charges"]:
                    fd = rec.get("from_date")
                    if fd is not None and not pd.isna(fd):
                        fd_clean = datetime(int(fd.year), int(fd.month), 1)
                        if _earliest is None or fd_clean < _earliest:
                            _earliest = fd_clean
                _ws = _earliest if _earliest else datetime(_today.year - 5, _today.month, 1)
                _ws = datetime(_ws.year, _ws.month, 1)
                _months = [m.to_pydatetime() for m in pd.date_range(start=_ws, end=_we, freq='MS')]

                # Classify charge codes (same logic as charts tab)
                _has_freq2 = any('frequency' in rec for rec in _cd["parsed_charges"])
                _cc2 = {}
                _by_pc2 = defaultdict(list)
                for rec in _cd["parsed_charges"]:
                    _by_pc2[(rec["property_name"], rec["charge_code"])].append(rec)
                for (pn, cc), recs in _by_pc2.items():
                    if _has_freq2:
                        freqs = [str(r.get('frequency', '')).strip().lower() for r in recs if r.get('frequency')]
                        ot = sum(1 for f in freqs if f == 'one-time')
                        mo = sum(1 for f in freqs if f in ('monthly', 'recurring'))
                        _cc2[(pn, cc)] = "onetime" if ot > mo else "recurring"
                    else:
                        spans = []
                        for r in recs:
                            f, t = r.get("from_date"), r.get("_raw_to_date") if "_raw_to_date" in r else r.get("to_date")
                            if f and t and not pd.isna(f) and not pd.isna(t):
                                try:
                                    spans.append((t - f).days)
                                except (TypeError, AttributeError):
                                    pass
                        # No valid date spans → assume recurring (missing dates ≠ one-time)
                        _median = float(np.median(spans)) if spans else None
                        if _median is None:
                            _cc2[(pn, cc)] = "recurring"
                        else:
                            _cc2[(pn, cc)] = "recurring" if _median > 60 else "onetime"

                # Apply user charge-type overrides (same as Charts tab)
                _user_overrides2 = st.session_state.get("charge_type_overrides", {})
                if _user_overrides2:
                    for (pn, cc) in list(_cc2.keys()):
                        if cc in _user_overrides2:
                            _cc2[(pn, cc)] = _user_overrides2[cc]

                _monthly_portfolio = defaultdict(float)
                _monthly_by_prop = defaultdict(lambda: defaultdict(float))
                for rec in _cd["parsed_charges"]:
                    prop = rec["property_name"]
                    amt = rec["amount"]
                    from_dt = rec["from_date"]
                    to_dt = rec["to_date"]
                    if from_dt is None or pd.isna(from_dt) or amt <= 0:
                        continue
                    cs = datetime(int(from_dt.year), int(from_dt.month), 1)
                    _ct2 = _cc2.get((prop, rec.get("charge_code")), "recurring")
                    if _ct2 == "onetime":
                        ce = cs
                    elif to_dt is not None and not pd.isna(to_dt) and isinstance(to_dt, datetime):
                        ce = datetime(int(to_dt.year), int(to_dt.month), 1)
                    else:
                        ce = _we
                    for month in _months:
                        if cs <= month <= ce:
                            _monthly_portfolio[month] += amt
                            _monthly_by_prop[prop][month] += amt
                _monthly_by_prop = {p: dict(v) for p, v in _monthly_by_prop.items()}

                _launch_dates = _cd.get("launch_dates", {})
                _launch_analysis = compute_launch_analysis(_monthly_by_prop, _months, _launch_dates)

                # Comparable properties (with observed pre data and meaningful baseline)
                _comparable = {p: a for p, a in _launch_analysis.items()
                               if _launch_analysis_is_comparable(a)} if _launch_analysis else {}
                _agg_diff_mo = sum(a["diff_monthly"] for a in _comparable.values()) if _comparable else 0
                _agg_diff = sum(a["diff_total"] for a in _comparable.values()) if _comparable else 0

                # Latest observed month revenue
                _latest_month = _latest_observed_revenue_month(_monthly_by_prop, _months) if _months else None
                _current_monthly_rev = sum(
                    _monthly_by_prop[p].get(_latest_month, 0) for p in _monthly_by_prop
                ) if _latest_month else 0

                # Adoption data
                _pid_lookup = _build_property_id_lookup(_cd["parsed_charges"])
                _all_pids = list(set(_pid_lookup.values()))
                _comp_data = fetch_compliance_data(tuple(sorted(_all_pids))) if _all_pids else {}

                # NOTE: property_doors_by_name is rebuilt AFTER the doors query below

                # Adoption basis — an explicit control here instead of
                # silently inheriting from the Charts overlay radio (which
                # defaults to "None" → Unit without the user ever choosing).
                _overlay_sel = st.session_state.get("adoption_overlay", "None")
                _adopt_choice = st.radio(
                    "Adoption basis for this summary and the PDF:",
                    ["Unit Adoption", "Resident Adoption"],
                    index=1 if _overlay_sel == "Resident Adoption %" else 0,
                    horizontal=True,
                    key="summary_adoption_basis",
                    help=(
                        "Unit adoption = % of units with a completed PetScreening "
                        "profile; resident adoption = % of residents. Drives the "
                        "adoption KPI and the projected-at-100% numbers everywhere "
                        "in this summary and the generated reports."
                    ),
                )
                if _adopt_choice == "Resident Adoption":
                    _adopt_key = "resident_adoption"
                    _adopt_type_label = "Resident"
                else:
                    _adopt_key = "unit_adoption"
                    _adopt_type_label = "Unit"

                # Projected revenue at 100% adoption + adoption average
                # (match the Charts tab logic: only properties with BOTH adoption AND revenue)
                _projected_100 = {}
                for pname in _monthly_by_prop:
                    pid = _pid_lookup.get(pname)
                    if not pid:
                        continue
                    prop_comp = _comp_data.get(pid, {})
                    latest_adopt = None
                    for m in reversed(_months):
                        entry = prop_comp.get(m)
                        if entry and entry.get(_adopt_key) is not None:
                            latest_adopt = entry[_adopt_key] * 100
                            break
                    latest_rev = _monthly_by_prop.get(pname, {}).get(_latest_month, 0) if _latest_month else 0
                    if latest_adopt and latest_adopt > 0 and latest_rev > 0:
                        projected = latest_rev / (latest_adopt / 100)
                        _projected_100[pname] = {
                            "current_rev": latest_rev,
                            "current_adoption": latest_adopt,
                            "projected_rev_100": projected,
                            "additional_rev": projected - latest_rev,
                        }

                _total_current_for_proj = sum(p["current_rev"] for p in _projected_100.values())
                _total_projected = sum(p["projected_rev_100"] for p in _projected_100.values())
                _total_additional = _total_projected - _total_current_for_proj
                _n_proj_props = len(_projected_100)

                # Average adoption — same set as projected_100 (matches Charts tab exactly)
                _avg_adoption = (
                    sum(p["current_adoption"] for p in _projected_100.values())
                    / len(_projected_100)
                ) if _projected_100 else None

                # Missing pet rent summary. Previously this only read the
                # session cache written by the Charts-tab toggles, so a user
                # who never flipped "Show uncollected pet rent" got a PDF
                # with no missing-rent tranche. Now the Summary tab computes
                # it itself when absent (and shares the same cache keys).
                _mr_total_profiles = 0
                _mr_current_mo = 0
                _mr_data = {}
                for k in list(st.session_state.keys()):
                    if k.startswith("missing_rent_") and isinstance(st.session_state[k], dict):
                        _mr_data = st.session_state[k]
                        break
                if not _mr_data and selected_codes and _prop_ids:
                    with st.spinner("Matching PetScreening tenants to charge data (uncollected pet rent)..."):
                        _mr_data = fetch_missing_pet_rent_by_property(
                            df, selected_codes, _prop_ids, _launch_dates, _months,
                        )
                    st.session_state[f"missing_rent_{hash(tuple(sorted(selected_codes)))}"] = _mr_data
                if _mr_data:
                    _mr_total_profiles = sum(v["missing_count"] for v in _mr_data.values())
                    _mr_current_mo = sum(
                        v["monthly_missing"].get(_latest_month, 0) for v in _mr_data.values()
                    ) if _latest_month else 0

                # Suspected undisclosed summary — same auto-compute behavior.
                _su_total_profiles = 0
                _su_current_mo = 0
                _su_data = {}
                for k in list(st.session_state.keys()):
                    if k.startswith("suspected_") and isinstance(st.session_state[k], dict):
                        _su_data = st.session_state[k]
                        break
                if not _su_data and selected_codes and _prop_ids:
                    with st.spinner("Computing suspected undisclosed pets..."):
                        _su_data = fetch_suspected_undisclosed_by_property(
                            df, selected_codes, _prop_ids, _launch_dates, _months,
                        )
                    st.session_state[f"suspected_{hash(tuple(sorted(selected_codes)))}"] = _su_data
                if _su_data:
                    _su_total_profiles = sum(v["missing_count"] for v in _su_data.values())
                    _su_current_mo = sum(
                        v["monthly_missing"].get(_latest_month, 0) for v in _su_data.values()
                    ) if _latest_month else 0

                # Recalculate suspected undisclosed revenue using the SAME
                # avg RECURRING fee as confirmed missing rent. The raw
                # monthly_missing sums include one-time deposits in the first
                # active month, inflating the headline. We compute:
                #   avg_fee = sum(count * avg_recurring) / total_profiles
                # This matches the "$28/mo" shown in the Missing Rent tab.
                if _mr_data and _mr_total_profiles > 0:
                    _mr_recurring_only = sum(
                        v.get("missing_count", 0) * v.get("avg_recurring", 0)
                        for v in _mr_data.values()
                    )
                    _avg_fee = _mr_recurring_only / _mr_total_profiles if _mr_total_profiles > 0 else 0
                else:
                    _all_avg_rec = [v.get("avg_recurring", 0) for v in (_su_data or {}).values() if v.get("avg_recurring", 0) > 0]
                    _avg_fee = sum(_all_avg_rec) / len(_all_avg_rec) if _all_avg_rec else 0
                if _su_total_profiles > 0 and _avg_fee > 0:
                    _su_current_mo = _su_total_profiles * _avg_fee
                # Also recalculate _mr_current_mo to use pure recurring (no one-time)
                if _mr_data and _mr_total_profiles > 0:
                    _mr_current_mo = sum(
                        v.get("missing_count", 0) * v.get("avg_recurring", 0)
                        for v in _mr_data.values()
                    )

                # ═══════════════════════════════════════════════════════════════
                #  TRANCHE-BASED IMPACT SUMMARY
                # ═══════════════════════════════════════════════════════════════

                n_props_total = len(_prop_ids)
                n_props_with_data = len(_monthly_by_prop)
                # Calculate total units — use QBR reporting table (authoritative), fall back to charge data
                _total_units = 0
                _property_doors = {}  # {PROPERTY_ID: door_count}
                _property_doors_by_name = {}  # {PROPERTY_NAME: door_count}
                _units_from_doors = False
                try:
                    # Build list of property names from the data for QBR lookup
                    _prop_names_for_doors = list(_monthly_by_prop.keys())
                    _pid_list = [int(p) for p in st.session_state.get("property_ids", []) if str(p).strip()]
                    if _prop_names_for_doors or _pid_list:
                        _doors_conn = get_snowflake_connection()
                        _doors_cur = _doors_conn.cursor(snowflake.connector.DictCursor)

                        # Primary: QBR reporting table (most accurate unit counts)
                        if _prop_names_for_doors:
                            _name_placeholders = ", ".join(["%s"] * len(_prop_names_for_doors))
                            _doors_cur.execute(f"""
                                SELECT PROPERTY_NAME, PROPERTY_ID, PROPERTY_NUMBER_OF_DOORS AS NUM_DOORS
                                FROM PROD.REPORTING.R_QUARTERLY_BUSINESS_REVIEW_REPORTING
                                WHERE PROPERTY_NAME IN ({_name_placeholders})
                                  AND PROPERTY_NUMBER_OF_DOORS IS NOT NULL
                                  AND PROPERTY_NUMBER_OF_DOORS > 0
                                QUALIFY ROW_NUMBER() OVER (
                                    PARTITION BY PROPERTY_NAME
                                    ORDER BY PROPERTY_NUMBER_OF_DOORS DESC
                                ) = 1
                            """, _prop_names_for_doors)
                            _doors_rows = _doors_cur.fetchall()
                            if _doors_rows:
                                for r in _doors_rows:
                                    _pid = int(r["PROPERTY_ID"]) if r.get("PROPERTY_ID") else None
                                    _num = int(r["NUM_DOORS"])
                                    _pn = r.get("PROPERTY_NAME", "")
                                    if _pid:
                                        _property_doors[_pid] = _num
                                    if _pn:
                                        _property_doors_by_name[_pn] = _num
                                _total_units = sum(_property_doors_by_name.values())
                                _units_from_doors = True

                        # Fallback: D_PROPERTIES by property_id (less accurate but works)
                        if not _units_from_doors and _pid_list:
                            _doors_ids = ", ".join(str(p) for p in _pid_list)
                            _doors_cur.execute(f"""
                                SELECT PROPERTY_ID, PROPERTY_NUMBER_OF_DOORS AS NUM_DOORS
                                FROM PROD.COMMON.D_PROPERTIES
                                WHERE PROPERTY_ID IN ({_doors_ids})
                                  AND PROPERTY_NUMBER_OF_DOORS IS NOT NULL
                                  AND PROPERTY_NUMBER_OF_DOORS > 0
                            """)
                            _doors_rows = _doors_cur.fetchall()
                            if _doors_rows:
                                for r in _doors_rows:
                                    _property_doors[int(r["PROPERTY_ID"])] = int(r["NUM_DOORS"])
                                _total_units = sum(_property_doors.values())
                                _units_from_doors = True

                        _doors_cur.close()
                except Exception:
                    pass
                if not _units_from_doors:
                    # Last resort: unique unit_codes in charge data
                    if 'unit_code' in df.columns and 'property_name' in df.columns:
                        _unit_df = df[df['property_name'].isin(_monthly_by_prop.keys())]
                        _unit_df = _unit_df[_unit_df['unit_code'].notna() & (_unit_df['unit_code'] != '')]
                        _total_units = _unit_df.drop_duplicates(subset=['property_name', 'unit_code']).shape[0]
                st.session_state["_total_units"] = _total_units
                st.session_state["_property_doors"] = _property_doors
                # Build property_name -> door count mapping for PDF
                # If QBR query populated _property_doors_by_name directly, use it;
                # otherwise fall back to pid-based mapping from D_PROPERTIES
                if not _property_doors_by_name:
                    for _pname, _pid_val in _pid_lookup.items():
                        if _pid_val in _property_doors:
                            _property_doors_by_name[_pname] = _property_doors[_pid_val]
                st.session_state["_property_doors_by_name"] = _property_doors_by_name
                _launch_in_data_wn = {p: d for p, d in _launch_dates.items() if p in _monthly_by_prop}
                n_with_launch = len(_launch_in_data_wn)

                # Pre-PS baseline: sum of pre_avg across comparable properties
                _pre_baseline_total = sum(a["pre_avg"] for a in _comparable.values()) if _comparable else 0

                # Tranche 1 numbers
                _t1_mo = _agg_diff_mo
                _t1_total = _agg_diff
                _t1_pct = (_t1_mo / _pre_baseline_total * 100) if _pre_baseline_total > 0 else 0
                _t1_months = max(a["n_post"] for a in _comparable.values()) if _comparable else 0

                # Tranche 2 numbers
                _t2_tenants = _mr_total_profiles
                _t2_mo = _mr_current_mo
                _t2_props = sum(1 for v in _mr_data.values() if v["missing_count"] > 0) if _mr_data else 0
                _t1_t2_combined = _t1_mo + _t2_mo
                _t1_t2_pct = (_t1_t2_combined / _pre_baseline_total * 100) if _pre_baseline_total > 0 else 0

                # Tranche 3 numbers
                _t3_adoption = _avg_adoption
                _t3_additional = _total_additional
                _t3_total_impact = _t1_mo + _t2_mo + _t3_additional
                _t3_pct = (_t3_total_impact / _pre_baseline_total * 100) if _pre_baseline_total > 0 else 0

                # ── Branded header ──
                _logo_img = f'<img src="{_PS_LOGO_WHITE_URI}" height="22" alt="PetScreening">' if _PS_LOGO_WHITE_URI else ""
                _today_str = datetime.now().strftime("%B %d, %Y")
                st.markdown(
                    f'<div style="background:linear-gradient(135deg,#1F2257 0%,#2a2d6e 100%);'
                    f'border-radius:12px;padding:28px 32px 20px 32px;margin-bottom:28px">'
                    f'<div style="display:flex;align-items:center;gap:12px;margin-bottom:8px">'
                    f'{_logo_img}'
                    f'<span style="color:#E2AB58;font-family:Lora,Georgia,serif;font-size:22px;'
                    f'font-weight:700;letter-spacing:-0.3px">PetScreening Impact Analysis</span>'
                    f'</div>'
                    f'<p style="color:#DAEBF5;font-family:Poppins,Arial,sans-serif;font-size:15px;'
                    f'margin:0;opacity:0.9">{_label} &middot; {_today_str}</p>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

                # ── Property funnel — consistent cascade ──────────────
                _render_property_funnel(
                    n_total=st.session_state.get("total_parent_props") or None,
                    n_api=st.session_state.get("api_props_count") or None,
                    n_with_charges=n_props_with_data,
                    n_with_launch=n_with_launch,
                    n_with_adoption=_n_proj_props if _n_proj_props > 0 else None,
                    n_comparable=len(_comparable) if _comparable else None,
                )

                # ═══════════════════════════════════════════════════════════════
                #  SUMMARY AT A GLANCE
                # ═══════════════════════════════════════════════════════════════
                def _format_summary_large_currency(val):
                    """Format large currency values with K/M suffixes for compact cards."""
                    val = val or 0
                    if abs(val) >= 1_000_000:
                        return f"${val / 1_000_000:,.1f}M"
                    if abs(val) >= 100_000:
                        return f"${val / 1_000:,.0f}K"
                    return f"${val:,.0f}"

                def _summary_glance_card(value_text, label_text, sublabel_text="", color="#1F2257", bg="#fff"):
                    """Render an At-a-Glance style big-number card for the app Summary tab."""
                    _sub_html = ""
                    if sublabel_text:
                        _sub_html = f'<div style="font-size:12px;color:#86868B;margin-top:5px;line-height:1.35">{sublabel_text}</div>'
                    return (
                        f'<div style="background:{bg};border:1px solid #E8E4DA;border-radius:14px;'
                        f'padding:24px 18px;text-align:center;min-height:142px;'
                        f'box-shadow:0 1px 2px rgba(31,34,87,0.04)">'
                        f'<div style="font-size:34px;line-height:1.05;font-weight:750;color:{color};letter-spacing:-1.1px;'
                        f'font-family:Poppins,Arial,sans-serif;white-space:normal;overflow-wrap:anywhere">{value_text}</div>'
                        f'<div style="font-size:12px;line-height:1.25;font-weight:700;color:#636569;margin-top:10px;'
                        f'text-transform:uppercase;letter-spacing:0.65px;font-family:Poppins,Arial,sans-serif">{label_text}</div>'
                        f'{_sub_html}'
                        f'</div>'
                    )

                def _render_summary_card_row(cards, row_label=None, row_label_color="#1F2257"):
                    """Render a row of up to three At-a-Glance cards with an optional pill label."""
                    if row_label:
                        st.markdown(
                            f'<div style="display:inline-block;background:{row_label_color};color:white;'
                            f'border-radius:999px;padding:4px 11px;margin:10px 0 8px 0;'
                            f'font-family:Poppins,Arial,sans-serif;font-size:11px;font-weight:700;'
                            f'text-transform:uppercase;letter-spacing:0.7px">{row_label}</div>',
                            unsafe_allow_html=True,
                        )
                    cols = st.columns(3)
                    for idx, col in enumerate(cols):
                        card = cards[idx] if idx < len(cards) else {"value": "", "label": "", "sub": "", "color": "#1F2257"}
                        with col:
                            st.markdown(_summary_glance_card(
                                card.get("value", ""),
                                card.get("label", ""),
                                card.get("sub", ""),
                                color=card.get("color", "#1F2257"),
                                bg=card.get("bg", "#fff"),
                            ), unsafe_allow_html=True)

                _dark_blue = "#1F2257"
                _teal_blue = "#3B7F8C"
                _green = "#677848"
                _orange = "#DD7B45"
                _gray = "#636569"
                _latest_str = _latest_month.strftime("%b %Y") if _latest_month else "N/A"
                _use_avg = st.session_state.get("use_avg_lift_toggle", False)

                _has_comparable = bool(_comparable)
                _comp_current_rev = sum(
                    _monthly_by_prop[p].get(_latest_month, 0)
                    for p in _comparable
                ) if (_comparable and _latest_month) else 0
                _headline_current = _comp_current_rev if _has_comparable else _current_monthly_rev
                _headline_pre = _pre_baseline_total
                _comp_post_avg_sum = sum(
                    a.get("post_recent_avg", a.get("post_monthly_avg", 0))
                    for a in _comparable.values()
                ) if _comparable else 0
                _display_current = _comp_post_avg_sum if (_use_avg and _comp_post_avg_sum > 0) else _headline_current
                _display_current_label = "Post-PS Avg Revenue" if (_use_avg and _comp_post_avg_sum > 0) else "Current Revenue"
                _display_current_sub = (
                    f"Avg across {len(_comparable)} properties, post-launch months"
                    if (_use_avg and _comp_post_avg_sum > 0 and _has_comparable)
                    else (f"Same {len(_comparable)} properties, latest month" if _has_comparable else _latest_str)
                )
                _simple_lift_mo = _headline_current - _headline_pre if _headline_pre > 0 else 0
                _active_lift = _t1_mo if _use_avg else _simple_lift_mo
                _lift_label = "Average Monthly Lift" if _use_avg else "Monthly Lift"
                _simple_pct = (_simple_lift_mo / _headline_pre * 100) if _headline_pre > 0 else 0

                _comparable_units = sum(
                    _property_doors_by_name.get(p, 0)
                    for p in _comparable
                ) if _comparable and _property_doors_by_name else 0
                _headline_units = _comparable_units if _comparable_units > 0 else _total_units
                _lift_per_unit = _active_lift / _headline_units if _headline_units and _headline_units > 0 and _active_lift else 0
                _cap_rate = 0.05
                _asset_value_impact = (_active_lift * 12 / _cap_rate) if _active_lift > 0 else 0

                # Split confirmed missing rent into recurring monthly revenue for
                # the At-a-Glance Opportunity row. This intentionally excludes
                # one-time fees so the headline matches the PDF's monthly
                # "Pet Revenue Found" card.
                _s2_recurring_mo = 0
                if _mr_data:
                    for _v in _mr_data.values():
                        _cnt = _v.get("missing_count", 0)
                        if _cnt > 0:
                            _s2_recurring_mo += _cnt * _v.get("avg_recurring", 0)
                else:
                    _s2_recurring_mo = _t2_mo

                _found_mo = (_s2_recurring_mo or 0) + (_su_current_mo or 0)
                _found_residents = (_t2_tenants or 0) + (_su_total_profiles or 0)
                _units_for_found = _headline_units if _headline_units and _headline_units > 0 else _total_units
                _found_per_unit = _found_mo / _units_for_found if _units_for_found and _units_for_found > 0 and _found_mo > 0 else 0
                _combined_per_unit = (_lift_per_unit or 0) + (_found_per_unit or 0)
                _found_asset_value = (_found_mo * 12 / _cap_rate) if _found_mo > 0 else 0
                _has_missing_rent_data = any(
                    k.startswith("missing_rent_") and isinstance(st.session_state[k], dict)
                    for k in st.session_state.keys()
                )

                st.markdown(
                    '<p style="font-family:Lora,Georgia,serif;font-size:22px;font-weight:700;'
                    'color:#1F2257;margin:24px 0 6px 0">At a Glance</p>'
                    '<p style="font-family:Poppins,Arial,sans-serif;font-size:13px;color:#636569;'
                    'margin:0 0 14px 0">Same headline card structure as the generated report: actual value created first, then pet revenue found, then operating context.</p>',
                    unsafe_allow_html=True,
                )

                _glance_row1 = []
                if _has_comparable and _headline_units > 0:
                    _glance_row1.append({
                        "value": f"{_headline_units:,}",
                        "label": "Comparable Units",
                        "sub": f"{len(_comparable)} properties with pre & post data",
                        "color": _dark_blue,
                    })
                elif _total_units > 0:
                    _glance_row1.append({
                        "value": f"{_total_units:,}",
                        "label": "Live Units",
                        "sub": f"Across {_n_props(n_props_with_data)}",
                        "color": _dark_blue,
                    })
                else:
                    _glance_row1.append({
                        "value": f"{n_props_with_data}",
                        "label": "Properties Analyzed",
                        "sub": f"of {n_props_total} selected properties",
                        "color": _dark_blue,
                    })
                _glance_row1.append({
                    "value": f"${_headline_pre:,.0f}/mo" if _headline_pre > 0 else "--",
                    "label": "Pre-PS Revenue",
                    "sub": f"Avg baseline across {len(_comparable)} properties" if _has_comparable else "No pre-launch baseline",
                    "color": _dark_blue if _headline_pre > 0 else _gray,
                })
                _glance_row1.append({
                    "value": f"${_display_current:,.0f}/mo" if _display_current > 0 else "--",
                    "label": _display_current_label,
                    "sub": _display_current_sub,
                    "color": _dark_blue if _display_current > 0 else _gray,
                })
                _render_summary_card_row(_glance_row1)

                _glance_row2 = []
                if _headline_pre <= 0:
                    _glance_row2.extend([
                        {"value": "--", "label": _lift_label, "sub": "No pre-launch baseline", "color": _gray},
                        {"value": "--", "label": "Lift per Unit", "sub": "No pre-launch baseline", "color": _gray},
                    ])
                else:
                    _lift_sign = "+" if _active_lift > 0 else ""
                    _lift_sub = (
                        f"${_comp_post_avg_sum:,.0f} avg - ${_headline_pre:,.0f} pre"
                        if (_use_avg and _comp_post_avg_sum > 0)
                        else f"${_headline_current:,.0f} - ${_headline_pre:,.0f} ({_simple_pct:+.1f}%)"
                    )
                    _glance_row2.append({
                        "value": f"{_lift_sign}${_active_lift:,.0f}/mo",
                        "label": _lift_label,
                        "sub": _lift_sub,
                        "color": _teal_blue if _active_lift >= 0 else _orange,
                    })
                    _lpu_sign = "+" if _lift_per_unit > 0 else ""
                    _glance_row2.append({
                        "value": f"{_lpu_sign}${_lift_per_unit:,.2f}/unit/mo" if _headline_units > 0 else "--",
                        "label": "Lift per Unit",
                        "sub": f"${_active_lift:,.0f} / {_headline_units:,} units" if _headline_units > 0 else "Unit counts unavailable",
                        "color": _teal_blue if _lift_per_unit >= 0 else _orange,
                    })
                _glance_row2.append({
                    "value": _format_summary_large_currency(_asset_value_impact) if _asset_value_impact > 0 else "--",
                    "label": "Est. Asset Value Impact",
                    "sub": f"${_active_lift:,.0f}/mo x 12 / 5% cap rate" if _asset_value_impact > 0 else "Requires positive lift",
                    "color": _teal_blue if _asset_value_impact > 0 else _gray,
                })
                _render_summary_card_row(_glance_row2, row_label="Actuals", row_label_color=_teal_blue)

                _opportunity_sub = (
                    f"{_found_residents:,} residents not paying pet fees"
                    if _found_residents > 0
                    else ("No missed pet-fee revenue detected" if _has_missing_rent_data else "Enable uncollected pet rent on Charts tab")
                )
                _glance_row3 = [
                    {
                        "value": f"${_found_mo:,.0f}/mo" if _found_mo > 0 else "--",
                        "label": "Pet Revenue Found",
                        "sub": _opportunity_sub,
                        "color": _orange if _found_mo > 0 else _gray,
                    },
                    {
                        "value": f"${_found_per_unit:,.2f}/unit/mo" if _found_per_unit > 0 else "--",
                        "label": "Found per Unit",
                        "sub": (
                            f"${_found_mo:,.0f} / {_units_for_found:,} units | Combined: ${_combined_per_unit:,.2f}/unit/mo"
                            if _found_per_unit > 0 else "Requires found revenue + unit counts"
                        ),
                        "color": _orange if _found_per_unit > 0 else _gray,
                    },
                    {
                        "value": _format_summary_large_currency(_found_asset_value) if _found_asset_value > 0 else "--",
                        "label": "Est. Value Impact (Found)",
                        "sub": f"${_found_mo:,.0f}/mo x 12 / 5% cap rate" if _found_asset_value > 0 else "Requires found revenue",
                        "color": _orange if _found_asset_value > 0 else _gray,
                    },
                ]
                _render_summary_card_row(_glance_row3, row_label="Opportunity", row_label_color=_orange)

                _adopt_color = _green if (_avg_adoption is not None and _avg_adoption >= 70) else (_orange if _avg_adoption is not None else _gray)
                _context_row = [
                    {
                        "value": f"{_t2_tenants:,}" if _has_missing_rent_data else "--",
                        "label": "Tenants Not Paying",
                        "sub": f"Confirmed missing pet rent across {_t2_props} properties" if _has_missing_rent_data and _t2_tenants > 0 else ("None found" if _has_missing_rent_data else "Enable on Charts tab"),
                        "color": _orange if _t2_tenants > 0 else (_green if _has_missing_rent_data else _gray),
                    },
                    {
                        "value": f"{_su_total_profiles:,}" if _su_total_profiles > 0 else "0",
                        "label": "Suspected Undisclosed",
                        "sub": f"~${_su_current_mo:,.0f}/mo potential revenue" if _su_total_profiles > 0 else "No signals detected",
                        "color": _orange if _su_total_profiles > 0 else _green,
                    },
                    {
                        "value": f"{_avg_adoption:.1f}%" if _avg_adoption is not None else "--",
                        "label": f"Average {_adopt_type_label} Adoption",
                        "sub": f"Across {_n_proj_props} properties" if _avg_adoption is not None else "Enable adoption overlay on Charts tab",
                        "color": _adopt_color,
                    },
                ]
                _render_summary_card_row(_context_row, row_label="Portfolio Health", row_label_color=_dark_blue)

                _summary_parts = []
                if n_props_with_data > 0:
                    if _total_units > 0:
                        _summary_parts.append(
                            f'Data pulled for <strong>{n_props_with_data:,} properties with charge data</strong>, '
                            f'with <strong>{_total_units:,} units represented</strong>. '
                        )
                    else:
                        _summary_parts.append(
                            f'Data pulled for <strong>{n_props_with_data:,} properties with charge data</strong>. '
                        )
                if _headline_pre > 0:
                    _summary_parts.append(
                        f'Pre-PS baseline was <strong>${_headline_pre:,.0f}/mo</strong> across {len(_comparable)} comparable properties. '
                    )
                    _summary_parts.append(
                        f'Current same-property revenue is <strong>${_headline_current:,.0f}/mo</strong>, producing '
                        f'<strong>{"+" if _active_lift > 0 else ""}${_active_lift:,.0f}/mo</strong> in {_lift_label.lower()}.'
                    )
                if _found_mo > 0:
                    _summary_parts.append(
                        f' Pet revenue found adds <strong>${_found_mo:,.0f}/mo</strong> from '
                        f'<strong>{_t2_tenants:,}</strong> confirmed tenants not paying and '
                        f'<strong>{_su_total_profiles:,}</strong> suspected undisclosed profiles.'
                    )
                if _avg_adoption is not None:
                    _summary_parts.append(
                        f' Selected adoption metric: <strong>{_avg_adoption:.1f}% average {_adopt_type_label.lower()} adoption</strong>.'
                    )
                if _summary_parts:
                    st.markdown(
                        f'<p style="font-family:Poppins,Arial,sans-serif;font-size:13px;color:#636569;'
                        f'text-align:center;margin:12px 0 24px 0;line-height:1.5">'
                        f'{"".join(_summary_parts)}</p>',
                        unsafe_allow_html=True,
                    )
                else:
                    st.markdown('<div style="margin-bottom:24px"></div>', unsafe_allow_html=True)

                # ── Detailed metrics (collapsed) ──
                _quick_rows = []
                _quick_rows.append({"Metric": "Comparable Units" if _has_comparable else "Live Units", "Value": f"{_headline_units:,}" if _headline_units else "--", "Period": f"{len(_comparable)} comparable properties" if _has_comparable else f"across {n_props_with_data} properties"})
                _quick_rows.append({"Metric": "Pre-PS Baseline", "Value": f"${_headline_pre:,.0f}/mo", "Period": f"across {len(_comparable)} properties"})
                _quick_rows.append({"Metric": _display_current_label, "Value": f"${_display_current:,.0f}/mo", "Period": _display_current_sub})
                _quick_rows.append({"Metric": _lift_label, "Value": f"{('+' if _active_lift > 0 else '')}${_active_lift:,.0f}/mo", "Period": f"across {len(_comparable)} properties" if _has_comparable else _latest_str})
                if _headline_units > 0:
                    _quick_rows.append({"Metric": "Lift per Unit", "Value": f"${_lift_per_unit:,.2f}/unit/mo", "Period": f"{_headline_units:,} units"})
                if _asset_value_impact > 0:
                    _quick_rows.append({"Metric": "Est. Asset Value Impact", "Value": _format_summary_large_currency(_asset_value_impact), "Period": "at 5% cap rate"})
                _quick_rows.append({"Metric": "Pet Revenue Found", "Value": f"${_found_mo:,.0f}/mo", "Period": f"{_found_residents:,} residents not paying pet fees" if _found_residents else ""})
                if _found_per_unit > 0:
                    _quick_rows.append({"Metric": "Found per Unit", "Value": f"${_found_per_unit:,.2f}/unit/mo", "Period": f"Combined: ${_combined_per_unit:,.2f}/unit/mo"})
                if _t2_tenants > 0 or _has_missing_rent_data:
                    _quick_rows.append({"Metric": "Tenants Not Paying", "Value": f"{_t2_tenants:,}", "Period": f"across {_t2_props} properties"})
                _quick_rows.append({"Metric": "Suspected Undisclosed", "Value": f"{_su_total_profiles:,}", "Period": f"~${_su_current_mo:,.0f}/mo potential" if _su_total_profiles > 0 else "No signals detected"})
                if _t3_adoption is not None:
                    _quick_rows.append({"Metric": f"Average {_adopt_type_label} Adoption", "Value": f"{_t3_adoption:.1f}%", "Period": f"across {_n_proj_props} properties"})
                _quick_rows.append({"Metric": "Properties with Launch Date", "Value": f"{n_with_launch} of {n_props_total}", "Period": ""})
                _quick_rows.append({"Metric": "Properties with Charge Data", "Value": f"{n_props_with_data} of {n_props_total}", "Period": ""})

                with st.expander("Detailed metrics breakdown", expanded=False):
                    _render_table(pd.DataFrame(_quick_rows))

                # ═══════════════════════════════════════════════════════════════
                #  PROPERTY MANAGERS & EMAIL
                # ═══════════════════════════════════════════════════════════════

                st.markdown("---")

                st.markdown(
                    '<p style="font-family:Lora,Georgia,serif;font-size:20px;font-weight:700;'
                    'color:#1F2257;margin:0 0 4px 0">Property Managers</p>'
                    '<p style="font-family:Poppins,Arial,sans-serif;font-size:13px;color:#636569;'
                    'margin:0 0 16px 0">Load property manager contacts to send a compliance reminder.</p>',
                    unsafe_allow_html=True,
                )

                # Fetch PM emails
                if "pm_emails_cache" not in st.session_state:
                    st.session_state.pm_emails_cache = None

                _pm_col1, _pm_col2 = st.columns([3, 1])
                with _pm_col2:
                    if st.button("Load Property Managers", use_container_width=True,
                                 key="load_pms_btn",
                                 help="Query Snowflake for property manager email addresses"):
                        with st.spinner("Fetching property manager emails..."):
                            _sel_ancestry = st.session_state.get("selected_ancestry_id")
                            _sel_parent = st.session_state.get("selected_parent_company")
                            pm_rows = fetch_property_manager_emails(
                                _prop_ids,
                                ancestry_id=_sel_ancestry,
                                parent_company_name=_sel_parent,
                            )
                            st.session_state.pm_emails_cache = pm_rows

                pm_rows = st.session_state.pm_emails_cache

                with _pm_col1:
                    if pm_rows is None:
                        st.markdown(
                            '<div style="background:#FAFAF8;border:1px solid #E8E4DA;border-radius:8px;'
                            'padding:14px 18px;font-family:Poppins,Arial,sans-serif;font-size:13px;color:#636569">'
                            'Click <strong>Load Property Managers</strong> to find email addresses '
                            'for all PMs across your selected properties.</div>',
                            unsafe_allow_html=True,
                        )
                    elif len(pm_rows) == 0:
                        st.warning("No property manager emails found for the selected properties.")
                    else:
                        unique_emails = sorted(set(
                            r['PM_EMAIL'] for r in pm_rows
                            if r.get('PM_EMAIL') and r['PM_EMAIL'].strip()
                        ))
                        n_properties_covered = len(set(r.get('PROPERTY_ID') for r in pm_rows if r.get('PROPERTY_ID')))

                        st.markdown(
                            f'<div style="background:#FAFAF8;border:1px solid #E8E4DA;border-radius:8px;'
                            f'padding:14px 18px;font-family:Poppins,Arial,sans-serif;font-size:13px;color:#4F5155">'
                            f'Found <strong>{len(unique_emails)}</strong> property managers '
                            f'across <strong>{n_properties_covered}</strong> properties.</div>',
                            unsafe_allow_html=True,
                        )

                # Show email action if PMs are loaded
                if pm_rows and len(pm_rows) > 0:
                    unique_emails = sorted(set(
                        r['PM_EMAIL'] for r in pm_rows
                        if r.get('PM_EMAIL') and r['PM_EMAIL'].strip()
                    ))

                    if unique_emails:
                        st.markdown("")

                        # Build email content
                        _email_subject = f"PetScreening Compliance Reminder — {_label}"
                        _email_body_parts = [
                            f"Hi team,",
                            f"",
                            f"Quick reminder: all new leases and lease renewals require a completed PetScreening profile before move-in.",
                            f"",
                            f"This ensures we're capturing pet fee revenue for every pet-owning resident.",
                        ]
                        if _comparable and _agg_diff_mo > 0:
                            _email_body_parts.append(
                                f"Properties with high PetScreening adoption are seeing +${_agg_diff_mo:,.0f}/mo more in pet fee revenue."
                            )
                        if _avg_adoption is not None:
                            _email_body_parts.append(f"Current portfolio adoption: {_avg_adoption:.1f}%")
                        if _mr_total_profiles > 0 and _mr_current_mo > 0:
                            _email_body_parts.append(
                                f"We've identified {_mr_total_profiles} tenants with completed PetScreening screening who aren't being charged pet rent "
                                f"(~${_mr_current_mo:,.0f}/mo in uncollected revenue)."
                            )
                        if _su_total_profiles > 0 and _su_current_mo > 0:
                            _email_body_parts.append(
                                f"Additionally, {_su_total_profiles} tenants show signals of having undisclosed pets "
                                f"(~${_su_current_mo:,.0f}/mo in potential additional revenue)."
                            )
                        _email_body_parts += [
                            f"",
                            f"Please ensure your teams are following up with all new and renewing residents.",
                            f"",
                            f"If you have questions about the PetScreening process, please reach out.",
                            f"",
                            f"Thank you!",
                        ]
                        _email_body = "\n".join(_email_body_parts)

                        # Determine mailto approach based on email count
                        _MAX_MAILTO_EMAILS = 30  # safe limit for mailto: URL length
                        _use_mailto = len(unique_emails) <= _MAX_MAILTO_EMAILS

                        if _use_mailto:
                            bcc_str = ",".join(unique_emails)
                            mailto_url = (
                                f"mailto:?bcc={urllib.parse.quote(bcc_str)}"
                                f"&subject={urllib.parse.quote(_email_subject)}"
                                f"&body={urllib.parse.quote(_email_body)}"
                            )
                            st.markdown(
                                f'<a href="{mailto_url}" target="_blank" style="'
                                f'display:inline-block;background:#1F2257;color:#FFFFFF;'
                                f'font-family:Poppins,Arial,sans-serif;font-size:15px;font-weight:600;'
                                f'padding:14px 28px;border-radius:8px;text-decoration:none;'
                                f'letter-spacing:0.3px;transition:background 0.2s">'
                                f'Email All Property Managers ({len(unique_emails)})'
                                f'</a>'
                                f'<p style="font-family:Poppins,Arial,sans-serif;font-size:12px;'
                                f'color:#AFB2B3;margin:8px 0 0 0">'
                                f'Opens your email client with all PMs in BCC. You still have to click Send.</p>',
                                unsafe_allow_html=True,
                            )
                        else:
                            # Too many emails for mailto: — provide copy-to-clipboard
                            st.markdown(
                                f'<div style="background:#FEF9EF;border:1px solid #E2AB58;border-radius:8px;'
                                f'padding:14px 18px;font-family:Poppins,Arial,sans-serif;font-size:13px;color:#4F5155;'
                                f'margin-bottom:12px">'
                                f'<strong>{len(unique_emails)} property managers</strong> is too many for a '
                                f'mailto: link (browsers limit URL length). Use the fields below to copy into your '
                                f'email client.</div>',
                                unsafe_allow_html=True,
                            )

                            _ec1, _ec2 = st.columns(2)
                            with _ec1:
                                st.text_area(
                                    "BCC List (copy & paste into your email BCC field)",
                                    value="; ".join(unique_emails),
                                    height=120,
                                    key="pm_bcc_list",
                                    help="Select all, copy, and paste into your email's BCC field.",
                                )
                            with _ec2:
                                st.text_area(
                                    "Subject",
                                    value=_email_subject,
                                    height=68,
                                    key="pm_email_subject",
                                )

                            st.text_area(
                                "Email Body (copy & paste)",
                                value=_email_body,
                                height=200,
                                key="pm_email_body",
                            )

                        # PM details expander
                        with st.expander(f"View all {len(unique_emails)} property manager emails", expanded=False):
                            # Group by property
                            _pm_by_prop = defaultdict(list)
                            for r in pm_rows:
                                pname = r.get('PROPERTY_NAME', 'Unknown')
                                email = r.get('PM_EMAIL', '')
                                if email and email.strip():
                                    _pm_by_prop[pname].append(email)

                            _pm_table_rows = []
                            for pname in sorted(_pm_by_prop.keys()):
                                emails = sorted(set(_pm_by_prop[pname]))
                                short = pname.split(" - ", 1)[-1] if " - " in pname else pname
                                _pm_table_rows.append({
                                    "Property": short,
                                    "Property Managers": ", ".join(emails),
                                    "Count": len(emails),
                                })
                            if _pm_table_rows:
                                _render_table(pd.DataFrame(_pm_table_rows))

                # ═══════════════════════════════════════════════════════════════
                #  DOWNLOAD IMPACT ANALYSIS
                # ═══════════════════════════════════════════════════════════════

                st.markdown("---")
                # ═══════════════════════════════════════════════════════
                #  PET RENT PRICING — what this portfolio charges (own data)
                # ═══════════════════════════════════════════════════════
                _benchmarks = None
                _bm_key = f"benchmarks_{hash(tuple(sorted(str(p) for p in _prop_ids)))}_{hash(tuple(sorted(selected_codes or [])))}"
                if _bm_key not in st.session_state:
                    try:
                        from benchmarks import build_pet_rent_pricing
                        _bm_pet = df[df['charge_code'].isin(selected_codes)].copy()
                        if not _bm_pet.empty:
                            _bm_pet['_amt'] = pd.to_numeric(_bm_pet['charge_amount'], errors='coerce').fillna(0)
                            _bm_pet['_from'] = _bm_pet['charge_from_date'].apply(parse_date)
                            _bm_pet['_to'] = _bm_pet['charge_to_date'].apply(parse_date)
                            _bm_class = _classify_pet_charge_codes(
                                _bm_pet, st.session_state.get("pmc_system", "yardi"),
                                st.session_state.get("charge_type_overrides", {}),
                                system_by_pid=st.session_state.get("property_system_by_pid"),
                            )
                            _bm_rec_by_prop, _ = _estimate_property_fees(_bm_pet, _bm_class)
                            _bm_recent = _recent_paying_charges(_bm_pet)
                            _bm_rec_codes = {c for (_pn, c), cls in _bm_class.items() if cls == 'recurring'}
                            _bm_paying = _bm_recent[_bm_recent['charge_code'].isin(_bm_rec_codes)]
                            _bm_n_paying = (
                                _bm_paying.groupby(['property_id', 'tenant_code']).ngroups
                                if not _bm_paying.empty else 0
                            )
                            _bm_pricing = build_pet_rent_pricing(_bm_rec_by_prop, _bm_n_paying)
                            st.session_state[_bm_key] = {"pricing": _bm_pricing} if _bm_pricing else None
                        else:
                            st.session_state[_bm_key] = None
                    except Exception as _bm_exc:  # noqa: BLE001
                        st.session_state[_bm_key] = None
                        st.caption(f"Pricing summary unavailable: {str(_bm_exc)[:150]}")
                _benchmarks = st.session_state.get(_bm_key)

                if _benchmarks and _benchmarks.get("pricing"):
                    _bmp = _benchmarks["pricing"]
                    st.markdown(
                        '<p style="font-family:Lora,Georgia,serif;font-size:20px;font-weight:700;'
                        'color:#1F2257;margin:12px 0 4px 0">Pet Rent Pricing</p>'
                        '<p style="font-family:Poppins,Arial,sans-serif;font-size:13px;color:#636569;'
                        'margin:0 0 8px 0">What this portfolio charges for recurring pet rent, from its own data.</p>',
                        unsafe_allow_html=True,
                    )
                    if _bmp["n_props"] > 1:
                        _f1, _f2, _f3 = st.columns(3)
                        _f1.metric("Typical pet rent", f"${_bmp['median']:,.0f}/mo",
                                   help=f"Median across {_bmp['n_props']:,} properties with recurring pet rent.")
                        _f2.metric("Full range", f"${_bmp['min']:,.0f}–${_bmp['max']:,.0f}",
                                   help="Lowest to highest property typical fee.")
                        _f3.metric("Middle 50%", f"${_bmp['p25']:,.0f}–${_bmp['p75']:,.0f}",
                                   help="25th–75th percentile of property typical fees.")
                        if _bmp["max"] - _bmp["min"] > 10:
                            st.caption(
                                f"Fees span ${_bmp['min']:,.0f}–${_bmp['max']:,.0f}/mo across "
                                f"{_bmp['n_props']:,} properties. A wide spread can be a pricing "
                                f"opportunity — low-end properties may support rates closer to the "
                                f"portfolio median of ${_bmp['median']:,.0f}/mo."
                            )
                    else:
                        st.metric("Typical pet rent", f"${_bmp['median']:,.0f}/mo",
                                  help="Recurring pet rent at this property.")
                    st.caption("Basis: recurring pet-rent charges pulled live from your property "
                               "management system for this report.")
                    st.divider()

                st.markdown(
                    '<p style="font-family:Lora,Georgia,serif;font-size:20px;font-weight:700;'
                    'color:#1F2257;margin:0 0 4px 0">Download Impact Analysis</p>'
                    '<p style="font-family:Poppins,Arial,sans-serif;font-size:13px;color:#636569;'
                    'margin:0 0 16px 0">Generate a branded report to share with stakeholders.</p>',
                    unsafe_allow_html=True,
                )

                _include_pm_in_report = False
                _pm_cache = st.session_state.get("pm_emails_cache")
                if _pm_cache and len(_pm_cache) > 0:
                    _include_pm_in_report = st.checkbox(
                        "Include property manager emails in report",
                        value=False,
                        key="include_pm_report",
                    )
                _use_avg_lift = st.checkbox(
                    "Use Average Monthly Lift (for student housing / seasonal portfolios)",
                    value=False,
                    key="use_avg_lift_toggle",
                    help="Default uses simple lift (current - pre). Check this for portfolios "
                         "where current-month comparisons distort the story due to seasonality.",
                )
                _include_prop_charts = st.checkbox(
                    "Include individual property charts in the PDF (appendix)",
                    value=False,
                    key="include_prop_charts_pdf",
                    help="Appends the per-property before/after fee collection charts "
                         "(same as the Fee Collection tab) for every comparable property, "
                         "sorted by monthly lift. Adds ~1 page per 4 properties.",
                )

                _exec_col1, _exec_col2, _exec_col3 = st.columns(3)

                # ── Generate button ──
                with _exec_col1:
                    if st.button("Generate Report", use_container_width=True,
                                 key="gen_exec_btn",
                                 help="Build branded PDF and HTML reports"):
                        _pm_for_report = _pm_cache if _include_pm_in_report else None

                        # Snapshot metrics (current + previous) — used for the
                        # PDF "Progress Since Last Report" section and the
                        # in-app delta table further below.
                        _snap_label = f"{st.session_state.get('pmc_system', '')}::{_label}"
                        _prev_snap, _prev_ts = (None, None)
                        try:
                            from fetch_cache import load_last_report_snapshot
                            _prev_snap, _prev_ts = load_last_report_snapshot(_snap_label)
                        except Exception:
                            pass
                        _simple_lift_now = _current_monthly_rev - _pre_baseline_total if _pre_baseline_total > 0 else 0
                        _now_snap = {
                            "current_monthly_rev": float(_current_monthly_rev or 0),
                            "monthly_lift": float((_agg_diff_mo if _use_avg_lift else _simple_lift_now) or 0),
                            "missing_tenants": int(_mr_total_profiles or 0),
                            "missing_monthly": float(_mr_current_mo or 0),
                            "suspected_tenants": int(_su_total_profiles or 0),
                            "avg_adoption": float(_avg_adoption) if _avg_adoption is not None else None,
                            "comparable_count": int(len(_comparable) if _comparable else 0),
                        }

                        # Generate PDF
                        # Build monthly revenue series for PDF chart
                        _pdf_monthly_series = []
                        if _months and _monthly_by_prop:
                            _raw_series = []
                            for _m in _months:
                                _m_total = sum(
                                    _monthly_by_prop.get(p, {}).get(_m, 0)
                                    for p in _monthly_by_prop
                                )
                                _raw_series.append((_m, _m_total))
                            # Trim leading and trailing zero-revenue months so the
                            # chart only shows the range with actual data
                            _first_nonzero = None
                            _last_nonzero = None
                            for _idx, (_m, _v) in enumerate(_raw_series):
                                if _v > 0:
                                    if _first_nonzero is None:
                                        _first_nonzero = _idx
                                    _last_nonzero = _idx
                            if _first_nonzero is not None and _last_nonzero is not None:
                                _trimmed = _raw_series[_first_nonzero:_last_nonzero + 1]
                            else:
                                _trimmed = _raw_series
                            # Fill interior zero months with linear interpolation
                            # so the line chart doesn't have misleading dips to $0
                            _pdf_monthly_series = list(_trimmed)
                            for _i in range(1, len(_pdf_monthly_series) - 1):
                                if _pdf_monthly_series[_i][1] == 0:
                                    # Find previous and next non-zero values
                                    _prev_val = None
                                    for _j in range(_i - 1, -1, -1):
                                        if _pdf_monthly_series[_j][1] > 0:
                                            _prev_val = _pdf_monthly_series[_j][1]
                                            _prev_idx = _j
                                            break
                                    _next_val = None
                                    for _j in range(_i + 1, len(_pdf_monthly_series)):
                                        if _pdf_monthly_series[_j][1] > 0:
                                            _next_val = _pdf_monthly_series[_j][1]
                                            _next_idx = _j
                                            break
                                    if _prev_val is not None and _next_val is not None:
                                        # Linear interpolation
                                        _span = _next_idx - _prev_idx
                                        _frac = (_i - _prev_idx) / _span
                                        _interp = _prev_val + (_next_val - _prev_val) * _frac
                                        _pdf_monthly_series[_i] = (_pdf_monthly_series[_i][0], _interp)

                        # Compute comparable-only latest observed revenue (apples-to-apples)
                        _comp_current_rev_latest = 0
                        if _comparable:
                            _comp_latest_month = _latest_observed_revenue_month(_monthly_by_prop, _months, _comparable.keys())
                            if _comp_latest_month:
                                _comp_current_rev_latest = sum(
                                    _monthly_by_prop.get(p, {}).get(_comp_latest_month, 0)
                                    for p in _comparable
                                )

                        pdf_bytes = generate_tranche_pdf(
                            label=_label,
                            today_str=_today_str,
                            pre_baseline=_pre_baseline_total,
                            comparable_count=len(_comparable),
                            t1_mo=_t1_mo, t1_total=_t1_total, t1_pct=_t1_pct, t1_months=_t1_months,
                            t2_tenants=_t2_tenants, t2_mo=_t2_mo, t2_props=_t2_props,
                            t1_t2_combined=_t1_t2_combined, t1_t2_pct=_t1_t2_pct,
                            t3_adoption=_t3_adoption, t3_additional=_t3_additional,
                            t3_total_impact=_t3_total_impact, t3_pct=_t3_pct,
                            adopt_type_label=_adopt_type_label,
                            current_monthly_rev=_current_monthly_rev,
                            n_props_total=n_props_total,
                            n_with_launch=n_with_launch,
                            n_props_with_data=n_props_with_data,
                            include_pm=_include_pm_in_report,
                            pm_rows=_pm_for_report,
                            su_total_profiles=_su_total_profiles,
                            su_current_mo=_su_current_mo,
                            total_projected=_total_projected,
                            missing_rent_data=_mr_data,
                            total_units=st.session_state.get("_total_units", 0),
                            comparable_data=_comparable if _comparable else None,
                            total_portfolio_units=st.session_state.get("_total_portfolio_units", 0),
                            property_doors=st.session_state.get("_property_doors_by_name", {}),
                            monthly_revenue_series=_pdf_monthly_series if _pdf_monthly_series else None,
                            comparable_current_rev=_comp_current_rev_latest,
                            pmc_system=st.session_state.get("pmc_system", "yardi"),
                            use_avg_lift=_use_avg_lift,
                            monthly_by_prop=_monthly_by_prop,
                            latest_month=_latest_month,
                            selected_charge_codes=selected_codes,
                            avg_pet_fee=_avg_fee,
                            include_property_charts=_include_prop_charts,
                            benchmarks=_benchmarks,
                            prev_snapshot=_prev_snap,
                            prev_snapshot_ts=_prev_ts,
                            current_snapshot=_now_snap,
                            realpage_prop_count=sum(
                                1 for v in (st.session_state.get("property_system_by_pid") or {}).values()
                                if v == "real_page"
                            ) if st.session_state.get("pmc_system") == "all" else (
                                len(st.session_state.get("property_ids", []))
                                if st.session_state.get("pmc_system") == "real_page" else 0
                            ),
                        )
                        st.session_state["exec_pdf"] = pdf_bytes

                        # Generate HTML (existing)
                        _exec_email_subject = f"PetScreening Compliance Reminder — {_label}"
                        _exec_email_body_parts = [
                            "Hi team,", "",
                            "Quick reminder: all new leases and lease renewals require a completed PetScreening profile before move-in.",
                            "", "This ensures we're capturing pet fee revenue for every pet-owning resident.",
                        ]
                        if _comparable and _agg_diff_mo > 0:
                            _exec_email_body_parts.append(f"Properties with high PetScreening adoption are seeing +${_agg_diff_mo:,.0f}/mo more in pet fee revenue.")
                        if _avg_adoption is not None:
                            _exec_email_body_parts.append(f"Current portfolio adoption: {_avg_adoption:.1f}%")
                        if _mr_total_profiles > 0 and _mr_current_mo > 0:
                            _exec_email_body_parts.append(f"We've identified {_mr_total_profiles} tenants with completed PetScreening screening who aren't being charged pet rent (~${_mr_current_mo:,.0f}/mo in uncollected revenue).")
                        _exec_email_body_parts += ["", "Please ensure your teams are following up with all new and renewing residents.", "", "Thank you!"]
                        _exec_email_body = "\n".join(_exec_email_body_parts)

                        # Compute toggle-aware values for HTML
                        _html_simple_lift = _current_monthly_rev - _pre_baseline_total if _pre_baseline_total > 0 else 0
                        _html_lift = _agg_diff_mo if _use_avg_lift else _html_simple_lift
                        _html_comp_post_avg = sum(
                            a.get("post_recent_avg", a.get("post_monthly_avg", 0))
                            for a in _comparable.values()
                        ) if _comparable else 0
                        _html_display_rev = _html_comp_post_avg if (_use_avg_lift and _html_comp_post_avg > 0) else _current_monthly_rev

                        exec_html = generate_exec_summary_html(
                            label=_label, rev_change_mo=_html_lift, rev_change_total=_agg_diff,
                            avg_adoption=_avg_adoption, adopt_type_label=_adopt_type_label,
                            total_projected=_total_projected, total_additional=_total_additional,
                            n_proj_props=_n_proj_props, mr_total_profiles=_mr_total_profiles,
                            mr_current_mo=_mr_current_mo, comparable_count=len(_comparable),
                            current_monthly_rev=_current_monthly_rev, n_props_total=n_props_total,
                            n_with_launch=len(_launch_in_data_wn), quick_rows=_quick_rows,
                            pm_rows=_pm_for_report,
                            email_subject=_exec_email_subject, email_body=_exec_email_body,
                            su_total_profiles=_su_total_profiles, su_current_mo=_su_current_mo,
                            use_avg_lift=_use_avg_lift, display_rev=_html_display_rev,
                        )
                        st.session_state["exec_html"] = exec_html
                        st.session_state["exec_label"] = _label

                        # Also build the Missing / Suspected CSVs here so all
                        # three outputs (PDF + both CSVs) download from one
                        # place — no tab-hopping.
                        with st.spinner("Building Missing Pet Rent CSV..."):
                            try:
                                _csv_missing_df = generate_missing_pet_rent_report(df, selected_codes, _prop_ids)
                                st.session_state["exec_missing_csv"] = (
                                    _csv_missing_df.to_csv(index=False)
                                    if _csv_missing_df is not None and not _csv_missing_df.empty else ""
                                )
                            except Exception as _csv_err:
                                st.session_state["exec_missing_csv"] = ""
                                st.warning(f"Missing Pet Rent CSV failed: {str(_csv_err)[:200]}")
                        with st.spinner("Building Suspected Undisclosed CSV..."):
                            try:
                                _csv_susp_df = generate_suspected_undisclosed_report(df, selected_codes, _prop_ids)
                                st.session_state["exec_suspected_csv"] = (
                                    _csv_susp_df.to_csv(index=False)
                                    if _csv_susp_df is not None and not _csv_susp_df.empty else ""
                                )
                            except Exception as _csv_err:
                                st.session_state["exec_suspected_csv"] = ""
                                st.warning(f"Suspected Undisclosed CSV failed: {str(_csv_err)[:200]}")

                        # ── "Since last report" deltas + snapshot ──
                        try:
                            from fetch_cache import save_report_snapshot
                            if _prev_snap:
                                _delta_specs = [
                                    ("current_monthly_rev", "Current revenue", "${:,.0f}/mo"),
                                    ("monthly_lift", "Monthly lift", "${:,.0f}/mo"),
                                    ("missing_tenants", "Missing pet rent tenants", "{:,.0f}"),
                                    ("missing_monthly", "Uncollected revenue", "${:,.0f}/mo"),
                                    ("suspected_tenants", "Suspected undisclosed", "{:,.0f}"),
                                    ("avg_adoption", "Avg adoption", "{:.1f}%"),
                                ]
                                _delta_rows = []
                                for _dk, _dl, _df_fmt in _delta_specs:
                                    _new_v = _now_snap.get(_dk)
                                    _old_v = _prev_snap.get(_dk)
                                    if _new_v is None or _old_v is None:
                                        continue
                                    try:
                                        _old_v = float(_old_v)
                                    except (TypeError, ValueError):
                                        continue
                                    _d = _new_v - _old_v
                                    _delta_rows.append({
                                        "Metric": _dl,
                                        "Last report": _df_fmt.format(_old_v),
                                        "Now": _df_fmt.format(_new_v),
                                        "Change": ("+" if _d >= 0 else "") + _df_fmt.format(_d).replace("$-", "-$"),
                                    })
                                if _delta_rows:
                                    st.markdown(
                                        f"**Since last report ({str(_prev_ts)[:10]}):**"
                                    )
                                    _render_table(pd.DataFrame(_delta_rows))
                            save_report_snapshot(_snap_label, _now_snap)
                        except Exception as _snap_err:
                            st.caption(f"Report deltas unavailable: {str(_snap_err)[:120]}")

                        # ── Flag cohort for the reclaim-rate OKR ──
                        # Persist WHO was flagged (with reason) so the next
                        # quarter's run can measure how many started paying.
                        try:
                            from fetch_cache import save_flagged_tenants
                            _flag_rows = []
                            for _fpn, _fv in (_mr_data or {}).items():
                                _fpid = _pid_lookup.get(_fpn)
                                for _ft in _fv.get("missing_tenants", []):
                                    _flag_rows.append({
                                        "property_id": _fpid,
                                        "property_name": _fpn,
                                        "email": str(_ft.get("email") or "").lower().strip(),
                                        "name": _ft.get("name", ""),
                                        "reason": "missing_pet_rent",
                                        "est_fee_mo": float(_fv.get("avg_recurring", 0) or 0),
                                    })
                            for _fpn, _fv in (_su_data or {}).items():
                                _fpid = _pid_lookup.get(_fpn)
                                for _ft in _fv.get("missing_tenants", []):
                                    _flag_rows.append({
                                        "property_id": _fpid,
                                        "property_name": _fpn,
                                        "email": str(_ft.get("email") or "").lower().strip(),
                                        "name": _ft.get("name", ""),
                                        "reason": _ft.get("suspected_reason", "Other suspected"),
                                        "est_fee_mo": float(_fv.get("avg_recurring", 0) or 0),
                                    })
                            if _flag_rows:
                                save_flagged_tenants(
                                    _snap_label,
                                    st.session_state.get("pmc_system", ""),
                                    _prop_ids,
                                    _flag_rows,
                                )
                                st.caption(
                                    f"Reclaim tracking: {len(_flag_rows)} flagged tenants "
                                    f"saved as this run's cohort."
                                )
                        except Exception as _flag_err:
                            st.caption(f"Flag cohort not saved: {str(_flag_err)[:120]}")

                        # ── Auto-append to the analytics snapshot tables ──
                        # (RAW.MISC.PET_VALUE_*) — every Generate Report run
                        # lands in Snowflake, same pattern as the fetch cache.
                        try:
                            import snapshot_tables as _snap_tbl
                            _sel_parent_id = str(st.session_state.get("selected_ancestry_id") or "")
                            _sel_parent_name = _label if _sel_parent_id else ""
                            _n_users = _snap_tbl.append_users_snapshot(
                                missing_df=locals().get("_csv_missing_df"),
                                suspected_df=locals().get("_csv_susp_df"),
                                pmc_system=st.session_state.get("pmc_system", ""),
                                run_label=_snap_label,
                                parent_id=_sel_parent_id,
                                parent_name=_sel_parent_name,
                            )
                            _prop_rows = _snap_tbl.build_property_rows_from_report(
                                st.session_state.get("pmc_system", ""),
                                _mr_data, _su_data, _monthly_by_prop,
                                _latest_month, _pid_lookup,
                                doors_by_name=_property_doors_by_name,
                                parent_id=_sel_parent_id,
                                parent_name=_sel_parent_name,
                            )
                            _n_props_snap = _snap_tbl.append_property_snapshot(_prop_rows)
                            if _sel_parent_id or st.session_state.get("selected_parent"):
                                _snap_tbl.append_parent_snapshot({
                                    "PMC": st.session_state.get("pmc_system", ""),
                                    "PARENT_ID": _sel_parent_id,
                                    "PARENT_NAME": _label,
                                    "PROPERTY_COUNT": n_props_total,
                                    "UNIT_COUNT": _total_units or None,
                                    "COMPARABLE_COUNT": len(_comparable) if _comparable else None,
                                    "PRE_REVENUE": _pre_baseline_total,
                                    "CURRENT_REVENUE": _current_monthly_rev,
                                    "MONTHLY_LIFT": _t1_mo,
                                    "UNCOLLECTED_TENANTS": _t2_tenants,
                                    "MISSING_MONTHLY_RENT": _t2_mo,
                                    "SUSPECTED_UNDISCLOSED": _su_total_profiles,
                                    "SUSPECTED_MONTHLY": _su_current_mo,
                                    "AVG_UNIT_ADOPTION_PCT": _avg_adoption,
                                    "PROPERTIES_WITH_LAUNCH": n_with_launch,
                                    "PROPERTIES_WITH_DATA": n_props_with_data,
                                    "TOTAL_PROPERTIES": n_props_total,
                                    "ANNUALIZED_LIFT": (_t1_mo * 12) if _t1_mo else None,
                                })
                            if _n_users or _n_props_snap:
                                st.caption(
                                    f"Snapshot tables: +{_n_users} people, "
                                    f"+{_n_props_snap} property rows appended to Snowflake."
                                )
                        except Exception as _snap_tbl_err:
                            st.caption(f"Snapshot tables not appended: {str(_snap_tbl_err)[:120]}")

                        st.toast("Reports ready for download!")

                # ── Enhanced PDF download ──
                with _exec_col2:
                    if "exec_pdf" in st.session_state and st.session_state["exec_pdf"]:
                        safe_name = st.session_state.get("exec_label", "report").replace(" ", "_").replace("/", "-")
                        st.download_button(
                            label="Enhanced PDF",
                            data=st.session_state["exec_pdf"],
                            file_name=f"PetScreening_Value_Report_{safe_name}_{datetime.now().strftime('%Y%m%d')}.pdf",
                            mime="application/pdf",
                            use_container_width=True,
                            type="primary",
                            key="dl_exec_pdf",
                            help="Full report with At a Glance, trends, appendix",
                        )
                    else:
                        st.button("Enhanced PDF", disabled=True,
                                  use_container_width=True, key="dl_pdf_disabled")

                # ── HTML download ──
                with _exec_col3:
                    if "exec_html" in st.session_state and st.session_state["exec_html"]:
                        safe_name = st.session_state.get("exec_label", "report").replace(" ", "_").replace("/", "-")
                        st.download_button(
                            label="Download HTML",
                            data=st.session_state["exec_html"],
                            file_name=f"PetScreening_Value_Report_{safe_name}_{datetime.now().strftime('%Y%m%d')}.html",
                            mime="text/html",
                            use_container_width=True,
                            type="primary",
                            key="dl_exec_html",
                        )
                    else:
                        st.button("Download HTML", disabled=True,
                                  use_container_width=True, key="dl_html_disabled")

                # ── Missing / Suspected CSVs (same outputs as the report tabs) ──
                _csv_col1, _csv_col2 = st.columns(2)
                _safe_csv_name = st.session_state.get("exec_label", "report").replace(" ", "_").replace("/", "-")
                _csv_date = datetime.now().strftime('%Y%m%d')
                with _csv_col1:
                    if st.session_state.get("exec_missing_csv"):
                        st.download_button(
                            label="Missing Pet Rent CSV",
                            data=st.session_state["exec_missing_csv"],
                            file_name=f"missing_pet_rent_{_safe_csv_name}_{_csv_date}.csv",
                            mime="text/csv",
                            use_container_width=True,
                            key="dl_exec_missing_csv",
                            help="Tenants with active household pet profiles who aren't paying the selected pet charges",
                        )
                    elif "exec_missing_csv" in st.session_state:
                        st.button("Missing Pet Rent CSV — no rows", disabled=True,
                                  use_container_width=True, key="dl_missing_csv_empty")
                    else:
                        st.button("Missing Pet Rent CSV", disabled=True,
                                  use_container_width=True, key="dl_missing_csv_disabled",
                                  help="Click 'Generate Report' first")
                with _csv_col2:
                    if st.session_state.get("exec_suspected_csv"):
                        st.download_button(
                            label="Suspected Undisclosed CSV",
                            data=st.session_state["exec_suspected_csv"],
                            file_name=f"suspected_undisclosed_{_safe_csv_name}_{_csv_date}.csv",
                            mime="text/csv",
                            use_container_width=True,
                            key="dl_exec_suspected_csv",
                            help="Residents whose PetScreening signals suggest an undisclosed pet",
                        )
                    elif "exec_suspected_csv" in st.session_state:
                        st.button("Suspected Undisclosed CSV — no rows", disabled=True,
                                  use_container_width=True, key="dl_suspected_csv_empty")
                    else:
                        st.button("Suspected Undisclosed CSV", disabled=True,
                                  use_container_width=True, key="dl_suspected_csv_disabled",
                                  help="Click 'Generate Report' first")


        # ─── TAB 3: Missing Pet Rent Report ─────────────────────────
        with tab_report:
            st.header("Missing Pet Rent Report")

            # ── Property funnel — consistent cascade ──────────────
            _report_cd = st.session_state.get("chart_data") or {}
            _report_parsed = _report_cd.get("parsed_charges", []) if isinstance(_report_cd, dict) else []
            _report_n_charges = len(_report_parsed)
            _report_n_with_charges = len(set(
                r["property_name"] for r in _report_parsed
                if r.get("amount", 0) > 0
            )) if _report_n_charges > 0 else None
            _render_property_funnel(
                n_total=st.session_state.get("total_parent_props") or None,
                n_api=st.session_state.get("api_props_count") or None,
                n_with_charges=_report_n_with_charges,
            )

            st.markdown(
                """
                This report identifies **current tenants** who have an **active household pet** screening
                in PetScreening but are **NOT paying** any of the selected pet charge codes.

                **How it works:**
                1. Uses the **live rent roll data** you just fetched (replaces the staging table)
                2. Queries Snowflake for **PetScreening tenants** with household pets (`petscreening__user_enriched`)
                3. Matches tenants by `(property_id, tenant_code)` to determine who's paying vs. who's not
                4. Pulls detailed pet info from `R_MONTHLY_EXECUTIVE_SUMMARY`
                5. Returns tenants with status **Profile_No_Rent** — has a pet, not paying

                **Selected charge codes:** """ + ", ".join(f"`{c}`" for c in selected_codes) + """
                """
            )

            st.info(
                f"**Properties in scope:** {len(st.session_state.property_ids)}  ·  "
                f"**Charge codes used:** {', '.join(selected_codes)}"
            )

            report_btn = st.button(
                "Generate Missing Pet Rent Report",
                type="primary",
                use_container_width=True,
                key="report_btn",
            )

            if report_btn:
                prop_ids = st.session_state.property_ids
                if not prop_ids:
                    st.error("No property IDs found. Please fetch rent roll data first.")
                else:
                    with st.spinner("Matching live rent roll data with PetScreening tenants... (this may take 30-60 seconds)"):
                        try:
                            report_df = generate_missing_pet_rent_report(df, selected_codes, prop_ids)

                            if report_df.empty:
                                st.success("No missing pet rent records found — all tenants with household pets are paying!")
                            else:
                                # Count unique TENANTS (by email) — report rows are per-pet,
                                # but KPIs should match the Charts/Summary tabs which count per-tenant.
                                _n_tenants = report_df['USER_EMAIL'].nunique() if 'USER_EMAIL' in report_df.columns else len(report_df)
                                n_props = report_df['PROPERTY_NAME'].nunique() if 'PROPERTY_NAME' in report_df.columns else 0
                                n_pets = report_df['PET_NAME'].nunique() if 'PET_NAME' in report_df.columns else 0

                                st.warning(
                                    f"Found **{_n_tenants:,}** tenants with active household pets "
                                    f"who are **not paying** any of the selected pet charge codes "
                                    f"({n_pets} pets across {n_props} properties)."
                                )

                                # KPI metrics — tenant count matches Charts & Summary tabs
                                rcol1, rcol2, rcol3 = st.columns(3)
                                rcol1.metric("Missing Pet Rent Tenants", f"{_n_tenants:,}")
                                rcol2.metric("Properties Affected", f"{n_props}")
                                rcol3.metric("Unique Pets", f"{n_pets}")

                                # Breakdown by property
                                st.divider()
                                st.subheader("By Property")
                                if 'PROPERTY_NAME' in report_df.columns:
                                    prop_summary = (
                                        report_df.groupby('PROPERTY_NAME')
                                        .agg(
                                            tenants=('USER_EMAIL', 'nunique'),
                                            pets=('PET_NAME', 'nunique'),
                                        )
                                        .sort_values('tenants', ascending=False)
                                        .reset_index()
                                    )
                                    prop_summary.columns = ['Property', '# Tenants Not Paying', '# Pets']
                                    _render_table(prop_summary)

                                # Full data table
                                st.divider()
                                st.subheader("Full Report Data")
                                display_cols = [
                                    c for c in [
                                        'PROPERTY_NAME', 'USER_FIRST_NAME', 'USER_LAST_NAME',
                                        'USER_EMAIL', 'PET_NAME', 'BREED', 'SPECIES',
                                        'FULL_UNIT_ADDRESS', 'PET_PROFILE_URL',
                                        'LEASE_START_DATE', 'LEASE_END_DATE',
                                        'PET_LEVEL_COMPLIANCE_STATUS',
                                        'USER_LEVEL_COMPLIANCE_STATUS',
                                        'PET_PROFILE_TYPE', 'PET_PROFILE_STATUS',
                                        'OVERALL_STATUS',
                                    ] if c in report_df.columns
                                ]
                                _render_table(
                                    report_df[display_cols] if display_cols else report_df,
                                    height=500,
                                )

                                # Download buttons
                                st.divider()
                                label = st.session_state.selection_label
                                csv_report = report_df.to_csv(index=False)
                                st.download_button(
                                    "Download Missing Pet Rent Report (CSV)",
                                    data=csv_report,
                                    file_name=f"missing_pet_rent_{label.replace(' ', '_')}.csv",
                                    mime="text/csv",
                                    key="download_csv",
                                )

                                # Also offer Excel download
                                import io
                                buffer = io.BytesIO()
                                with pd.ExcelWriter(buffer, engine='openpyxl') as writer:
                                    report_df.to_excel(writer, sheet_name='Missing Pet Rent', index=False)
                                    if 'PROPERTY_NAME' in report_df.columns:
                                        prop_summary.to_excel(writer, sheet_name='By Property', index=False)
                                buffer.seek(0)
                                st.download_button(
                                    "Download Missing Pet Rent Report (Excel)",
                                    data=buffer,
                                    file_name=f"missing_pet_rent_{label.replace(' ', '_')}.xlsx",
                                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                    key="download_xlsx",
                                )

                        except Exception as e:
                            st.error(f"Error running report: {e}")
                            with st.expander("Full error details"):
                                import traceback
                                st.code(traceback.format_exc())

            # ── Suspected Undisclosed Pets Report ──────────────────────
            st.divider()
            st.header("Suspected Undisclosed Pets Report")
            st.markdown(
                """
                This report identifies **current residents** who show **signals of having an undisclosed pet**
                but are **NOT paying** any of the selected pet charge codes.

                **Who qualifies as "suspected":**
                - **Abandoned household profile** — current household pet profile is still incomplete/unresolved
                - **Unresolved assistance request** — current assistance animal request was declined, left in draft, or returned
                
                *Conservative rule: old all-time profile history, expired profiles, recommended assistance animals, and "no pet" attestations are excluded.*

                These patterns suggest the current resident likely has a pet but hasn't completed the proper screening process.
                """
            )
            st.info(
                f"**Properties in scope:** {len(st.session_state.property_ids)}  ·  "
                f"**Charge codes used:** {', '.join(selected_codes)}"
            )

            suspected_btn = st.button(
                "Generate Suspected Undisclosed Report",
                type="primary",
                use_container_width=True,
                key="suspected_btn",
            )

            if suspected_btn:
                prop_ids = st.session_state.property_ids
                if not prop_ids:
                    st.error("No property IDs found. Please fetch rent roll data first.")
                else:
                    with st.spinner("Identifying suspected undisclosed pets... (this may take 30-60 seconds)"):
                        try:
                            suspected_df = generate_suspected_undisclosed_report(df, selected_codes, prop_ids)

                            if suspected_df.empty:
                                st.success("No suspected undisclosed pets found — great compliance!")
                            else:
                                _n_suspected_tenants = suspected_df['USER_EMAIL'].nunique() if 'USER_EMAIL' in suspected_df.columns else len(suspected_df)
                                st.warning(
                                    f"Found **{_n_suspected_tenants:,}** tenants with suspected undisclosed pets "
                                    f"who are **not paying** any of the selected pet charge codes."
                                )

                                # KPI metrics
                                scol1, scol2, scol3 = st.columns(3)
                                scol1.metric("Suspected Tenants", f"{_n_suspected_tenants:,}")
                                n_props_s = suspected_df['PROPERTY_NAME'].nunique() if 'PROPERTY_NAME' in suspected_df.columns else 0
                                scol2.metric("Properties Affected", f"{n_props_s}")
                                # Breakdown by reason
                                if 'SUSPECTED_REASON' in suspected_df.columns:
                                    reason_counts = suspected_df['SUSPECTED_REASON'].value_counts()
                                    top_reason = reason_counts.index[0] if len(reason_counts) > 0 else "—"
                                    scol3.metric("Top Reason", top_reason)

                                # Breakdown by reason
                                st.divider()
                                st.subheader("By Reason")
                                if 'SUSPECTED_REASON' in suspected_df.columns:
                                    reason_summary = (
                                        suspected_df.groupby('SUSPECTED_REASON')
                                        .agg(count=('SUSPECTED_REASON', 'size'))
                                        .sort_values('count', ascending=False)
                                        .reset_index()
                                    )
                                    reason_summary.columns = ['Reason', 'Count']
                                    _render_table(reason_summary)

                                # Breakdown by property
                                if 'PROPERTY_NAME' in suspected_df.columns:
                                    st.subheader("By Property")
                                    prop_summary_s = (
                                        suspected_df.groupby('PROPERTY_NAME')
                                        .agg(tenants=('USER_EMAIL', 'nunique') if 'USER_EMAIL' in suspected_df.columns else ('PROPERTY_NAME', 'size'))
                                        .sort_values('tenants', ascending=False)
                                        .reset_index()
                                    )
                                    prop_summary_s.columns = ['Property', 'Suspected Tenants']
                                    _render_table(prop_summary_s)

                                # Full data
                                st.subheader("Full Report Data")
                                s_display_cols = [
                                    c for c in [
                                        'PROPERTY_NAME', 'FULL_UNIT_ADDRESS',
                                        'USER_FIRST_NAME', 'USER_LAST_NAME',
                                        'USER_EMAIL', 'SUSPECTED_REASON',
                                        'LEASE_START_DATE', 'LEASE_END_DATE',
                                        'USER_PET_TYPE', 'USER_PET_STATUS',
                                        'COMPLIANCE_STATUS', 'USER_PROFILE_URL',
                                    ] if c in suspected_df.columns
                                ]
                                _render_table(
                                    suspected_df[s_display_cols] if s_display_cols else suspected_df,
                                    height=500,
                                )

                                # Download buttons
                                st.divider()
                                s_label = st.session_state.selection_label
                                csv_suspected = suspected_df.to_csv(index=False)
                                st.download_button(
                                    "Download Suspected Undisclosed Report (CSV)",
                                    data=csv_suspected,
                                    file_name=f"suspected_undisclosed_{s_label.replace(' ', '_')}.csv",
                                    mime="text/csv",
                                    key="download_suspected_csv",
                                )

                                import io as _io_s
                                buffer_s = _io_s.BytesIO()
                                with pd.ExcelWriter(buffer_s, engine='openpyxl') as writer:
                                    suspected_df.to_excel(writer, sheet_name='Suspected Undisclosed', index=False)
                                    if 'PROPERTY_NAME' in suspected_df.columns:
                                        prop_summary_s.to_excel(writer, sheet_name='By Property', index=False)
                                    if 'SUSPECTED_REASON' in suspected_df.columns:
                                        reason_summary.to_excel(writer, sheet_name='By Reason', index=False)
                                buffer_s.seek(0)
                                st.download_button(
                                    "Download Suspected Undisclosed Report (Excel)",
                                    data=buffer_s,
                                    file_name=f"suspected_undisclosed_{s_label.replace(' ', '_')}.xlsx",
                                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                    key="download_suspected_xlsx",
                                )

                        except Exception as e:
                            st.error(f"Error running suspected report: {e}")
                            with st.expander("Full error details"):
                                import traceback
                                st.code(traceback.format_exc())

        # ─── TAB 4: Documentation & SQL ──────────────────────────────
        with tab_docs:
            st.header("Documentation & SQL Reference")
            st.markdown("Everything that happens behind the scenes. Organized by **Shared Logic**, **Yardi-specific**, **Entrata-specific**, and **RealPage-specific**.")

            with st.expander("**What's new — July 2026 overhaul**", expanded=True):
                st.markdown("""
| Area | Change |
|---|---|
| **All Systems mode** | One pull combines Yardi + Entrata + RealPage for a parent company: one dataset, one PDF, one CSV set. Fetch Results shows each property's source system. |
| **RealPage** | Live OneSite API, **current-state only** (2 calls per property). No synthetic history → no lift for RealPage until RealPage authorizes `getresidentledger`. Missing/suspected candidates are filtered against the live resident roster. |
| **Fetch cache** | Every pull appends to `RAW.PMC_EXTERNAL_INTEGRATIONS.CACHED_PET_VALUE_DATA` and is reused for 7 days (sidebar → *Data reuse*). Append-only, so it doubles as our own history of what the APIs returned on each date. |
| **Pet rent pricing** | The portfolio's own recurring pet-rent distribution (median, middle 50%, full range across its properties) — Summary tab + PDF. Replaced the earlier network benchmark + assistance-animal ratio. |
| **Progress deltas** | Every *Generate Report* snapshots the headline metrics; the next report on the same selection shows a *Since Last Report* table (app + PDF). |
| **Data health checks** | Six automated sanity checks on the Charts tab (launch dates, duplicates, cross-property emails, ...). |
| **Calculation fixes** | Per-tenant **median** fee estimation, per-person dollar dedup, 90-day recency rule for "paying" matching. |
| **AppFolio** | Hidden until API access (MFA) is resolved; provider code kept as a placeholder. |
| **Developer view** | Sidebar toggle reveals raw exports, AR reconciliation, detail tables, and the property data explorer. |

Deep-dive write-ups live in the repo: `docs/realpage-accuracy-audit.md`,
`docs/yardi-entrata-staging-audit.md`, `docs/calc-audit-2026-07.md`,
`docs/property-manager-endpoints.md`.
                """)

            # ══════════════════════════════════════════════════════════
            #  SECTION: BOTH SYSTEMS
            # ══════════════════════════════════════════════════════════
            st.subheader("Shared Logic — All Systems")

            with st.expander("**Property Selection — How properties are loaded**", expanded=False):
                st.markdown("""
**Parent Company dropdown** counts ALL properties from `d_properties` (total count)
plus the subset with active API integrations (api count). The difference = properties
missing integration records that will be skipped during fetch.

**When you click Fetch**, only properties with active integrations (Yardi or Entrata)
are loaded. The app shows you how many of the total properties have API access.
                """)

            with st.expander("**Tenant Identification — Who counts as 'missing pet rent'?**"):
                st.markdown("""
The same Snowflake query is used **everywhere** (Charts tab orange bars, Summary tab,
Missing Pet Rent Report tab) to ensure numbers match:

```sql
SELECT DISTINCT du.property_id, tenant_code, ue.user_email, ...
FROM PROD.common.d_units du
JOIN PROD.petscreening.petscreening__user_enriched ue ON ue.unit_id = du.unit_id
JOIN PROD.common.f_leases l ON du.unit_key = l.unit_key AND l.user_key = ue.user_key
WHERE du.unit_source = '<yardi|entrata>'
  AND ue.compliance_status = 'compliant'
  AND ue.user_pet_type = 'household'
  AND ue.user_pet_status = 'active'     -- active household tenants only
```

**Matching priority:**

| Method | How it works |
|--------|-------------|
| **Tenant code** | Case-insensitive match of Snowflake `tenant_code` → API `tenant_code` |
| **Email** | Case-insensitive match of Snowflake `user_email` → API `email` |
| **Unit expansion** | If anyone on the same `(property, unit)` is paying, all tenants on that unit are paying |
| **Lease expansion (Entrata)** | If anyone on the same `lease_id` is paying, all customers on that lease are paying |

**Entrata note:** The deep dive on Hillpointe showed that tenant code matching
is **0% effective** for some Entrata PMCs (Snowflake `tenant_code` is NULL). Email
matching is the primary path for Entrata. The lease-level expansion was added to
catch co-tenants that unit-level matching alone would miss.

**Stale profile filtering (Entrata):** Profiles whose email does not appear in the
current API data are excluded from missing-rent counts. This prevents ex-tenants
with lingering PetScreening profiles from inflating the numbers.
                """)

            with st.expander("**Roommate & Co-Tenant Handling**"):
                st.markdown("""
**How roommates are handled throughout the app:**

Pet rent is typically charged **per-lease** (or per-unit), not per-person. When multiple
tenants share a unit, only one usually has the pet charge on their name. The app handles
this at several levels:

| Layer | How it works |
|-------|-------------|
| **Yardi (API)** | GetRentroll returns one charge row per `(unit, tenant, charge_code)`. If roommate A has "Pet Rent" but roommate B does not, only A has the charge row. |
| **Entrata (API)** | `getLeases` returns scheduled charges at the **lease** level. Every customer on the same lease gets an identical copy of the charge. The app deduplicates by `(lease_id, interval_id, charge_code, start_date, amount)` so the charge is counted **once** for revenue, but every customer is marked as "paying". |
| **Paying flag (unit-level)** | If *any* tenant on a `(property_id, unit_code)` has a pet charge, **all** tenants on that unit are marked paying. |
| **Paying flag (lease-level, Entrata)** | If *any* customer on the same `lease_id` has a pet charge, all other customers on that lease are also marked paying. This handles cases where unit-level matching is incomplete due to tenant code mismatches. |
| **Paying flag (email)** | The `_apply_paying_flag` function propagates payment status by **email**: if _any_ row for a given `USER_EMAIL` is paying, _all_ rows for that email are marked paying. |
| **Revenue aggregation** | For Entrata, the dedup key ensures charges shared across multiple customers are summed once. For Yardi, each charge row is naturally unique per `(unit, tenant, charge_code, from_date)`. |

**Edge case:** If two roommates each independently have a pet, both should ideally have their own
pet charge. The app will correctly identify the second roommate as "not paying" if they have a
completed PetScreening screening but no charge in their name.

**Why this matters for counts:** The "tenants not paying" count uses `USER_EMAIL.nunique()`,
so a person appearing in multiple lease rows is counted once.
                """)

            with st.expander("**Suspected Undisclosed Pets — What gets excluded?**"):
                st.markdown("""
The suspected undisclosed report identifies residents whose PetScreening profile signals
a likely undisclosed pet. The following are **excluded**:

| Excluded | Why |
|----------|-----|
| **Assistance profiles with `recommended` status** | They completed the process successfully — not suspicious |
| **Assistance profiles with `expired` status** | Expired requests are no longer actionable |
| **Pet profiles with `archive_reason` set** | Already resolved/archived by the property |
| **Profiles already paying** any selected charge code | They're paying — not missing |

**Who IS included (suspected reasons):**
- **Abandoned household profile** — current household pet profile is incomplete/unresolved
- **Unresolved assistance request** — current assistance request was declined, left in draft, or returned
                """)

            with st.expander("**Charge Classification — Recurring vs One-Time**"):
                st.markdown("""
The app classifies each selected charge code at each property as either **recurring rent**
or **one-time deposit**. This determines how uncollected revenue is estimated:

- **Recurring rent**: added to every month the missing tenant was active
- **One-time deposit**: added only to the tenant's first active month

| System | How classification works |
|--------|------------------------|
| **Yardi** | Inferred from median date span of actual charges: > 60 days = recurring, ≤ 60 days = one-time |
| **Entrata** | Uses the explicit `frequency` field on each charge (`Monthly` → recurring, `One-Time` → one-time) |

The estimated uncollected amount per missing tenant uses the **average (mean)** charge
amount from tenants who ARE paying at that specific property (with portfolio-wide
average as fallback if no payers at that property).
                """)

            with st.expander("**Fetch Cache — `CACHED_PET_VALUE_DATA` (reuse + our own history)**"):
                st.markdown("""
Every per-property API pull is appended to an **append-only Snowflake
table** and reused for 7 days (configurable in the sidebar → *Data
reuse*). Errors are never cached; partial cache hits fetch only the
missing properties. Because nothing is deleted, the table doubles as our
own record of what each PMS API returned on each date — independent of
the EKS/Airbyte staging jobs.

Query it directly:

```sql
SELECT PROPERTY_ID, FETCHED_AT,
       DATA:charge_code::STRING      AS charge_code,
       DATA:charge_amount::FLOAT     AS amount,
       DATA:charge_from_date::STRING AS month
FROM RAW.PMC_EXTERNAL_INTEGRATIONS.CACHED_PET_VALUE_DATA
WHERE RECORD_TYPE = 'charge'
QUALIFY FETCH_ID = FIRST_VALUE(FETCH_ID) OVER (
    PARTITION BY PROPERTY_ID ORDER BY FETCHED_AT DESC);
```

Row types: `charge`, `ar_charge`, `raw_lease`, `meta` (one per
property-fetch: log row + counts + the RealPage live resident roster),
and `snapshot` (report metrics powering the *Since Last Report* deltas;
`PMC_SYSTEM = '_report'`). Table name is the `PET_VALUE_CACHE_TABLE` env
var — move it to `RAW.MISC` once DEVELOPER gets `CREATE TABLE` there.
                """)

            with st.expander("**Pet Rent Pricing — methodology**"):
                st.markdown("""
The Summary tab / PDF **Pet Rent Pricing** section is built entirely from
the portfolio's **own pulled data** (`benchmarks.py` →
`build_pet_rent_pricing`) — no external market pool:

- Each property's **typical recurring pet fee** comes from the same
  `_estimate_property_fees` per-tenant-median logic used for missing-rent
  estimation ($5–$200 plausibility band).
- Across those per-property fees the section reports the **median**, the
  **middle 50%** (p25–p75), and the **full range** (min–max), always with
  the property count.
- Single-property reports show just the property's typical fee — a range
  over one property would be meaningless.

The earlier network benchmark ("your state's median is $Y") and the
assistance-animal share comparison were removed in July 2026: the fee
pool's provenance was hard to defend and one state number is misleading
for multi-state parents. A future own-book benchmark (state/zip
percentiles materialized from the `RAW.MISC` snapshot tables) is the
planned replacement; the old pool SQL lives in git history.
                """)

            with st.expander("**Launch Date Handling — Charts & Impact Calculation**"):
                st.markdown("""
- **Source:** `PROD.PETSCREENING.PETSCREENING__PROPERTY_KEY_FACTS.PROPERTY_LAUNCH_DATE`
- **Launch month is counted as post-launch** (green bar). The red line sits at the boundary
  between the last pre-launch month and the launch month.
- **Pre-launch baseline** = average of up to 6 observed pre-launch months. Only months with actual property revenue records are averaged; missing months are not averaged as $0.
- **Latest/current-month lift** = latest visible month revenue − pre-launch baseline. This is the default headline and individual-property badge methodology.
- **Average Monthly Lift toggle** = optional seasonal/student-housing view that uses completed post-launch average lift instead of latest-month lift.
- **Cumulative impact** = total actual post-launch revenue − (pre_avg × number of post months).
- **Baseline reliability**: fewer than 3 observed pre-launch months are flagged as low-data, but the baseline still uses the observed months instead of diluting with missing calendar months.
- **Properties launched before the lookback window** have no pre-launch data in the selected window and are excluded from before/after impact.
                """)

            # ══════════════════════════════════════════════════════════
            #  SECTION: YARDI-SPECIFIC
            # ══════════════════════════════════════════════════════════
            st.subheader("Yardi-Specific Behavior")

            with st.expander("**Yardi API — GetRentroll endpoint**"):
                st.markdown("""
**How data is fetched:**
- SOAP API call to each property's `GetRentroll` endpoint
- Requests wide date range (charges from `2000-01-01`, move dates from 10 years back)
- The lookback slider controls only the **display window**, not the API request

**Data structure:**
- One row per unit → tenant → charge (naturally deduplicated)
- Each tenant's charges are listed individually under their tenant node
- `ChargeCode`, `ChargeType`, `ChargeAmount`, `FromDate` are extracted per charge

**Charge classification (Yardi):**
- No explicit `frequency` field available
- Classification inferred from **median date span**: if the median span between
  `FromDate` and end date is > 60 days → recurring, otherwise → one-time

**Tenant matching:**
- `tenant_code` from API maps directly to `lease_source_external_id:tenant_code` in Snowflake
- Normalized: stripped + uppercased for case-insensitive matching
- Email fallback: stripped + lowercased
                """)

            with st.expander("**Yardi — End date logic**"):
                st.markdown("""
Since Yardi does not have a `frequency` field, the end date is handled as follows:

| Scenario | End date used |
|----------|--------------|
| Charge has explicit `charge_to_date` | Used as-is |
| Current tenant with no `charge_to_date` | Treated as ongoing (no end) |
| Past tenant with no `charge_to_date` | Falls back to `move_out_date` → `lease_to_date` |
                """)

            # ══════════════════════════════════════════════════════════
            #  SECTION: ENTRATA-SPECIFIC
            # ══════════════════════════════════════════════════════════
            st.subheader("Entrata-Specific Behavior")

            with st.expander("**Entrata API — getLeases endpoint**"):
                st.markdown("""
**How data is fetched:**
- REST API call to each property's `getLeases` endpoint (paginated, **500 per page**)
- Pagination uses a fallback: if the API returns fewer leases than the page size, we stop;
  otherwise we keep paging (Entrata's `meta` response is unreliable for pagination)
- Returns all leases including historical, cancelled, and applicant leases
- **AR Transactions** are also fetched (`includeArTransactions=1`) for scheduled-vs-actual comparison

**Data structure (key differences from Yardi):**
- One JSON object per lease, containing arrays of customers, intervals, charges, activities, and AR transactions
- **Multiple customers per lease**: Primary, Co-Applicant, Guarantor, Roommate
- **Multiple intervals per lease**: Original, renewals, cancelled renewal attempts
- **Charges belong to the lease** (not individual customers) — shared across all customer rows

**Scheduled vs. Actual Charges:**

The primary revenue data uses **scheduled charges** (`scheduledCharges`). A separate
**AR Transactions** view (in Fee Collection Analysis) shows actual posted/collected amounts.

| | Yardi (GetRentroll) | Entrata (getLeases) |
|---|---|---|
| **What we pull** | Lease charges (tied to active rent roll) | Scheduled charges + AR transactions |
| **Confirmation level** | Reflects what's on the current rent roll — closer to "billed" | Scheduled = what *should* be billed; AR = what *was* posted |
| **Implication** | Higher confidence that the charge is actively being collected | Use the AR comparison view to gauge collection accuracy |

For Entrata properties, the charts show **scheduled** revenue. Use the "Scheduled vs Actual
Revenue" expander in Fee Collection Analysis to compare against actual AR postings.
                """)

            with st.expander("**Entrata — Interval & Status Filtering**"):
                st.markdown("""
Entrata has **two status fields** that track different things:

| Field | Tracks | Used for |
|-------|--------|----------|
| `leaseIntervalStatus` | Did the lease go live? | **This is what we filter on** |
| `leaseCustomerStatus` | Is this person still on the lease? | Don't use for revenue filtering |

**Why it matters:** A guarantor with `leaseCustomerStatus: "Cancelled"` on a
`leaseIntervalStatus: "Notice"` lease should still have their charges counted —
the lease is active, the guarantor was just removed.

**Exclusion-based filter:** We use an **exclusion list** rather than inclusion list.
The following statuses are explicitly rejected:
- `cancelled` — lease never went live
- `applicant` — hasn't started
- `denied` — application denied
- `future` — not yet active

**Everything else is accepted**, including empty/unknown statuses. This is critical
because some PMCs (e.g. Hillpointe) return empty `leaseIntervalStatus` on 100% of
charges. An inclusion-based filter would reject all their data.

Each scheduled charge carries a `leaseIntervalId` that links it to a specific interval.
We only include charges from non-excluded intervals — this prevents counting charges
from cancelled renewal attempts while preserving valid data with empty status fields.
                """)

            with st.expander("**Entrata — Deduplication & Lease-Level Matching**"):
                st.markdown("""
**For revenue aggregation:**
Charges are deduplicated by a composite key:
`lease_id | interval_id | charge_code | start_date | amount`

Since every customer on the same lease gets identical charges, without dedup we'd
triple-count a lease with 3 customers. The app shows a caption when dedup occurs.

**For missing pet rent matching (two-level expansion):**

| Level | How it works |
|-------|-------------|
| **Unit-level** | If *any* tenant on a `(property_id, unit_code)` has a pet charge, all tenants on that unit are marked paying |
| **Lease-level** | If *any* customer on the same `lease_id` has a pet charge, all other customers on that lease are also marked paying |

This two-level approach prevents co-tenants from being flagged as "missing" when
their roommate's lease already includes a pet charge. It was added after the
Hillpointe deep dive revealed that unit-level expansion alone missed cases where
the `tenant_code` matching was incomplete.
                """)

            with st.expander("**Entrata — End Date Logic (uses `frequency` field)**"):
                st.markdown("""
Entrata explicitly provides a `frequency` field on each charge. This is **better** than
Yardi where we have to infer.

| Charge type | End date logic |
|-------------|---------------|
| **Monthly** charges with `endDate` | Used as-is |
| **Monthly** charges without `endDate` | Falls back to interval's `lease_to_date` |
| **One-Time** charges | `charge_to = charge_from` (same day). Does **NOT** inherit the lease end date — prevents a one-time $300 deposit from looking like recurring revenue across 12 months |

The `frequency` field is also used for **charge classification** (recurring vs one-time)
when estimating uncollected pet rent.
                """)

            with st.expander("**Entrata — Stale Profile Filtering**"):
                st.markdown("""
**Problem discovered:** The Entrata deep dive on Hillpointe revealed that **96%** of
PetScreening profiles flagged as "missing pet rent" did not appear in the Entrata API
data at all. These are likely past tenants whose PetScreening profiles were never
deactivated, inflating the missing-rent count.

**How the freshness check works:**

1. After fetching Entrata lease data, the app builds a set of **known tenant emails**
   per property from the API response
2. Before running missing pet rent or suspected undisclosed analysis, each PetScreening
   profile is checked: does this tenant's email appear in the API data for their property?
3. Profiles that do NOT appear in the API data are **excluded** from the counts (unless
   they are already marked as paying)
4. An info message shows how many stale profiles were filtered out

**Why this only applies to Entrata:** Yardi's `GetRentroll` endpoint returns only
current rent roll tenants, so stale profiles are less of a concern. Entrata's
`getLeases` returns historical leases, but the tenant population visible in the
API is still constrained by pagination and integration timing.

**Caveat:** Pagination limits (500 per page) mean some active tenants may not appear
in the API data. The freshness check is conservative — it only excludes profiles that
clearly don't match any API tenant, and preserves profiles already marked as paying.
                """)

            with st.expander("**Entrata — Pre-PetScreening Baseline Reliability**"):
                st.markdown("""
**Problem discovered:** For many Entrata PMCs, the `getLeases` API returns scheduled
charges only from around the time the PetScreening integration was configured — not
the full historical charge ledger. This means pre-launch revenue data is often
incomplete or missing entirely.

**What the app does:**

- `compute_launch_analysis` now tracks `baseline_reliable` for each property:
  reliable = at least **3 months** of pre-launch charge data available (baseline uses up to 6 months when available)
- Properties with unreliable baselines are flagged in the charts with
  "-- insufficient data" annotations
- The impact summary table shows a **(low data)** badge next to unreliable properties
- If the majority of Entrata properties have unreliable baselines, a warning banner
  appears advising caution with the "Revenue Change Since PetScreening" metric

**Recommendation:** For Entrata PMCs with limited pre-launch data, focus on
post-launch trends and the missing pet rent analysis rather than before/after
revenue comparisons.
                """)

            with st.expander("**Entrata — Scheduled vs Actual Revenue (AR Transactions)**"):
                st.markdown("""
**What it is:** A comparison view in the Fee Collection Analysis section that shows
scheduled charges alongside actual AR (Accounts Receivable) transactions.

**How it works:**

| Data source | What it represents |
|-------------|-------------------|
| **Scheduled charges** | What the lease says *should* be billed (from `scheduledCharges`) |
| **AR transactions** | What was *actually posted* to the ledger (from `arTransactions`) |
| **AR collected** | Of the posted amount, how much has been *paid* (`amountPaid`) |

The app fetches AR transactions via `includeArTransactions=1` on the Entrata API,
filters for pet-related charge codes, and aggregates by month and property.

**Metrics shown:**
- **Scheduled Revenue** — total from scheduled charges in the display window
- **Actual Posted (AR)** — total from AR transactions posted in the same window
- **AR Collected** — how much of the posted amount has been paid
- **Variance** — percentage difference between AR posted and scheduled

**Monthly and per-property tables** let you drill into where scheduled and actual
amounts diverge. A large negative variance may indicate charges that are scheduled
but not yet billed, or tenants who have moved out before the scheduled charge period.

**Important:** AR transactions are only available if the PMC's Entrata integration
supports `includeArTransactions`. If no AR data is returned, this section won't appear.
                """)

            with st.expander("**Entrata — Property Count (X/Y format)**"):
                st.markdown("""
The parent company dropdown shows `X / Y` where:
- **X** = properties with active Entrata API integrations (will be fetched)
- **Y** = total properties under that parent company in `d_properties` (regardless of source)

This matches the Yardi behavior. Properties without active integrations are skipped during fetch.
                """)

            # ══════════════════════════════════════════════════════════
            #  SECTION: REALPAGE-SPECIFIC
            # ══════════════════════════════════════════════════════════
            st.subheader("RealPage-Specific Behavior")

            with st.expander("**RealPage / OneSite — Where the data comes from**", expanded=True):
                st.markdown("""
**RealPage is fetched LIVE from the OneSite SOAP API** — 2 calls per property:

| Call | What it returns |
|---|---|
| `getscheduledcharges` | Current residents' scheduled charges (today's snapshot) |
| `getresidentlist` | ALL residents — Current + Former + Applicants — incl. balance fields |

**Current-state only, by design.** Empirical testing
(`docs/realpage-accuracy-audit.md`) proved the API cannot return history:
the `postmonth` parameter is silently ignored (byte-identical responses
for any value) and Former residents' charges return zero rows. Charge
rows are therefore emitted for the **current month only** (plus one
future month when pet rent is scheduled to start), and RealPage
properties are excluded from the before/after lift analysis.

**Live resident roster filtering:** missing/suspected candidates must
match a current (non-Former, not-moved-out) resident from
`getresidentlist` — the reports reflect who lives there *today*, with no
stale staging dependency. The roster is persisted with the cache payload
so cached runs produce identical reports.

**History (July 2026):** the app previously read Snowflake staging tables
populated by a daily EKS job; audits found stale ingestion and properties
with zero usable rows. A hybrid staging+live mode was built and then
retired in favor of honest current-state reporting. `getresidentledger`
**exists** on the RealPage gateway but our license key is not authorized —
once RealPage grants it, true per-month history (including former
residents) unlocks lift for RealPage.

**Property identifier is composite:** `pmcid` + `siteid`, both extracted
from `D_PROPERTIES.property_source_id` JSON. `property_source_name` is
`'real_page'` (with underscore — different from `'yardi'` and `'entrata'`).

**No integration_id required.** Yardi/Entrata each have a row in
`STG_PETSCREENING__INTEGRATIONS` with property-specific credentials.
RealPage uses a single global credential set, so `D_PROPERTIES.integration_id`
is `NULL` for all `real_page` properties — the loader does not JOIN
against the integration table.
                """)

            with st.expander("**RealPage — Charge ↔ Resident Join Logic**"):
                st.markdown("""
Charges and residents are joined in Python
(`realpage_live_api._join_and_emit`) using the same priority order as the
canonical pet-signals SQL used internally (shown here for reference):

```sql
-- Pick the representative resident per (pmc, site, lease)
lease_resident_pick AS (
  SELECT * FROM all_residents
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY pmc_id, site_id, lease_id
    ORDER BY
      CASE WHEN hoh_bit = 'TRUE' THEN 0 ELSE 1 END,
      CASE WHEN signer_bit = 'TRUE' THEN 0 ELSE 1 END,
      CASE WHEN active_bit = 'TRUE' THEN 0 ELSE 1 END,
      CASE WHEN contact_status = 'Lease signer' THEN 0 ELSE 1 END,
      last_name, first_name
  ) = 1
)
```

**Match priority:** direct match on the full
`(pmc_id, site_id, lease_id, resident_member_id)` key when the charge's
`ResHID` maps to a resident on the same lease. If that does not line up,
fall back to the representative resident on the same `lease_id`.
`lease_id` is required in the direct match because RealPage
`resident_member_id` values are not unique enough by themselves inside a site.

**`tenant_code = lease_id`** for RealPage. The Snowflake side mirrors
this: `f_leases.lease_source_external_id` for `real_page` is exactly
`{"lease_id": N}` (no per-resident tenant_code), so the missing-pet-rent
SQL falls back to that key when the Yardi-style `tenant_code` JSON path
is absent.

**Test/model residents are filtered out:** OneSite typically has
placeholder units with names like `Model, Model`, `Test, Test`,
`Unknown, Unknown`, `Vacant, Down`, etc. The fetch drops rows where
`firstname` or `lastname` matches any of: `model`, `test`, `sample`,
`unknown`, `noname`, `tba`, `vacant`, `*`, or empty.
                """)

            with st.expander("**RealPage — Why Lift Analysis Is Not Shown**", expanded=True):
                st.markdown("""
**RealPage's data history doesn't extend back far enough to compute
reliable pre-PetScreening baselines.** Direct API testing confirmed
the limitation:

1. **`getscheduledcharges` ignores its `post_month` parameter.**
   Five different `post_month` values (current, 12mo ago, 24mo ago,
   pre-launch, even `'BANANA'`) all returned **byte-identical**
   responses for the same property. The endpoint always returns the
   *current* schedule.
2. **`getresidenttransaction` returns ~6 months** of currently open
   transactions — not full historical billing.
3. **Snowflake staging only has 15 distinct `post_month` values**
   spanning Jan 2025 → May 2026. The EKS job has been accumulating
   snapshots since Jan 2025; deeper history isn't available anywhere
   we can access.
4. **Past residents' historical pet rent payments cannot be recovered.**
   When a resident moves out, their schedule is wiped from OneSite.

**Implication for the Summary / PDF:** for properties launched before
January 2025, the "pre-PS baseline" would be fabricated from missing
data (months that simply have no records). Showing a lift number
against that baseline would be wrong.

**What we do instead:** for RealPage, the Summary tab and PDF show
**current pet revenue + missing pet rent opportunity** without the
lift comparison. Per-property cards show "data starts after launch"
instead of a fake `+$X/mo` lift annotation.

**The `data_starts_after_launch` guard:** in `compute_launch_analysis`,
any property whose first non-zero data month is more than 3 months
after its launch date gets `baseline_meaningful = False` automatically.
This excludes it from the comparable set.

**Yardi and Entrata are unaffected.** Their data goes back further
than typical launch dates, so the guard never triggers.
                """)

            with st.expander("**RealPage — Charge Code Filter (CatCode='C' only)**"):
                st.markdown("""
OneSite uses two `CatCode` values that look superficially similar but
mean opposite things:

| `CatCode` | Meaning | What we do |
|---|---|---|
| `C` | Charge (resident owes the property) | **Keep** — these are revenue |
| `P` | Payment / Reimbursement (property owes resident) | **Drop** — these are credits, not revenue |

We filter to `CatCode = 'C'` at the SQL level. Without this, utility
allowance reimbursements (Section 8 properties) and similar credits
would be summed alongside actual charges and inflate revenue.

**Pet code matching uses both `transaction_name` (TranName) and
`description` (TransDesc).** OneSite's pet codes are concentrated
around the `PETRENT` family (with variants `PET RENT`, `PETR`,
`PETFEE`, etc.) but description text often spells things out more
clearly (`'Pet Rent - Dog'`, `'Pet Rent - Cat'`, `'Monthly Pet Rent'`).
The existing `pet|animal|petnr|concpet` regex still works — we just
apply it to both fields when the user is selecting which codes count
as pet-related.
                """)

            with st.expander("**RealPage — Known DevOps / Pipeline Issues**"):
                st.markdown("""
**Historical context** — since July 2026 the app calls the OneSite API
live and no longer reads these staging tables. The pipeline issues below
remain open for the data engineering team (they matter for anyone else
building on the staging layer):

1. **`getscheduledcharges` parameter is silently ignored.**
   The EKS job's `MONTHS_BACK=2` setting does nothing — calling the
   API for any past month returns the current snapshot. The job is
   essentially generating duplicate snapshots with different
   `ingested_at` timestamps. Deeper history is not retrievable
   through this endpoint.
2. **dbt staging surrogate key omits `resh_id`, `unit_id`, `amount`,
   `bill_end`.** Two genuinely-different charges (different residents
   on the same lease, same code, same start) could collapse into one
   row.
3. **dbt staging omits `haspets`, `begindate`, `enddate`, `moveindate`.**
   `haspets` is a unique RealPage advantage — OneSite's own opinion on
   whether the resident has pets. Combined with PetScreening profile
   data, this would give us **high-confidence missing pet rent**
   (`haspets='Y'` + no pet charge + active PS profile = strong signal).
   Currently dropped at the staging layer.
4. **Coverage gaps.** Snowflake shows ~6,447 `(pmc, site)` pairs in
   the charges staging vs ~4,989 enabled `real_page` properties in
   `D_PROPERTIES`. The 1,458 orphans are either stale entries or
   missing entries on one side or the other.
5. **`residentwillhavestatus` typo.** The WSDL field is
   `residentwillhave**the**status` but the EKS payload sends
   `residentwillhavestatus`. Probably moot given the API ignores most
   parameters anyway, but worth fixing for cleanliness.
                """)

            # ══════════════════════════════════════════════════════════
            # SECTION A  —  FREQUENTLY ASKED QUESTIONS
            # ══════════════════════════════════════════════════════════
            st.subheader("Frequently Asked Questions")

            with st.expander("**Why do the property counts change at each step? (e.g. 41 → 36 → 33 → 29)**", expanded=True):
                st.markdown("""
Each number represents a progressively smaller slice — here's what each one means:

| Step | Count | What it represents |
|------|-------|--------------------|
| **Total properties** | e.g. 41 | All properties under the parent company in `d_properties` |
| **API access** | e.g. 36 | Properties with active integration credentials (Yardi or Entrata) in `STG_PETSCREENING__INTEGRATIONS`. Missing ones have no API credentials configured. |
| **Properties with charge data** | e.g. 33 | Properties where the API returned data **and** at least one charge matched your selected charge codes. If a property has no tenants with the codes you selected (e.g., it uses a different charge code name), it drops out. |
| **Properties in adoption KPIs** | e.g. 29 | Properties that have **both** (a) charge revenue > $0 in the latest month **and** (b) adoption data in the QBR reporting table. Properties with $0 revenue or no QBR data are excluded from the average adoption and projected revenue calculations. |
| **Comparable properties** | varies | Properties with launch dates where we have **both pre-launch and post-launch** charge data. Used for the "Revenue Change Since PetScreening" KPI. Properties launched before the lookback window or too recently are excluded. |

**Why it matters:** Each filter ensures the math is apples-to-apples. If a property has $0 revenue, including it in the adoption average would be misleading. If a property has no pre-launch baseline, we can't calculate a meaningful "revenue change."
                """)

            with st.expander("**How is 'Revenue Change Since PetScreening' calculated?**"):
                st.markdown("""
For each property with a PetScreening launch date:

1. **Pre-launch baseline** = average monthly pet fee revenue in **up to 6 months before** the launch month (uses whatever pre-launch data is available — more months = less seasonal noise)
2. **Post-launch avg** = average of all completed post-launch months (excludes the current partial month to avoid undercounting)
3. **Monthly Change** = Cumulative impact ÷ completed post months (average monthly uplift since launch)
4. **Total Change** = Sum(all post revenue) − (Pre avg × post months) — actual observed cumulative impact in real dollars

The aggregate KPIs sum these values across all **comparable** properties (those with both pre & post data).

**Why up to 6 months for pre?** Using more months smooths out seasonal variation and one-time charges. The monthly lift uses all completed post months so it's mathematically consistent with the cumulative total (monthly × months ≈ cumulative).

**The launch month itself is counted as post-launch** (green bar on charts). The red line sits at the boundary between the last pre-launch month and the launch month.

**Properties excluded from this calculation:**
- No launch date in `PETSCREENING__PROPERTY_KEY_FACTS`
- Launch date is before the lookback window (no pre-launch data available)
- Launch date is too recent (no post-launch months yet)

**Entrata caveat:** Many Entrata PMCs have limited pre-launch charge data (the API often
only returns charges from around when the integration was configured). Properties with
fewer than 3 pre-launch months are flagged as "low data" and should be interpreted with
caution.
                """)

            with st.expander("**How does the adoption overlay work?**"):
                st.markdown("""
The adoption overlay adds a **purple line** on top of each property's revenue bar chart, showing how the adoption rate has trended over the same time period.

- **Unit Adoption** = Active Units ÷ Total Units (from `R_QUARTERLY_BUSINESS_REVIEW_REPORTING`)
- **Resident Adoption** = Active Users ÷ Total Users (same table)

The overlay uses a **secondary Y-axis** (right side, 0–100%) so the scales don't clash.

**When an overlay is active**, additional KPIs appear:
- **Current Monthly Pet-Related Revenue** — what the portfolio earns today in pet fees
- **Projected at 100% Adoption** — linear extrapolation: `current_revenue ÷ (adoption_rate / 100)`
- **Average Adoption** — mean across properties that have **both** revenue and adoption data

A per-property table also appears below the charts showing each property's current adoption, current revenue, and projected revenue at 100%.
                """)

            with st.expander("**How does 'Projected Revenue at 100% Adoption' work?**"):
                st.markdown("""
For each property, we take two data points from the **latest month**:
- **Current Monthly Pet-Related Revenue** (from the selected pet fee charge codes)
- **Current Adoption Rate** (unit or resident, from the QBR table)

Then: `Projected Revenue = Current Revenue ÷ (Current Adoption / 100)`

**Example:** A property earns $5,000/mo at 65% unit adoption.
The average revenue per compliant unit ≈ $5,000 ÷ 0.65 = $7,692.
At 100% adoption → $7,692/mo. Additional = $2,692/mo.

**Caveats:**
- This is a **linear extrapolation** — the last units to comply may have fewer or no pets
- Only properties with **both** revenue > $0 **and** adoption data are included (this is why you may see fewer properties in the "Summary" tab than in the charts)
- The adoption type (unit vs. resident) matches your Charts tab overlay selection
                """)

            with st.expander("**How does 'Show uncollected pet rent' (orange bars) work?**"):
                st.markdown("""
When you toggle **Show uncollected pet rent**, the app:

**1. Identifies missing tenants** — queries Snowflake to find current tenants at each property who:
   - Have an active household pet in PetScreening (`compliance_status = 'compliant'`, `user_pet_type = 'household'`)
   - Are **NOT** being charged any of the selected pet fee charge codes

**2. Estimates the missing revenue per tenant** — this is property-specific, not a flat number:

   a. The app looks at **tenants who ARE paying** at that specific property and collects all their actual charge amounts for the codes you selected.

   b. It classifies each charge code as **recurring rent** or **one-time deposit**:
      - **Yardi:** inferred from median date span (> 60 days = recurring, ≤ 60 days = one-time)
      - **Entrata:** uses the explicit `frequency` field (`Monthly` → recurring, `One-Time` → one-time)

   c. It computes the **average (mean)** charge amount across paying tenants at that property, separately for recurring and one-time charges.

   d. That average becomes the estimate for each non-paying tenant.

   **Example:** If at "Miro" there are 20 tenants paying `PetRent` at amounts like $35, $40, $35, $38, etc., the average is ~$37/mo. Each non-paying tenant at Miro is estimated at $37/mo of uncollected revenue.

   **Fallback:** If a property has **zero** tenants paying the selected codes (so there's no data to average), the app falls back to the **portfolio-wide average** — the mean across all other properties in the parent company.

**3. Distributes across months** — each missing tenant's uncollected revenue is only shown for months where:
   - Their lease was active (`lease_from` to `lease_to` or ongoing, from the API)
   - The month is **on or after** the property's PetScreening launch date
   - The month is within the display window
   - For recurring rent → added to every active month
   - For one-time deposits → added only to the tenant's first active month

**4. Displays as orange bars** stacked on top of the green/blue collected revenue bars

**Below each property chart**, you'll see a count like "12 tenants not paying pet rent" — this is the number of unique tenants at that property identified in Step 1.

**In the summary section**, you'll see aggregate KPIs: total tenants not paying, estimated uncollected $/mo, and total uncollected across the window.

**Important:** This data comes from **PetScreening's internal profile data** (not the PMC system). It may not be 100% accurate if profile data and PMC charges are out of sync.

**Entrata stale profile filter:** For Entrata properties, profiles whose email does not
appear in the current API data are excluded before the uncollected revenue calculation.
This prevents ex-tenants with lingering PetScreening profiles from inflating the count.
An info message shows how many profiles were filtered out.
                """)

            with st.expander("**How does the Missing Pet Rent Report tab differ from the orange bars?**"):
                st.markdown("""
They answer the same core question — "who has a pet profile but isn't paying?" — and now
use the **exact same profile identification** so the tenant counts match across all tabs.

| Feature | Orange bars (Charts tab) | Missing Pet Rent Report tab |
|---------|-------------------------|----------------------------------|
| **Purpose** | Visual storytelling — show the revenue gap | Actionable download — send to property managers |
| **Output** | Stacked bars on charts + summary KPIs | Downloadable CSV/Excel with individual names |
| **Granularity** | Per-tenant count & estimated $ per property | Per-pet: each pet is a separate row (tenant may have 2+ pets) |
| **Charge estimate** | Avg charge from paying tenants at that property | Identifies tenants not paying ANY selected code |
| **Time dimension** | Distributed across months by lease dates | Point-in-time snapshot (current tenants only) |

**Important:** The KPIs in all tabs (Charts summary, Summary, Report) use the same
profile identification query (`compliant` + `household` + `active`). The Report tab's
"Missing Pet Rent Tenants" counts unique tenants (by email), not rows (which are per-pet).

Both use the **same live API data** + Snowflake profile data. Neither uses stale staging tables.
                """)

            with st.expander("**What does the 'Summary' tab show and how is it calculated?**"):
                st.markdown("""
The **Summary** tab is designed for **VP-level skimming** — high-level numbers with a narrative.

**KPIs shown (Row 1 — Revenue):**
- **Current Monthly Pet Fee Revenue** — sum of all properties' latest-month revenue for selected charge codes
- **Revenue Change Since PetScreening** — aggregate monthly lift based on all completed post-launch months vs pre-launch baseline (same calc as Charts tab)
- **Projected at 100% Adoption** — what total revenue could be if every property reached 100% adoption

**KPIs shown (Row 2 — Compliance):**
- **Average Adoption** — mean adoption across properties with both revenue and adoption data. The adoption type (unit/resident) is set by the **radio on the Summary tab** (it defaults from the Charts overlay).
- **Tenants Not Paying Pet Rent** — total count from the uncollected pet rent analysis (computed automatically; the Charts-tab toggle only controls the orange chart overlay)

**Natural language story:** Auto-generated paragraph summarizing the key numbers in plain English.

**"Email All Property Managers" button:** Populates a mailto: link with all PMs in BCC and a pre-written reminder about PetScreening compliance.

**Why does the property count differ from Charts?** See the first FAQ above — adoption KPIs only include properties with BOTH revenue > $0 AND adoption data in the QBR table.
                """)

            with st.expander("**Why might the adoption % on 'Summary' differ from Charts?**"):
                st.markdown("""
They should now match exactly. Both use the **same calculation**:

```
avg_adoption = mean(latest_adoption for each property in projected_100)
```

Where `projected_100` = properties that have **both**:
1. Latest-month revenue > $0 (from selected charge codes)
2. Latest-month adoption data (from `R_QUARTERLY_BUSINESS_REVIEW_REPORTING`)

If they ever differ, it means the data changed between tab renders (e.g., Streamlit reran with different slider settings). The adoption type (unit vs. resident) is set by the radio on the Summary tab, which defaults from the Charts overlay selection.
                """)

            with st.expander("**What is the lookback window slider and how does it affect results?**"):
                st.markdown("""
The **Display Window** slider in the sidebar (default: 24 months) controls how far back the charts show.

**Important:** The API always fetches the **full charge history** (10+ years of lease charge data). The slider only filters which months appear on charts and in calculations.

This means:
- Changing the slider **does NOT re-fetch data** from the API
- The same raw data is re-aggregated dynamically when you move the slider
- A shorter window (e.g., 6 months) gives a more recent view but may exclude pre-launch data
- A longer window (e.g., 60 months) shows more history but may include noise from old charge codes

**Tip:** For the "Revenue Change Since PetScreening" calculation, the pre-launch baseline (up to 6 months) is computed within the lookback window. If your window is too short, some properties may not have enough pre-launch data for comparison.
                """)

            with st.expander("**How are charge codes selected and why does it matter?**"):
                st.markdown("""
After fetching rent roll data, the app shows **all unique charge codes** found across all properties. You then select which ones represent pet fees (e.g., `PetRent`, `Pet Fee`, `PetDeposit`).

**This selection affects everything downstream:**
- Which charges are included in the monthly revenue charts
- Which tenants are considered "paying" for the Missing Pet Rent analysis
- Which revenue numbers appear in KPIs and the Summary summary

**Common pitfalls:**
- Different properties may use **different charge code names** for the same thing (e.g., `PetRent` vs `Pet Rent` vs `Pet Fee`). If you miss one, that property's revenue will appear lower.
- If a property uses a code you didn't select, it won't show up in the charts at all (this is why "properties with charge data" may be fewer than "properties fetched").
- The preview table below the multiselect shows sample rows from each selected code so you can verify.
                """)

            with st.expander("**What is 'Suspected Undisclosed Pets' and how does it work?**"):
                st.markdown("""
"Suspected undisclosed pets" are residents who show **behavioral signals** in PetScreening that suggest they
likely have a pet they haven't properly disclosed. Unlike the "confirmed" missing pet rent (residents with
active household pet screening who simply aren't being charged), suspected undisclosed tenants fall into three categories:

| Category | What it means | Why it matters |
|----------|---------------|----------------|
| **Abandoned household profile** | Current household pet profile is incomplete/unresolved | They likely have a pet but have not completed screening |
| **Unresolved assistance request** | Current assistance animal request was declined, left in draft, or returned | The request signals they have an animal. A denied request doesn't mean the pet left. |

**Exclusions:** Old all-time profile history, expired profiles, recommended assistance animals, residents who attested `not_pet`, archived profiles, and residents already paying the selected pet charge codes are excluded from suspected cases.

**How the red bars on charts are calculated:**

The estimated revenue methodology is the same as confirmed missing rent (orange bars):
1. We identify suspected residents not paying any of the selected charge codes
2. We use their lease dates to determine which months they were at the property
3. We use the **average charge amount from paying tenants at that property** as the per-tenant estimate
4. We apply the charge only for months on/after the property's PetScreening launch date

**Key differences from confirmed missing rent:**

| Aspect | Confirmed (orange) | Suspected (red) |
|--------|-------------------|-----------------|
| **Who** | Active household pet profile — **confirmed** pet owner | Behavioral signals — **likely** pet owner |
| **Confidence** | High — they told us they have a pet | Medium — inferred from profile behavior |
| **Source** | `user_pet_type = 'household'` AND `user_pet_status = 'active'` | Current unresolved household/assistance profile state |
| **Use case** | Revenue you're definitely leaving on the table | Additional revenue opportunity — worth investigating |

**Important notes:**
- Suspected residents are **excluded** if they're already paying any of the selected charge codes
- Suspected and confirmed never overlap — a resident is in one category or neither
- The suspected report includes a `SUSPECTED_REASON` column so you can prioritize follow-up
- Archived profiles are excluded (only active/current records are considered)
                """)

            # ══════════════════════════════════════════════════════════
            # SECTION B  —  TECHNICAL REFERENCE
            # ══════════════════════════════════════════════════════════
            st.markdown("---")
            st.subheader("Technical Reference")

            with st.expander("Data Sources & Key Tables"):
                st.markdown("""
**Shared tables (all systems):**

| Table | Purpose |
|-------|---------|
| `PROD.COMMON.D_PROPERTIES` | Property master data, parent company info, ancestry IDs |
| `PROD.STAGING.STG_PETSCREENING__INTEGRATIONS` | API credentials per integration (Yardi SOAP or Entrata REST) |
| `PROD.PETSCREENING.PETSCREENING__PROPERTY_KEY_FACTS` | PetScreening launch dates per property |
| `PROD.COMMON.D_UNITS` | Unit dimension (joins profiles to properties via `unit_source`) |
| `PROD.PETSCREENING.PETSCREENING__USER_ENRICHED` | PetScreening user profiles, compliance status, pet type |
| `PROD.COMMON.F_LEASES` | Lease facts (links tenant_code/customerId to PetScreening user_key) |
| `PROD.COMMON.F_USER_PETS` | Links users to pet profiles (suspected undisclosed filtering) |
| `PROD.COMMON.D_PET_PROFILES` | Pet profile details — kind, status, archive reason |
| `PROD.REPORTING.R_MONTHLY_EXECUTIVE_SUMMARY` | Detailed pet/profile data for the downloadable report |
| `PROD.REPORTING.R_QUARTERLY_BUSINESS_REVIEW_REPORTING` | Monthly compliance/adoption rates (unit & resident level) |
| `PROD.STAGING.STG_PETSCREENING__PROPERTY_MANAGER_PERMISSIONS_ONLY` | Property manager permissions (for email feature) |

**Yardi-only:**

| Table | Purpose |
|-------|---------|
| `PROD.STAGING.STG_PETSCREENING__UNITS` | Property code mapping (Yardi property code → PetScreening property) |

**Entrata-only:**

| Table | Purpose |
|-------|---------|
| `RAW.PMC_EXTERNAL_INTEGRATIONS.ENTRATA_GETLEASES` | Raw Entrata getLeases responses (fallback historical data) |

**RealPage-only:**

| Table | Purpose |
|-------|---------|
| `PROD.STAGING.STG_PMC_INTEGRATIONS_REALPAGE__GETRESIDENTLIST` | SQL fallback for the current-resident check when no live roster is available (cache hits use the roster persisted with the payload) |

**Written by this app:**

| Table | Purpose |
|-------|---------|
| `RAW.PMC_EXTERNAL_INTEGRATIONS.CACHED_PET_VALUE_DATA` | Append-only per-property fetch cache + report snapshots (see the Fetch Cache expander above) |
                """)

            with st.expander("Yardi GetRentroll API Call"):
                st.markdown("For each property with credentials, we call the Yardi `GetRentroll` SOAP endpoint:")
                st.code("""
POST {resident_data_url}
Content-Type: text/xml; charset=utf-8
SOAPAction: http://tempuri.org/YSI.Interfaces.WebServices/ItfResidentData/GetRentroll

<GetRentroll>
    <UserName>{user_name}</UserName>
    <Password>{password}</Password>
    <ServerName>{server_name}</ServerName>
    <Database>{database_name}</Database>
    <Platform>Yardi</Platform>
    <InterfaceEntity>PetScreening</InterfaceEntity>
    <InterfaceLicense>{license_token}</InterfaceLicense>
    <YardiPropertyId>{property_code}</YardiPropertyId>
    <MoveIn>{10_years_ago}</MoveIn>         <!-- Wide range to get all history -->
    <MoveOut>{10_years_ago}</MoveOut>
    <LeaseChgFrom>2000-01-01</LeaseChgFrom> <!-- Get ALL charges ever -->
    <LeaseChgTo>{today}</LeaseChgTo>
</GetRentroll>
                """, language="xml")
                st.markdown("""
The XML response is parsed into a flat table with one row per **lease charge**:

| Column | Source |
|--------|--------|
| `property_id`, `property_name` | From our Snowflake query (`d_properties`) |
| `unit_code`, `unit_type`, `market_rent` | `<Unit>` element in XML response |
| `tenant_code`, `first_name`, `last_name`, `tenant_status` | `<Tenant>` element |
| `lease_from`, `lease_to`, `move_in` | `<Tenant>` element |
| `charge_code`, `charge_type`, `charge_amount` | `<LeaseCharge>` element |
| `charge_from_date`, `charge_to_date` | `<LeaseCharge>` element |

**This live API output replaces `stg_pmc_integrations_yardi__getrentroll_new`.**
                """)

            with st.expander("Entrata getLeases API Call"):
                st.markdown("""For each property with credentials, we call the Entrata `getLeases` REST endpoint:""")
                st.code("""
POST https://{corp_id}.entrata.com/api/v1/leases
Content-Type: application/json

{
    "method": { "name": "getLeases", "version": "r3" },
    "params": {
        "propertyId": "<entrata_property_id>",
        "includeScheduledCharges": 1,
        "includeArTransactions": 1,
        "page": 1,
        "per_page": 500
    }
}
                """, language="json")
                st.markdown("""
**Pagination:** 500 leases per page. The app pages forward until the API returns fewer
than 500 results (Entrata's `meta.currentPage`/`meta.lastPage` fields are unreliable).

The JSON response produces **two** flat tables:

**1. Scheduled charges** — one row per charge x customer:

| Column | Source |
|--------|--------|
| `property_id`, `property_name` | From our Snowflake query (`d_properties`) |
| `tenant_code` | `customer.id` from the lease JSON |
| `first_name`, `last_name` | `customer.firstName`, `customer.lastName` |
| `charge_code` | `scheduledCharge.chargeCodeDescription` |
| `charge_amount` | `scheduledCharge.amount` |
| `charge_from_date` | `scheduledCharge.startDate` |
| `charge_to_date` | `scheduledCharge.endDate` (or lease end for Monthly charges) |
| `frequency` | `scheduledCharge.frequency` — Monthly vs One-Time |
| `lease_id` | Used for lease-level deduplication and co-tenant matching |
| `lease_interval_status` | Used with exclusion filter (see Interval & Status Filtering) |
| `lease_interval_id` | Links charges to specific lease intervals |

**2. AR transactions** — one row per posted transaction:

| Column | Source |
|--------|--------|
| `property_name`, `lease_id` | From the lease context |
| `charge_code` | `arTransaction.chargeCodeName` (filtered for pet-related codes) |
| `amount`, `amount_paid` | Posted amount and paid amount |
| `post_date`, `post_month` | When the transaction was posted to the ledger |

**Key differences from Yardi:**
- Has explicit `frequency` field (no need to infer from date spans)
- Charges belong to the lease (shared across all customers)
- Deduplication is required to avoid counting shared charges multiple times
- AR transactions provide actual posted/collected amounts for comparison
                """)

            with st.expander("Chart Aggregation Logic (SQL equivalent)"):
                codes_display = ", ".join(f"'{c}'" for c in selected_codes)
                st.code(f"""
-- SQL equivalent of the chart aggregation logic
-- (this runs in Python/pandas, not against Snowflake)

WITH selected_charges AS (
    SELECT *
    FROM live_api_charges
    WHERE charge_code IN ({codes_display})
      AND charge_amount > 0
      AND charge_from_date IS NOT NULL
),
charge_months AS (
    SELECT c.property_name, m.month_dt, c.charge_amount
    FROM selected_charges c
    CROSS JOIN months m
    WHERE DATE_TRUNC('month', c.charge_from_date) <= m.month_dt
      AND COALESCE(DATE_TRUNC('month', c.charge_to_date), CURRENT_DATE()) >= m.month_dt
)
SELECT property_name, month_dt, SUM(charge_amount) AS revenue
FROM charge_months
GROUP BY property_name, month_dt;
                """, language="sql")

            with st.expander("Missing Pet Rent Report — Step-by-step SQL"):
                props_display = ", ".join(str(pid) for pid in st.session_state.property_ids[:5])
                if len(st.session_state.property_ids) > 5:
                    props_display += f", ... ({len(st.session_state.property_ids)} total)"
                st.markdown("""
**Step 1:** Identify tenants paying selected charges (from live API data)

**Step 2:** Query PetScreening household tenants from Snowflake (`d_units` → `user_enriched` → `f_leases`)

**Step 3:** Left join — tenants with completed screening but no matching charge = `Profile_No_Rent`

**Step 4:** Pull detailed pet data from `R_MONTHLY_EXECUTIVE_SUMMARY`

**Step 5:** Final join to get the downloadable report with names, emails, pet info, addresses
                """)

            with st.expander("Property Manager Email Query"):
                st.code("""
SELECT DISTINCT
    CASE
        WHEN u.user_email = 'parkatvenetopm@stylresidential.com'
        THEN 'parkatvenetoteam@stylresidential.com'
        WHEN u.user_email = 'admin@endeavourhsv.com'
        THEN 'contracts@endeavourhsv.com'
        ELSE u.user_email
    END AS pm_email,
    pm.entity_id AS property_id,
    p.property_name
FROM PROD.staging.stg_petscreening__property_manager_permissions_only pm
LEFT JOIN PROD.petscreening.petscreening__user_enriched u
    ON u.user_id = pm.user_id
LEFT JOIN PROD.common.d_properties p
    ON pm.entity_id = p.property_id
WHERE u.user_status = 'active'
  AND pm.name IN (
      'manager_admin_permission', 'manager_view_hipaa_permission',
      'manager_reviewer_permission', 'manager_view_only_permission'
  )
  AND u.compliance_status != 'n/a|manual|individual_level'
  AND u.user_role = 'property_manager'
  AND u.parent_company_ancestry_id = '<selected_ancestry_id>';
                """, language="sql")
                st.markdown("""
**Note:** `pm.entity_id` doesn't always match `d_properties.property_id`, so we filter by
`u.parent_company_ancestry_id` when selecting by parent company. For single-property
selections, we look up the ancestry_id from `d_properties` first.

The mailto: link (≤30 PMs) opens the email client with all PMs in BCC.
For >30 PMs, a copy-to-clipboard fallback is shown instead.
                """)

            with st.expander("End-to-End Data Flow"):
                st.markdown("""
```
┌─────────────────────────────────────────────────────────────────────┐
│                        SIDEBAR SELECTION                            │
│  User picks: System (All Systems / Yardi / Entrata / RealPage)      │
│  + Parent Company Name / Ancestry ID / Property ID                  │
└─────────────────┬───────────────────────────────────────────────────┘
                  │
                  
┌─────────────────────────────────────────────────────────────────────┐
│  SNOWFLAKE: d_properties + integrations → property list + creds     │
│  Count: Total Properties (e.g. 41)                                  │
│  Count: With API access (e.g. 36)                                   │
└─────────────────┬───────────────────────────────────────────────────┘
                  │
         ┌────────┴────────┐
                          
  ┌──────────────┐  ┌──────────────┐
  │  YARDI API   │  │ ENTRATA API  │
  │  GetRentroll │  │  getLeases   │
  │  SOAP/XML    │  │  REST/JSON   │
  └──────┬───────┘  └──────┬───────┘
         └────────┬────────┘
                  
┌─────────────────────────────────────────────────────────────────────┐
│  Flat table of ALL lease charges (full history, both systems)       │
│  → Same column shape: charge_code, amount, from/to dates, etc.     │
│  Count: Successfully fetched (e.g. 36, minus errors)                │
└─────────────────┬───────────────────────────────────────────────────┘
                  │
    ┌─────────────┼──────── User selects charge codes ────────┐
    │             │                                            │
    v             v                                            v
┌──────────────────────┐  ┌──────────────────┐  ┌────────────────────────┐
│ CHARTS TAB │ │ WHAT'S NEXT │ │ REPORT TAB │
│                      │  │                  │  │                        │
│  Filter to selected  │  │  VP summary KPIs │  │  Paying vs not-paying  │
│  charge codes        │  │  Natural language │  │  → Profile_No_Rent    │
│  Count: props with   │  │  story            │  │  → CSV/Excel download │
│  matching charges    │  │  Count: props with│  │                        │
│  (e.g. 33)           │  │  revenue+adoption │  │  Same profile query    │
│  → monthly charts    │  │  (e.g. 29)        │  │  as Charts tab         │
│  → adoption overlay  │  │  → Email All PMs  │  │  (counts match)        │
│  → uncollected bars  │  │                  │  │                        │
└──────────────────────┘  └──────────────────┘  └────────────────────────┘
```
                """)

            # ══════════════════════════════════════════════════════════
            # SECTION C  —  ASSUMPTIONS & LIMITATIONS
            # ══════════════════════════════════════════════════════════
            st.markdown("---")
            st.subheader("Assumptions & Limitations")
            st.markdown("""
**Both Systems:**
- **Live API data only:** All charge data comes from the PMC APIs in real time (Yardi GetRentroll SOAP, Entrata getLeases REST, RealPage OneSite SOAP). We do NOT read the PMC staging tables. RealPage is a current-state snapshot (no history — see the RealPage section). If an API returns incomplete data (e.g., during maintenance windows), charts will reflect that.
- **Charge duration assumption:** A charge is assumed active for every month between its `charge_from_date` and `charge_to_date`. If `charge_to_date` is null and the tenant is "Current", the charge is assumed ongoing.
- **Adoption = linear proxy for revenue:** The "projected at 100%" calculation assumes revenue scales linearly with adoption. The last units to comply may have fewer pets, so actual revenue at 100% may be lower.
- **Uncollected pet rent estimate:** Uses the **average (mean) charge amount** from tenants who ARE paying at each specific property. Falls back to portfolio-wide average if no payers at a property.
- **PetScreening tenants vs PMC charges:** Pet data (who has a pet) comes from PetScreening's internal tables. Charge data (who's paying) comes from the PMC API. These update independently — brief sync delays are possible.
- **Launch date = month boundary:** The launch month is counted as post-launch. Pre-launch baseline uses up to 6 months before the launch month. Post-launch monthly lift = cumulative impact ÷ completed post months (excludes current partial month). Cumulative impact uses actual observed total post-launch revenue minus the projected baseline.
- **PM emails:** Filtered to active PMs with specific permission types, using `parent_company_ancestry_id` from `user_enriched`.
- **Suspected undisclosed exclusions:** Assistance profiles with `recommended` or `expired` status are excluded. Pet profiles with a non-empty `archive_reason` are excluded.
- **Missing pet rent tenant criteria:** Only `compliant` + `household` + `active` tenants are considered. This is consistent across ALL tabs (Charts, Summary, Report).

**Yardi-specific:**
- **Charge classification:** Inferred from median date span (> 60 days = recurring, ≤ 60 = one-time). No explicit frequency field available.
- **End date fallback:** For past tenants with no `charge_to_date`, falls back to `move_out_date` → `lease_to_date`.

**Entrata-specific:**
- **Charge classification:** Uses the explicit `frequency` field (`Monthly` → recurring, `One-Time` → one-time).
- **One-time charge end dates:** `charge_to` is set to `charge_from` (same day) to prevent one-time deposits from appearing as recurring revenue across many months.
- **Deduplication:** Charges are deduplicated by `(lease_id, interval_id, charge_code, start_date, amount)` for revenue aggregation. Without this, leases with multiple customers would over-count.
- **Interval status filtering:** Only intervals with status `Current`, `Past`, or `Notice` are included. `Cancelled`, `Applicant`, and `Future` are excluded.
            """)
