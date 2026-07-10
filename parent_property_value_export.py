#!/usr/bin/env python3
"""
Parent-company property-level value export for PetScreening pet-rent reporting.

Input one parent-company ancestry ID (or a file of IDs) and produce one workbook
with:
  1. properties  — one row per property under the parent, including the same core
                  data points used in the PDF summary/log flow.
  2. aggregate   — parent rollup using comparable-property/unit-safe totals.

Examples:
    python parent_property_value_export.py --ids 61610 --output ./parent_exports --workers 8
    python parent_property_value_export.py --ids 61610 --pmc entrata --charge-codes PETRENT,PETFEE
    python parent_property_value_export.py --file parent_ids.txt --output ./parent_exports

Notes:
  - PMC is auto-detected per property from PROD.COMMON.D_PROPERTIES, so mixed
    parents can include Yardi, Entrata, RealPage, and AppFolio in one workbook.
  - Revenue math intentionally mirrors batch_pdf.py / the PDF: pet charge code
    selection, monthly bucketing, launch comparability, missing rent, suspected
    undisclosed, units, and 5% cap asset value.
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
import warnings
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

# Suppress Streamlit warnings when importing app.py in script mode.
os.environ["STREAMLIT_SERVER_HEADLESS"] = "true"
warnings.filterwarnings("ignore", message=".*missing ScriptRunContext.*")
warnings.filterwarnings("ignore", message=".*Session state does not function.*")
warnings.filterwarnings("ignore", category=UserWarning)
logging.getLogger("streamlit").setLevel(logging.ERROR)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

SUPPORTED_PMCS = ("yardi", "entrata", "real_page", "appfolio")
ASSET_VALUE_CAP_RATE = 0.05
PET_KEYWORDS = ["pet", "animal", "petnr", "concpet"]
EXCLUDED_KEYWORDS = ["carpet", "puppet", "trumpet"]

PROPERTY_COLUMNS = [
    "parent_company_name",
    "parent_company_id",
    "property_id",
    "property_name",
    "pmc",
    "status",
    "error",
    "selected_charge_codes",
    "charge_records_fetched",
    "pet_charge_records_used",
    "latest_month",
    "launch_date",
    "has_launch_date",
    "has_monthly_revenue_data",
    "is_comparable",
    "methodology",
    "total_units",
    "comparable_count",
    "pre_months",
    "post_months",
    "pre_baseline_monthly",
    "current_monthly_revenue",
    "comparable_current_monthly_revenue",
    "default_monthly_lift",
    "average_monthly_lift",
    "selected_monthly_lift",
    "selected_annual_lift",
    "selected_asset_value_5_cap",
    "missing_pet_rent_tenants",
    "missing_pet_rent_monthly",
    "missing_pet_rent_annual",
    "missing_pet_rent_properties",
    "missing_pet_rent_asset_value_5_cap",
    "suspected_undisclosed_profiles",
    "suspected_undisclosed_monthly",
    "suspected_undisclosed_annual",
    "suspected_undisclosed_asset_value_5_cap",
    "current_adoption_pct",
    "adoption_metric",
    "projected_100_pct_adoption_monthly",
    "additional_at_100_pct_adoption_monthly",
    "combined_delivered_plus_missing_monthly",
    "combined_delivered_plus_missing_annual",
    "combined_delivered_plus_missing_asset_value_5_cap",
    "processed_at",
]

AGGREGATE_COLUMNS = [
    "parent_company_name",
    "parent_company_id",
    "properties_total",
    "properties_success",
    "properties_failed",
    "pmc_systems",
    "total_units",
    "comparable_properties",
    "comparable_units",
    "pre_baseline_monthly",
    "current_monthly_revenue",
    "comparable_current_monthly_revenue",
    "default_monthly_lift",
    "average_monthly_lift",
    "selected_monthly_lift",
    "selected_annual_lift",
    "selected_asset_value_5_cap",
    "missing_pet_rent_tenants",
    "missing_pet_rent_monthly",
    "missing_pet_rent_annual",
    "missing_pet_rent_properties",
    "missing_pet_rent_asset_value_5_cap",
    "suspected_undisclosed_profiles",
    "suspected_undisclosed_monthly",
    "suspected_undisclosed_annual",
    "suspected_undisclosed_asset_value_5_cap",
    "combined_delivered_plus_missing_monthly",
    "combined_delivered_plus_missing_annual",
    "combined_delivered_plus_missing_asset_value_5_cap",
    "processed_at",
]


class FakeProgressBar:
    def progress(self, val: Any) -> None:  # pragma: no cover - only used by live fetchers
        return None


class FakeStatusText:
    def __init__(self, verbose: bool = True):
        self.verbose = verbose

    def text(self, msg: str) -> None:  # pragma: no cover - only used by live fetchers
        if self.verbose:
            print(f"    {msg}")


def parse_ids(args: argparse.Namespace) -> list[str]:
    if args.ids:
        return [x.strip() for x in args.ids.split(",") if x.strip()]
    if args.file:
        with open(args.file, "r", encoding="utf-8") as f:
            return [line.strip() for line in f if line.strip() and not line.lstrip().startswith("#")]
    raise SystemExit("Error: Provide --ids or --file")


def sanitize_filename(value: Any, max_len: int = 90) -> str:
    text = str(value or "").strip() or "unknown"
    for ch in ["/", "\\", ":", "*", "?", '"', "<", ">", "|", ","]:
        text = text.replace(ch, "-")
    text = "_".join(text.split())
    return text[:max_len].strip("._-") or "unknown"


def normalize_pmc_source(source: Any) -> str:
    raw = str(source or "").lower().strip().replace("-", "_").replace(" ", "_")
    if "entrata" in raw:
        return "entrata"
    if "real" in raw and "page" in raw:
        return "real_page"
    if "appfolio" in raw or "app_folio" in raw:
        return "appfolio"
    if "yardi" in raw:
        return "yardi"
    return raw or "unknown"


def _num(value: Any) -> float:
    try:
        if value is None or value == "":
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _round_money(value: Any) -> float:
    return round(_num(value), 2)


def _prop_get(prop: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in prop and prop.get(key) is not None:
            return prop.get(key)
        lower = key.lower()
        if lower in prop and prop.get(lower) is not None:
            return prop.get(lower)
        upper = key.upper()
        if upper in prop and prop.get(upper) is not None:
            return prop.get(upper)
    return default


def _month_start(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if hasattr(value, "to_pydatetime"):
        value = value.to_pydatetime()
    if isinstance(value, datetime):
        return datetime(value.year, value.month, 1)
    text = str(value)
    for fmt in ("%Y-%m-%d", "%Y-%m", "%m/%d/%Y"):
        try:
            dt = datetime.strptime(text[:10], fmt)
            return datetime(dt.year, dt.month, 1)
        except ValueError:
            continue
    return None


def is_pet_charge_code(code: Any) -> bool:
    if code is None:
        return False
    code_lower = str(code).lower()
    return any(kw in code_lower for kw in PET_KEYWORDS) and not any(
        bad in code_lower for bad in EXCLUDED_KEYWORDS
    )


def select_charge_codes(df: Any, manual_codes: str | None) -> list[str]:
    if "charge_code" not in df.columns:
        raise ValueError("No charge_code column in fetched charge data")
    if manual_codes:
        selected = [c.strip() for c in manual_codes.split(",") if c.strip()]
        if not selected:
            raise ValueError("--charge-codes was provided but no usable codes were parsed")
        return selected
    selected = sorted(
        df.loc[df["charge_code"].apply(is_pet_charge_code), "charge_code"]
        .dropna()
        .astype(str)
        .unique()
        .tolist()
    )
    if not selected:
        raise ValueError("No pet-related charge codes found")
    return selected


def detect_parent_label(conn: Any, parent_id: str) -> str:
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT PARENT_COMPANY_NAME
            FROM PROD.COMMON.D_PROPERTIES
            WHERE PARENT_COMPANY_ANCESTRY_ID = %s
              AND PARENT_COMPANY_NAME IS NOT NULL
            GROUP BY PARENT_COMPANY_NAME
            ORDER BY COUNT(DISTINCT PROPERTY_ID) DESC
            LIMIT 1
            """,
            (str(parent_id),),
        )
        row = cur.fetchone()
        if row:
            return str(row[0])
    finally:
        cur.close()
    return str(parent_id)


def load_parent_properties(conn: Any, parent_id: str, *, force_pmc: str | None = None) -> list[dict[str, Any]]:
    import snowflake.connector  # noqa: WPS433

    cur = conn.cursor(snowflake.connector.DictCursor)
    try:
        cur.execute(
            """
            SELECT DISTINCT
                PROPERTY_ID,
                PROPERTY_NAME,
                PARENT_COMPANY_NAME,
                PARENT_COMPANY_ANCESTRY_ID,
                PROPERTY_SOURCE_NAME,
                PROPERTY_NUMBER_OF_DOORS,
                PROPERTY_STATUS,
                INTEGRATION_STATUS
            FROM PROD.COMMON.D_PROPERTIES
            WHERE PARENT_COMPANY_ANCESTRY_ID = %s
              AND PROPERTY_ID IS NOT NULL
              AND PROPERTY_NAME IS NOT NULL
            ORDER BY PROPERTY_SOURCE_NAME, PROPERTY_NAME
            """,
            (str(parent_id),),
        )
        rows = cur.fetchall()
    finally:
        cur.close()

    props = []
    for row in rows:
        pmc = force_pmc or normalize_pmc_source(row.get("PROPERTY_SOURCE_NAME"))
        if pmc in SUPPORTED_PMCS:
            row["pmc"] = pmc
            props.append(row)
    return props


def load_app_imports() -> dict[str, Any]:
    from app import (  # noqa: WPS433 - intentional lazy script import
        ENTRATA_API_KEY,
        SOAP_HEADERS,
        YARDI_LICENSE_TOKEN,
        _build_property_id_lookup,
        _extract_ar_charges_from_entrata_lease,
        _extract_charges_from_entrata_lease,
        _fetch_entrata_leases_for_property,
        _launch_analysis_is_comparable,
        build_soap_payload,
        compute_launch_analysis,
        extract_charges_from_property,
        extract_property,
        fetch_appfolio_for_properties,
        fetch_compliance_data,
        fetch_entrata_for_properties,
        fetch_missing_pet_rent_by_property,
        fetch_realpage_for_properties,
        fetch_rentroll_for_properties,
        fetch_suspected_undisclosed_by_property,
        get_snowflake_connection,
        load_appfolio_properties_for_selection,
        load_entrata_properties_for_selection,
        load_properties_for_selection,
        load_realpage_properties_for_selection,
        parse_date,
        xml_to_dict,
    )

    return {
        "ENTRATA_API_KEY": ENTRATA_API_KEY,
        "SOAP_HEADERS": SOAP_HEADERS,
        "YARDI_LICENSE_TOKEN": YARDI_LICENSE_TOKEN,
        "_build_property_id_lookup": _build_property_id_lookup,
        "_extract_ar_charges_from_entrata_lease": _extract_ar_charges_from_entrata_lease,
        "_extract_charges_from_entrata_lease": _extract_charges_from_entrata_lease,
        "_fetch_entrata_leases_for_property": _fetch_entrata_leases_for_property,
        "_launch_analysis_is_comparable": _launch_analysis_is_comparable,
        "build_soap_payload": build_soap_payload,
        "compute_launch_analysis": compute_launch_analysis,
        "extract_charges_from_property": extract_charges_from_property,
        "extract_property": extract_property,
        "fetch_appfolio_for_properties": fetch_appfolio_for_properties,
        "fetch_compliance_data": fetch_compliance_data,
        "fetch_entrata_for_properties": fetch_entrata_for_properties,
        "fetch_missing_pet_rent_by_property": fetch_missing_pet_rent_by_property,
        "fetch_realpage_for_properties": fetch_realpage_for_properties,
        "fetch_rentroll_for_properties": fetch_rentroll_for_properties,
        "fetch_suspected_undisclosed_by_property": fetch_suspected_undisclosed_by_property,
        "get_snowflake_connection": get_snowflake_connection,
        "load_appfolio_properties_for_selection": load_appfolio_properties_for_selection,
        "load_entrata_properties_for_selection": load_entrata_properties_for_selection,
        "load_properties_for_selection": load_properties_for_selection,
        "load_realpage_properties_for_selection": load_realpage_properties_for_selection,
        "parse_date": parse_date,
        "xml_to_dict": xml_to_dict,
    }


def load_provider_property(prop: dict[str, Any], app_imports: dict[str, Any]) -> list[dict[str, Any]]:
    """Load the provider-specific property row used by the existing fetchers."""
    property_id = _prop_get(prop, "PROPERTY_ID", "property_id")
    pmc = prop["pmc"]
    if pmc == "entrata":
        loader = app_imports["load_entrata_properties_for_selection"]
    elif pmc == "real_page":
        loader = app_imports["load_realpage_properties_for_selection"]
    elif pmc == "appfolio":
        loader = app_imports["load_appfolio_properties_for_selection"]
    else:
        loader = app_imports["load_properties_for_selection"]

    props_df = loader(property_id=property_id)
    import pandas as pd  # noqa: WPS433

    if props_df is None:
        return []
    if isinstance(props_df, pd.DataFrame):
        return props_df.to_dict("records")
    if isinstance(props_df, list):
        return props_df
    raise TypeError(f"Unexpected provider property type: {type(props_df)}")


def fetch_charges_for_properties(
    properties: list[dict[str, Any]],
    pmc_system: str,
    app_imports: dict[str, Any],
    *,
    verbose: bool,
    workers: int = 1,
) -> tuple[list[dict[str, Any]], Any]:
    """Same provider routing as batch_pdf.py, kept local for import/test clarity."""
    if workers and workers > 1 and pmc_system in ("yardi", "entrata"):
        from parallel_fetch import fetch_entrata_parallel, fetch_yardi_parallel  # noqa: WPS433

        if pmc_system == "entrata":
            return fetch_entrata_parallel(
                properties,
                app_imports["ENTRATA_API_KEY"],
                app_imports,
                workers=workers,
                verbose=verbose,
            )[:2]
        return fetch_yardi_parallel(
            properties,
            app_imports,
            app_imports["YARDI_LICENSE_TOKEN"],
            app_imports["SOAP_HEADERS"],
            workers=workers,
            verbose=verbose,
        )[:2]

    progress_bar = FakeProgressBar()
    status_text = FakeStatusText(verbose=verbose)
    if pmc_system == "entrata":
        result = app_imports["fetch_entrata_for_properties"](properties, progress_bar, status_text)
        return result[0], result[1]
    if pmc_system == "real_page":
        return app_imports["fetch_realpage_for_properties"](properties, progress_bar, status_text)
    if pmc_system == "appfolio":
        return app_imports["fetch_appfolio_for_properties"](properties, progress_bar, status_text)
    return app_imports["fetch_rentroll_for_properties"](properties, progress_bar, status_text)


def build_month_window() -> list[datetime]:
    import pandas as pd  # noqa: WPS433

    today = datetime.now()
    window_end = datetime(today.year, today.month, 1)
    window_start = datetime(today.year - 5, today.month, 1)
    return [m.to_pydatetime() for m in pd.date_range(start=window_start, end=window_end, freq="MS")]


def latest_observed_revenue_month(
    monthly_by_prop: dict[str, dict[datetime, float]],
    months: list[datetime],
    property_names: Iterable[str] | None = None,
) -> datetime | None:
    if not monthly_by_prop or not months:
        return None
    scoped_names = list(property_names) if property_names is not None else list(monthly_by_prop.keys())
    scoped_data = [monthly_by_prop.get(name, {}) for name in scoped_names]
    scoped_data = [data for data in scoped_data if data]
    if not scoped_data:
        return None
    calendar_latest = months[-1]
    if any(calendar_latest in data for data in scoped_data):
        return calendar_latest
    month_set = set(months)
    observed = []
    for data in scoped_data:
        observed.extend(m for m in data.keys() if m in month_set)
    return max(observed) if observed else calendar_latest


def current_monthly_revenue(
    monthly_by_prop: dict[str, dict[datetime, float]],
    months: list[datetime],
    property_names: Iterable[str] | None = None,
) -> tuple[float, datetime | None]:
    revenue_month = latest_observed_revenue_month(monthly_by_prop, months, property_names)
    if revenue_month is None:
        return 0, None
    scoped_names = list(property_names) if property_names is not None else list(monthly_by_prop.keys())
    return sum(monthly_by_prop.get(name, {}).get(revenue_month, 0) for name in scoped_names), revenue_month


def prepare_pet_charges(all_charges: list[dict[str, Any]], selected_codes: list[str], pmc_system: str, parse_date: Any):
    import numpy as np  # noqa: WPS433
    import pandas as pd  # noqa: WPS433

    df = pd.DataFrame(all_charges)
    if df.empty:
        raise ValueError("No charges returned from API/Snowflake source")
    if "charge_code" not in df.columns:
        raise ValueError("No charge_code column in fetched charge data")

    filtered = df[df["charge_code"].astype(str).isin([str(c) for c in selected_codes])].copy()
    if filtered.empty:
        raise ValueError("No pet-related charges found after charge-code selection")

    filtered["from_date"] = filtered["charge_from_date"].apply(parse_date)
    filtered["_raw_to_date"] = filtered["charge_to_date"].apply(parse_date)
    filtered["_move_out_dt"] = filtered["move_out"].apply(parse_date) if "move_out" in filtered.columns else None
    filtered["_lease_to_dt"] = filtered["lease_to"].apply(parse_date) if "lease_to" in filtered.columns else None
    filtered["amount"] = pd.to_numeric(filtered["charge_amount"], errors="coerce").fillna(0)

    def _effective_to_date(row: Any) -> Any:
        charge_end = row.get("_raw_to_date")
        if charge_end is not None and not pd.isna(charge_end):
            return charge_end
        status = str(row.get("tenant_status", "")).strip().lower()
        if status == "current":
            return None
        move_out_dt = row.get("_move_out_dt")
        if move_out_dt is not None and not pd.isna(move_out_dt):
            return move_out_dt
        lease_to_dt = row.get("_lease_to_dt")
        if lease_to_dt is not None and not pd.isna(lease_to_dt):
            return lease_to_dt
        return None

    filtered["to_date"] = filtered.apply(_effective_to_date, axis=1)

    if pmc_system == "entrata" and "_entrata_charge_dedup_key" in filtered.columns:
        active_statuses = {"current", "past", "notice"}
        inactive_statuses = {"cancelled", "applicant", "denied", "future"}
        filtered["_status_sort"] = filtered["tenant_status"].apply(
            lambda s: 0 if str(s).strip().lower() in active_statuses else 1
        )
        filtered = filtered.sort_values("_status_sort", kind="stable")
        mask_has_key = filtered["_entrata_charge_dedup_key"].astype(str).str.strip().ne("")
        dedup_rows = filtered[mask_has_key].drop_duplicates(
            subset=["_entrata_charge_dedup_key"], keep="first"
        )
        filtered = pd.concat([dedup_rows, filtered[~mask_has_key]], ignore_index=True)
        filtered = filtered.drop(columns=["_status_sort"], errors="ignore")
        filtered = filtered[
            ~filtered["tenant_status"].apply(lambda s: str(s).strip().lower() in inactive_statuses)
        ]

    charge_cols = [
        "property_id",
        "property_name",
        "from_date",
        "to_date",
        "amount",
        "charge_code",
        "tenant_code",
        "unit_code",
        "tenant_status",
    ]
    parsed = filtered[[c for c in charge_cols if c in filtered.columns]].to_dict("records")

    code_class: dict[tuple[Any, Any], str] = {}
    by_prop_code: dict[tuple[Any, Any], list[dict[str, Any]]] = defaultdict(list)
    for rec in parsed:
        by_prop_code[(rec.get("property_name"), rec.get("charge_code"))].append(rec)

    for (pname, code), recs in by_prop_code.items():
        if pmc_system in ("real_page", "appfolio"):
            code_class[(pname, code)] = "recurring"
            continue
        spans = []
        for rec in recs:
            f, t = rec.get("from_date"), rec.get("to_date")
            if f is not None and t is not None and not pd.isna(f) and not pd.isna(t):
                try:
                    spans.append((t - f).days)
                except (TypeError, AttributeError):
                    pass
        median_span = float(np.median(spans)) if spans else None
        code_class[(pname, code)] = "recurring" if median_span is None or median_span > 60 else "onetime"

    return df, filtered, parsed, code_class


def build_monthly_by_prop(parsed_charges: list[dict[str, Any]], code_class: dict[tuple[Any, Any], str], months: list[datetime]):
    import pandas as pd  # noqa: WPS433

    latest_month = months[-1]
    monthly_by_prop: dict[str, dict[datetime, float]] = defaultdict(lambda: defaultdict(float))
    for rec in parsed_charges:
        prop = rec.get("property_name")
        amt = rec.get("amount", 0)
        from_dt = rec.get("from_date")
        to_dt = rec.get("to_date")
        if not prop or from_dt is None or pd.isna(from_dt) or amt <= 0:
            continue
        try:
            charge_start = datetime(int(from_dt.year), int(from_dt.month), 1)
        except (TypeError, ValueError, AttributeError):
            continue
        charge_type = code_class.get((prop, rec.get("charge_code")), "recurring")
        if charge_type == "onetime":
            charge_end = charge_start
        elif to_dt is not None and not pd.isna(to_dt) and isinstance(to_dt, datetime):
            charge_end = datetime(int(to_dt.year), int(to_dt.month), 1)
        else:
            charge_end = latest_month
        for month in months:
            if charge_start <= month <= charge_end:
                monthly_by_prop[prop][month] += float(amt)
    return {p: dict(v) for p, v in monthly_by_prop.items()}


def fetch_property_unit_counts(conn: Any, property_ids: list[Any], property_names: list[str]) -> tuple[dict[Any, int], dict[str, int]]:
    import snowflake.connector  # noqa: WPS433

    by_id: dict[Any, int] = {}
    by_name: dict[str, int] = {}
    if property_names:
        cur = conn.cursor(snowflake.connector.DictCursor)
        try:
            placeholders = ", ".join(["%s"] * len(property_names))
            cur.execute(
                f"""
                SELECT PROPERTY_NAME, PROPERTY_ID, PROPERTY_NUMBER_OF_DOORS AS NUM_DOORS
                FROM PROD.REPORTING.R_QUARTERLY_BUSINESS_REVIEW_REPORTING
                WHERE PROPERTY_NAME IN ({placeholders})
                  AND PROPERTY_NUMBER_OF_DOORS IS NOT NULL
                  AND PROPERTY_NUMBER_OF_DOORS > 0
                QUALIFY ROW_NUMBER() OVER (
                    PARTITION BY PROPERTY_NAME
                    ORDER BY PROPERTY_NUMBER_OF_DOORS DESC
                ) = 1
                """,
                property_names,
            )
            for row in cur.fetchall():
                pid = row.get("PROPERTY_ID")
                doors = int(row.get("NUM_DOORS") or 0)
                name = row.get("PROPERTY_NAME")
                if pid and doors:
                    by_id[pid] = doors
                if name and doors:
                    by_name[name] = doors
        finally:
            cur.close()

    missing_ids = [pid for pid in property_ids if pid not in by_id]
    if missing_ids:
        cur = conn.cursor(snowflake.connector.DictCursor)
        try:
            placeholders = ", ".join(["%s"] * len(missing_ids))
            cur.execute(
                f"""
                SELECT PROPERTY_ID, PROPERTY_NAME, PROPERTY_NUMBER_OF_DOORS AS NUM_DOORS
                FROM PROD.COMMON.D_PROPERTIES
                WHERE PROPERTY_ID IN ({placeholders})
                  AND PROPERTY_NUMBER_OF_DOORS IS NOT NULL
                  AND PROPERTY_NUMBER_OF_DOORS > 0
                """,
                missing_ids,
            )
            for row in cur.fetchall():
                pid = row.get("PROPERTY_ID")
                doors = int(row.get("NUM_DOORS") or 0)
                name = row.get("PROPERTY_NAME")
                if pid and doors:
                    by_id[pid] = doors
                if name and doors:
                    by_name[name] = doors
        finally:
            cur.close()
    return by_id, by_name


def latest_adoption_for_property(comp_data: dict[Any, Any], pid: Any, months: list[datetime], adopt_key: str) -> float | None:
    prop_comp = comp_data.get(pid, {}) if pid is not None else {}
    for month in reversed(months):
        entry = prop_comp.get(month)
        if entry and entry.get(adopt_key) is not None:
            return float(entry[adopt_key]) * 100
    return None


def build_empty_property_row(parent_id: str, parent_name: str, prop: dict[str, Any], *, status: str, error: str = "") -> dict[str, Any]:
    row = {col: "" for col in PROPERTY_COLUMNS}
    row.update(
        {
            "parent_company_name": parent_name,
            "parent_company_id": str(parent_id),
            "property_id": _prop_get(prop, "PROPERTY_ID", "property_id", default=""),
            "property_name": _prop_get(prop, "PROPERTY_NAME", "property_name", default=""),
            "pmc": prop.get("pmc") or normalize_pmc_source(_prop_get(prop, "PROPERTY_SOURCE_NAME", default="")),
            "status": status,
            "error": error,
            "processed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
    )
    return row


def process_property(
    *,
    parent_id: str,
    parent_name: str,
    prop: dict[str, Any],
    args: argparse.Namespace,
    app_imports: dict[str, Any],
    conn: Any,
) -> dict[str, Any]:
    import streamlit as st  # noqa: WPS433

    row = build_empty_property_row(parent_id, parent_name, prop, status="pending")
    pmc = row["pmc"]
    property_id = row["property_id"]
    property_name = row["property_name"]

    try:
        if pmc not in SUPPORTED_PMCS:
            raise ValueError(f"Unsupported PMC: {pmc}")

        st.session_state["pmc_system"] = pmc
        provider_props = load_provider_property(prop, app_imports)
        if not provider_props:
            raise ValueError("No provider/API-enabled property row found")

        all_charges, _fetch_log = fetch_charges_for_properties(
            provider_props,
            pmc,
            app_imports,
            verbose=args.verbose,
            workers=args.workers or 1,
        )
        row["charge_records_fetched"] = len(all_charges or [])
        if not all_charges:
            raise ValueError("No charges returned from API/Snowflake source")

        import pandas as pd  # noqa: WPS433

        raw_df = pd.DataFrame(all_charges)
        selected_codes = select_charge_codes(raw_df, args.charge_codes)
        row["selected_charge_codes"] = ",".join(selected_codes)

        months = build_month_window()
        raw_df, filtered, parsed_charges, code_class = prepare_pet_charges(
            all_charges,
            selected_codes,
            pmc,
            app_imports["parse_date"],
        )
        row["pet_charge_records_used"] = len(filtered)
        monthly_by_prop = build_monthly_by_prop(parsed_charges, code_class, months)

        launch_dt = _month_start(
            _prop_get(provider_props[0], "PROPERTY_LAUNCH_DATE", "property_launch_date", "launch_date")
            or _prop_get(prop, "PROPERTY_LAUNCH_DATE", "property_launch_date", "launch_date")
        )
        row["launch_date"] = launch_dt.strftime("%Y-%m-%d") if launch_dt else ""
        row["has_launch_date"] = bool(launch_dt)
        launch_dates = {property_name: launch_dt} if property_name and launch_dt else {}

        has_monthly = property_name in monthly_by_prop and bool(monthly_by_prop.get(property_name))
        row["has_monthly_revenue_data"] = bool(has_monthly)
        if not has_monthly:
            raise ValueError("No monthly revenue data computed for property")

        current_monthly, latest_month = current_monthly_revenue(monthly_by_prop, months, [property_name])
        latest_month = latest_month or months[-1]
        row["latest_month"] = latest_month.strftime("%Y-%m")

        property_ids = [property_id]
        prop_names = [property_name]
        unit_by_id, unit_by_name = fetch_property_unit_counts(conn, property_ids, prop_names)
        total_units = unit_by_name.get(property_name) or unit_by_id.get(property_id) or _prop_get(
            prop, "PROPERTY_NUMBER_OF_DOORS", "property_number_of_doors", default=0
        )
        if not total_units and "unit_code" in raw_df.columns:
            unit_df = raw_df[raw_df["property_name"] == property_name]
            unit_df = unit_df[unit_df["unit_code"].notna() & (unit_df["unit_code"] != "")]
            total_units = int(unit_df.drop_duplicates(subset=["property_name", "unit_code"]).shape[0])
        row["total_units"] = int(total_units or 0)

        launch_analysis = app_imports["compute_launch_analysis"](monthly_by_prop, months, launch_dates)
        analysis = launch_analysis.get(property_name, {})
        comparable = bool(analysis and app_imports["_launch_analysis_is_comparable"](analysis))
        row["is_comparable"] = comparable
        row["comparable_count"] = 1 if comparable else 0
        row["pre_months"] = int(analysis.get("n_pre", 0) or 0)
        row["post_months"] = int(analysis.get("n_post", 0) or 0)
        row["pre_baseline_monthly"] = _round_money(analysis.get("pre_avg", 0) if comparable else 0)
        row["average_monthly_lift"] = _round_money(analysis.get("diff_monthly", 0) if comparable else 0)
        row["current_monthly_revenue"] = _round_money(current_monthly)
        row["comparable_current_monthly_revenue"] = _round_money(current_monthly if comparable else 0)
        simple_lift = current_monthly - row["pre_baseline_monthly"] if comparable and row["pre_baseline_monthly"] > 0 else 0
        row["default_monthly_lift"] = _round_money(simple_lift)
        if args.avg_lift:
            selected_lift = row["average_monthly_lift"]
            methodology = "avg_lift"
        elif row["default_monthly_lift"] >= 0:
            selected_lift = row["default_monthly_lift"]
            methodology = "simple_lift"
        elif row["average_monthly_lift"] >= 0 or row["average_monthly_lift"] > row["default_monthly_lift"]:
            selected_lift = row["average_monthly_lift"]
            methodology = "avg_lift"
        else:
            selected_lift = row["default_monthly_lift"]
            methodology = "simple_lift"
        row["methodology"] = methodology
        row["selected_monthly_lift"] = _round_money(selected_lift)
        row["selected_annual_lift"] = _round_money(selected_lift * 12)
        row["selected_asset_value_5_cap"] = _round_money((selected_lift * 12) / ASSET_VALUE_CAP_RATE)

        missing_data = app_imports["fetch_missing_pet_rent_by_property"](
            raw_df,
            selected_codes,
            property_ids,
            launch_dates,
            months,
        )
        missing = missing_data.get(property_name, {}) if missing_data else {}
        missing_count = int(missing.get("missing_count", 0) or 0)
        missing_monthly = _round_money((missing.get("monthly_missing") or {}).get(latest_month, 0))
        row["missing_pet_rent_tenants"] = missing_count
        row["missing_pet_rent_monthly"] = missing_monthly
        row["missing_pet_rent_annual"] = _round_money(missing_monthly * 12)
        row["missing_pet_rent_properties"] = 1 if missing_count > 0 else 0
        row["missing_pet_rent_asset_value_5_cap"] = _round_money((missing_monthly * 12) / ASSET_VALUE_CAP_RATE)

        suspected_data = app_imports["fetch_suspected_undisclosed_by_property"](
            raw_df,
            selected_codes,
            property_ids,
            launch_dates,
            months,
        )
        suspected = suspected_data.get(property_name, {}) if suspected_data else {}
        suspected_count = int(suspected.get("missing_count", 0) or 0)
        suspected_monthly = _round_money((suspected.get("monthly_missing") or {}).get(latest_month, 0))
        row["suspected_undisclosed_profiles"] = suspected_count
        row["suspected_undisclosed_monthly"] = suspected_monthly
        row["suspected_undisclosed_annual"] = _round_money(suspected_monthly * 12)
        row["suspected_undisclosed_asset_value_5_cap"] = _round_money((suspected_monthly * 12) / ASSET_VALUE_CAP_RATE)

        comp_data = app_imports["fetch_compliance_data"]((property_id,)) if property_id else {}
        adopt_key = "resident_adoption" if args.adoption == "resident" else "unit_adoption"
        adoption_pct = latest_adoption_for_property(comp_data, property_id, months, adopt_key)
        row["adoption_metric"] = args.adoption
        row["current_adoption_pct"] = _round_money(adoption_pct) if adoption_pct is not None else ""
        if adoption_pct and adoption_pct > 0 and current_monthly > 0:
            projected = current_monthly / (adoption_pct / 100)
            row["projected_100_pct_adoption_monthly"] = _round_money(projected)
            row["additional_at_100_pct_adoption_monthly"] = _round_money(projected - current_monthly)
        else:
            row["projected_100_pct_adoption_monthly"] = 0
            row["additional_at_100_pct_adoption_monthly"] = 0

        combined = row["selected_monthly_lift"] + row["missing_pet_rent_monthly"]
        row["combined_delivered_plus_missing_monthly"] = _round_money(combined)
        row["combined_delivered_plus_missing_annual"] = _round_money(combined * 12)
        row["combined_delivered_plus_missing_asset_value_5_cap"] = _round_money((combined * 12) / ASSET_VALUE_CAP_RATE)
        row["status"] = "success"
        row["error"] = ""
        return row

    except Exception as exc:  # noqa: BLE001 - per-property export must keep going
        if args.verbose:
            import traceback

            traceback.print_exc()
        row["status"] = "failed"
        row["error"] = str(exc)
        return row


def build_aggregate_row(rows: list[dict[str, Any]], *, parent_id: str, parent_name: str) -> dict[str, Any]:
    successful = [r for r in rows if r.get("status") == "success"]
    comparable_successful = [r for r in successful if bool(r.get("is_comparable")) or _num(r.get("comparable_count")) > 0]

    agg = {col: "" for col in AGGREGATE_COLUMNS}
    agg.update(
        {
            "parent_company_name": parent_name,
            "parent_company_id": str(parent_id),
            "properties_total": len(rows),
            "properties_success": len(successful),
            "properties_failed": len([r for r in rows if r.get("status") == "failed"]),
            "pmc_systems": "; ".join(sorted({str(r.get("pmc")) for r in rows if r.get("pmc")})),
            "processed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
    )

    sum_fields = [
        "total_units",
        "pre_baseline_monthly",
        "current_monthly_revenue",
        "comparable_current_monthly_revenue",
        "default_monthly_lift",
        "average_monthly_lift",
        "selected_monthly_lift",
        "missing_pet_rent_tenants",
        "missing_pet_rent_monthly",
        "missing_pet_rent_properties",
        "suspected_undisclosed_profiles",
        "suspected_undisclosed_monthly",
    ]
    for field in sum_fields:
        agg[field] = _round_money(sum(_num(r.get(field)) for r in successful))

    agg["comparable_properties"] = int(sum(_num(r.get("comparable_count")) for r in successful))
    agg["comparable_units"] = int(sum(_num(r.get("total_units")) for r in comparable_successful))
    agg["total_units"] = int(agg["total_units"])
    agg["missing_pet_rent_tenants"] = int(agg["missing_pet_rent_tenants"])
    agg["missing_pet_rent_properties"] = int(agg["missing_pet_rent_properties"])
    agg["suspected_undisclosed_profiles"] = int(agg["suspected_undisclosed_profiles"])
    agg["selected_annual_lift"] = _round_money(agg["selected_monthly_lift"] * 12)
    agg["selected_asset_value_5_cap"] = _round_money(agg["selected_annual_lift"] / ASSET_VALUE_CAP_RATE)
    agg["missing_pet_rent_annual"] = _round_money(agg["missing_pet_rent_monthly"] * 12)
    agg["missing_pet_rent_asset_value_5_cap"] = _round_money(agg["missing_pet_rent_annual"] / ASSET_VALUE_CAP_RATE)
    agg["suspected_undisclosed_annual"] = _round_money(agg["suspected_undisclosed_monthly"] * 12)
    agg["suspected_undisclosed_asset_value_5_cap"] = _round_money(
        agg["suspected_undisclosed_annual"] / ASSET_VALUE_CAP_RATE
    )
    combined = agg["selected_monthly_lift"] + agg["missing_pet_rent_monthly"]
    agg["combined_delivered_plus_missing_monthly"] = _round_money(combined)
    agg["combined_delivered_plus_missing_annual"] = _round_money(combined * 12)
    agg["combined_delivered_plus_missing_asset_value_5_cap"] = _round_money(
        agg["combined_delivered_plus_missing_annual"] / ASSET_VALUE_CAP_RATE
    )
    return agg


def write_workbook(output_path: Path, property_rows: list[dict[str, Any]], aggregate_row: dict[str, Any]) -> None:
    import pandas as pd  # noqa: WPS433

    output_path.parent.mkdir(parents=True, exist_ok=True)
    properties_df = pd.DataFrame(property_rows)
    aggregate_df = pd.DataFrame([aggregate_row])
    for col in PROPERTY_COLUMNS:
        if col not in properties_df.columns:
            properties_df[col] = ""
    for col in AGGREGATE_COLUMNS:
        if col not in aggregate_df.columns:
            aggregate_df[col] = ""
    properties_df = properties_df[PROPERTY_COLUMNS]
    aggregate_df = aggregate_df[AGGREGATE_COLUMNS]

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        properties_df.to_excel(writer, sheet_name="properties", index=False)
        aggregate_df.to_excel(writer, sheet_name="aggregate", index=False)
        for sheet_name in ("properties", "aggregate"):
            ws = writer.book[sheet_name]
            ws.freeze_panes = "A2"
            for column_cells in ws.columns:
                header = str(column_cells[0].value or "")
                width = min(max(len(header) + 2, 12), 34)
                ws.column_dimensions[column_cells[0].column_letter].width = width


def write_property_csv(output_path: Path, property_rows: list[dict[str, Any]]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=PROPERTY_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in property_rows:
            writer.writerow(row)


def process_parent(parent_id: str, args: argparse.Namespace, app_imports: dict[str, Any], conn: Any) -> tuple[Path, Path, dict[str, Any]]:
    parent_name = args.label or detect_parent_label(conn, parent_id)
    properties = load_parent_properties(conn, parent_id, force_pmc=args.pmc)
    if args.limit:
        properties = properties[: args.limit]

    if not properties:
        row = build_empty_property_row(parent_id, parent_name, {}, status="failed", error="No supported properties found")
        aggregate = build_aggregate_row([row], parent_id=parent_id, parent_name=parent_name)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        stem = f"parent_property_value_{sanitize_filename(parent_name)}_{sanitize_filename(parent_id)}_{timestamp}"
        workbook_path = Path(args.output) / f"{stem}.xlsx"
        csv_path = Path(args.output) / f"{stem}_properties.csv"
        write_workbook(workbook_path, [row], aggregate)
        write_property_csv(csv_path, [row])
        return workbook_path, csv_path, aggregate

    print(f"Parent: {parent_name} ({parent_id})")
    print(f"Supported properties found: {len(properties):,}")
    by_pmc: dict[str, int] = defaultdict(int)
    for prop in properties:
        by_pmc[prop["pmc"]] += 1
    print("PMC mix: " + ", ".join(f"{pmc}={count}" for pmc, count in sorted(by_pmc.items())))
    print("-" * 60)

    rows: list[dict[str, Any]] = []
    for idx, prop in enumerate(properties, 1):
        pname = _prop_get(prop, "PROPERTY_NAME", "property_name", default="")
        pid = _prop_get(prop, "PROPERTY_ID", "property_id", default="")
        pmc = prop.get("pmc", "unknown")
        print(f"[{idx}/{len(properties)}] {pname} ({pid}) — {pmc}")
        row = process_property(
            parent_id=parent_id,
            parent_name=parent_name,
            prop=prop,
            args=args,
            app_imports=app_imports,
            conn=conn,
        )
        rows.append(row)
        if row["status"] == "success":
            print(
                f"  ✓ units={row['total_units']} comparable={row['comparable_count']} "
                f"lift=${_num(row['selected_monthly_lift']):,.0f}/mo "
                f"missing=${_num(row['missing_pet_rent_monthly']):,.0f}/mo "
                f"suspected=${_num(row['suspected_undisclosed_monthly']):,.0f}/mo"
            )
        else:
            print(f"  ✗ {row['error']}")

    aggregate = build_aggregate_row(rows, parent_id=parent_id, parent_name=parent_name)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = f"parent_property_value_{sanitize_filename(parent_name)}_{sanitize_filename(parent_id)}_{timestamp}"
    workbook_path = Path(args.output) / f"{stem}.xlsx"
    csv_path = Path(args.output) / f"{stem}_properties.csv"
    write_workbook(workbook_path, rows, aggregate)
    write_property_csv(csv_path, rows)
    return workbook_path, csv_path, aggregate


def main() -> None:
    parser = argparse.ArgumentParser(description="Export parent-company property-level pet-rent value workbook")
    parser.add_argument("--ids", type=str, help="Comma-separated parent company ancestry IDs")
    parser.add_argument("--file", type=str, help="File with one parent company ancestry ID per line")
    parser.add_argument("--output", default="./parent_property_exports", help="Output folder")
    parser.add_argument("--charge-codes", default=None, help="Comma-separated pet charge codes; overrides auto-detection")
    parser.add_argument("--pmc", choices=SUPPORTED_PMCS, default=None, help="Force all properties to one PMC path")
    parser.add_argument("--label", default=None, help="Override parent label for output filename/workbook")
    parser.add_argument("--adoption", choices=["unit", "resident"], default="unit", help="Adoption metric for projection")
    parser.add_argument("--avg-lift", action="store_true", help="Use average monthly lift as selected lift")
    parser.add_argument("--workers", type=int, default=1, help="Concurrent Yardi/Entrata fetches per property batch")
    parser.add_argument("--limit", type=int, default=None, help="Process only first N properties for smoke testing")
    parser.add_argument("--verbose", action="store_true", help="Show tracebacks and provider fetch chatter")
    args = parser.parse_args()

    parent_ids = parse_ids(args)
    Path(args.output).mkdir(parents=True, exist_ok=True)

    app_imports = load_app_imports()
    conn = app_imports["get_snowflake_connection"]()

    outputs = []
    for parent_id in parent_ids:
        workbook_path, csv_path, aggregate = process_parent(parent_id, args, app_imports, conn)
        outputs.append((workbook_path, csv_path, aggregate))
        print("\n" + "=" * 60)
        print(f"Workbook: {workbook_path.absolute()}")
        print(f"Property CSV copy: {csv_path.absolute()}")
        print(
            "Aggregate: "
            f"{aggregate['properties_success']}/{aggregate['properties_total']} succeeded, "
            f"{aggregate['comparable_properties']} comparable, "
            f"{aggregate['total_units']:,} total units, "
            f"{aggregate['comparable_units']:,} comparable units, "
            f"${_num(aggregate['selected_monthly_lift']):,.0f}/mo lift, "
            f"${_num(aggregate['missing_pet_rent_monthly']):,.0f}/mo missing rent"
        )
        print("=" * 60 + "\n")

    if len(outputs) > 1:
        print("All outputs:")
        for workbook_path, csv_path, _aggregate in outputs:
            print(f"  - {workbook_path.absolute()}")
            print(f"    {csv_path.absolute()}")


if __name__ == "__main__":
    main()
