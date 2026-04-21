#!/usr/bin/env python3
"""
Batch PDF Generator for PetScreening Value Reports

Auto-detects PMC system (Yardi/Entrata) for each ID and calls the appropriate API.

Usage:
    python batch_pdf.py --ids 123,456,789
    python batch_pdf.py --ids 123,456 --type parent --adoption resident
    python batch_pdf.py --file ids.txt --output ./reports

Options:
    --ids           Comma-separated list of IDs
    --file          Path to file with one ID per line
    --type          'property' (default) or 'parent' (parent_company)
    --adoption      'unit' (default) or 'resident'
    --output        Output folder (default: ./batch_pdfs)
    --avg-lift      Use average monthly lift methodology (for seasonal portfolios)
    --include-pm    Include property manager appendix

Outputs:
    - PDFs in output folder
    - batch_log_YYYYMMDD_HHMMSS.csv with status for each ID
"""

import argparse
import csv
import os
import sys
import logging
import warnings
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Suppress streamlit warnings when running as script
os.environ["STREAMLIT_SERVER_HEADLESS"] = "true"
warnings.filterwarnings("ignore", message=".*missing ScriptRunContext.*")
warnings.filterwarnings("ignore", message=".*Session state does not function.*")
warnings.filterwarnings("ignore", category=UserWarning)
logging.getLogger("streamlit").setLevel(logging.ERROR)

# Add the app directory to path so we can import from app.py
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd
import numpy as np
import requests
from collections import defaultdict


class FakeProgressBar:
    """Dummy progress bar for batch mode."""
    def progress(self, val):
        pass


class FakeStatusText:
    """Dummy status text for batch mode."""
    def __init__(self, verbose=True):
        self.verbose = verbose
    
    def text(self, msg):
        if self.verbose:
            print(f"    {msg}")


def detect_pmc_for_id(conn, id_val, id_type="property"):
    """
    Auto-detect which PMC system has this ID using PROD.COMMON.D_PROPERTIES.
    Returns: (pmc_system, prop_count, label) or (None, 0, None) if not found.
    """
    cur = conn.cursor()
    
    if id_type == "property":
        cur.execute("""
            SELECT PROPERTY_NAME, PROPERTY_SOURCE_NAME
            FROM PROD.COMMON.D_PROPERTIES
            WHERE PROPERTY_ID = %s
        """, (id_val,))
        row = cur.fetchone()
        cur.close()
        
        if row:
            pname, source = row
            pmc = (source or "unknown").lower().strip()
            return pmc, 1, pname
        return None, 0, None
    
    else:  # parent
        cur.execute("""
            SELECT 
                PARENT_COMPANY_NAME,
                PROPERTY_SOURCE_NAME,
                COUNT(DISTINCT PROPERTY_ID) as cnt
            FROM PROD.COMMON.D_PROPERTIES
            WHERE PARENT_COMPANY_ANCESTRY_ID = %s
            GROUP BY PARENT_COMPANY_NAME, PROPERTY_SOURCE_NAME
            ORDER BY cnt DESC
        """, (id_val,))
        rows = cur.fetchall()
        cur.close()
        
        if rows:
            best_row = rows[0]
            pname, source, cnt = best_row
            pmc = (source or "unknown").lower().strip()
            return pmc, cnt, pname
        return None, 0, None


def fetch_charges_for_properties(properties, pmc_system, app_imports, verbose=True):
    """
    Fetch charges via API for a list of properties.
    Returns: (all_charges_list, results_log)
    """
    progress_bar = FakeProgressBar()
    status_text = FakeStatusText(verbose=verbose)
    
    if pmc_system == "entrata":
        fetch_fn = app_imports["fetch_entrata_for_properties"]
    else:
        fetch_fn = app_imports["fetch_rentroll_for_properties"]
    
    all_charges, results_log = fetch_fn(properties, progress_bar, status_text)
    return all_charges, results_log


def process_single_id(id_val, id_type, pmc_system, label, args, output_path, app_imports, conn):
    """Process a single ID and generate PDF. Returns dict with results."""
    
    get_snowflake_connection = app_imports["get_snowflake_connection"]
    load_properties_for_selection = app_imports["load_properties_for_selection"]
    load_entrata_properties_for_selection = app_imports["load_entrata_properties_for_selection"]
    compute_launch_analysis = app_imports["compute_launch_analysis"]
    fetch_compliance_data = app_imports["fetch_compliance_data"]
    fetch_missing_pet_rent_by_property = app_imports["fetch_missing_pet_rent_by_property"]
    fetch_suspected_undisclosed_by_property = app_imports["fetch_suspected_undisclosed_by_property"]
    generate_tranche_pdf = app_imports["generate_tranche_pdf"]
    _build_property_id_lookup = app_imports["_build_property_id_lookup"]
    
    result = {
        "id": id_val,
        "type": id_type,
        "pmc": pmc_system,
        "label": label,
        "status": "pending",
        "properties_found": 0,
        "properties_with_data": 0,
        "comparable_count": 0,
        "monthly_lift": 0,
        "uncollected_tenants": 0,
        "suspected_undisclosed": 0,
        "pdf_filename": "",
        "error": "",
    }
    
    try:
        # Load properties
        if pmc_system == "entrata":
            if id_type == "parent":
                props_df = load_entrata_properties_for_selection(parent_company_name=label)
            else:
                props_df = load_entrata_properties_for_selection(property_id=id_val)
        else:  # yardi or unknown — try yardi
            if id_type == "parent":
                props_df = load_properties_for_selection(parent_company_name=label)
            else:
                props_df = load_properties_for_selection(property_id=id_val)
        
        # Handle both DataFrame and list returns
        if props_df is None:
            result["status"] = "failed"
            result["error"] = "No properties found in selection"
            return result
        
        if isinstance(props_df, pd.DataFrame):
            if props_df.empty:
                result["status"] = "failed"
                result["error"] = "No properties found in selection"
                return result
            properties = props_df.to_dict('records')
        elif isinstance(props_df, list):
            if len(props_df) == 0:
                result["status"] = "failed"
                result["error"] = "No properties found in selection"
                return result
            properties = props_df
        else:
            result["status"] = "failed"
            result["error"] = f"Unexpected properties type: {type(props_df)}"
            return result
        property_ids = [p.get("PROPERTY_ID") or p.get("property_id") for p in properties]
        result["properties_found"] = len(property_ids)
        
        print(f"    Fetching charges via {pmc_system.upper()} API...")
        
        # Fetch charges via API
        all_charges, fetch_log = fetch_charges_for_properties(
            properties, pmc_system, app_imports, verbose=False
        )
        
        if not all_charges:
            result["status"] = "failed"
            result["error"] = "No charges returned from API"
            return result
        
        print(f"    Got {len(all_charges):,} charge records")
        
        # Build DataFrame
        df = pd.DataFrame(all_charges)
        
        # Get launch dates from the properties we already loaded
        # Note: Snowflake returns lowercase keys from DictCursor
        launch_dates = {}
        for prop in properties:
            # Try both cases for property name
            pname = prop.get("PROPERTY_NAME") or prop.get("property_name")
            # Try multiple key variations for launch date
            ldate = (prop.get("PROPERTY_LAUNCH_DATE") or 
                     prop.get("property_launch_date") or 
                     prop.get("launch_date"))
            if pname and ldate:
                try:
                    if isinstance(ldate, str):
                        ldate = datetime.strptime(ldate[:10], "%Y-%m-%d")
                    launch_dates[pname] = datetime(ldate.year, ldate.month, 1)
                except:
                    pass
        
        print(f"    Launch dates found for {len(launch_dates)}/{len(properties)} properties")
        
        # Build monthly_by_prop
        today = datetime.now()
        window_end = datetime(today.year, today.month, 1)
        window_start = datetime(today.year - 5, today.month, 1)
        months = [m.to_pydatetime() for m in pd.date_range(start=window_start, end=window_end, freq='MS')]
        
        # Import parse_date from app
        parse_date = app_imports["parse_date"]
        
        # Filter for pet-related charge codes (same as app)
        if "charge_code" not in df.columns:
            result["status"] = "failed"
            result["error"] = "No charge_code column in data"
            return result
        
        # Filter to pet charges only
        pet_mask = df["charge_code"].str.lower().str.contains("pet", na=False)
        filtered = df[pet_mask].copy()
        
        if filtered.empty:
            result["status"] = "failed"
            result["error"] = "No pet-related charges found"
            return result
        
        print(f"    Found {len(filtered):,} pet charges")
        
        selected_codes = filtered["charge_code"].unique().tolist()
        
        # Normalize dates & amounts (same as app lines ~6993-7019)
        filtered["from_date"] = filtered["charge_from_date"].apply(parse_date)
        filtered["_raw_to_date"] = filtered["charge_to_date"].apply(parse_date)
        filtered["_move_out_dt"] = filtered["move_out"].apply(parse_date) if "move_out" in filtered.columns else None
        filtered["_lease_to_dt"] = filtered["lease_to"].apply(parse_date) if "lease_to" in filtered.columns else None
        filtered["amount"] = pd.to_numeric(filtered["charge_amount"], errors="coerce").fillna(0)
        
        # Effective to_date: coalesce(charge_to_date, move_out, lease_to)
        def _effective_to_date(row):
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
        
        # Convert to parsed_charges list (same structure as app)
        charge_cols = ["property_id", "property_name", "from_date", "to_date", "amount", 
                       "charge_code", "tenant_code", "unit_code", "tenant_status"]
        available_cols = [c for c in charge_cols if c in filtered.columns]
        parsed_charges = filtered[available_cols].to_dict("records")
        
        # Classify charge codes (same logic as app)
        _code_class = {}
        _by_prop_code = defaultdict(list)
        for rec in parsed_charges:
            key = (rec.get("property_name"), rec.get("charge_code"))
            _by_prop_code[key].append(rec)
        
        for (pname, code), recs in _by_prop_code.items():
            spans = []
            for r in recs:
                f, t = r.get("from_date"), r.get("to_date")
                if f and t and not pd.isna(f) and not pd.isna(t):
                    try:
                        spans.append((t - f).days)
                    except (TypeError, AttributeError):
                        pass
            median_span = float(np.median(spans)) if spans else None
            if median_span is None:
                _code_class[(pname, code)] = "recurring"
            else:
                _code_class[(pname, code)] = "recurring" if median_span > 60 else "onetime"
        
        # Aggregate charges into monthly buckets (same as app)
        monthly_by_prop = defaultdict(lambda: defaultdict(float))
        
        for rec in parsed_charges:
            prop = rec.get("property_name")
            amt = rec.get("amount", 0)
            from_dt = rec.get("from_date")
            to_dt = rec.get("to_date")
            
            if from_dt is None or pd.isna(from_dt) or amt <= 0:
                continue
            
            try:
                charge_start = datetime(int(from_dt.year), int(from_dt.month), 1)
            except:
                continue
            
            charge_type = _code_class.get((prop, rec.get("charge_code")), "recurring")
            if charge_type == "onetime":
                charge_end = charge_start
            elif to_dt is not None and not pd.isna(to_dt) and isinstance(to_dt, datetime):
                charge_end = datetime(int(to_dt.year), int(to_dt.month), 1)
            else:
                charge_end = window_end
            
            for month in months:
                if charge_start <= month <= charge_end:
                    monthly_by_prop[prop][month] += amt
        
        monthly_by_prop = {p: dict(v) for p, v in monthly_by_prop.items()}
        result["properties_with_data"] = len(monthly_by_prop)
        
        if not monthly_by_prop:
            result["status"] = "failed"
            result["error"] = "No monthly revenue data computed"
            return result
        
        # Compute launch analysis
        launch_analysis = compute_launch_analysis(monthly_by_prop, months, launch_dates)
        
        comparable = {p: a for p, a in launch_analysis.items()
                      if a["n_pre"] > 0 and a.get("baseline_reliable", True) and a.get("baseline_meaningful", True)}
        
        # Compute metrics
        pre_baseline = sum(a["pre_avg"] for a in comparable.values()) if comparable else 0
        t1_mo_avg = sum(a["diff_monthly"] for a in comparable.values()) if comparable else 0  # avg lift
        t1_total = sum(a["diff_total"] for a in comparable.values()) if comparable else 0
        t1_months = max((a["n_post"] for a in comparable.values()), default=0)
        
        latest_month = months[-1]
        current_monthly_rev = sum(monthly_by_prop[p].get(latest_month, 0) for p in monthly_by_prop)
        
        # Simple lift (current - pre)
        t1_mo_simple = current_monthly_rev - pre_baseline if pre_baseline > 0 else 0
        
        # Auto-select methodology: use simple lift unless negative, then try avg lift, then use least negative
        if args.avg_lift:
            # User explicitly requested avg lift
            use_avg_lift = True
            t1_mo = t1_mo_avg
        elif t1_mo_simple >= 0:
            # Simple lift is positive - use it
            use_avg_lift = False
            t1_mo = t1_mo_simple
        elif t1_mo_avg >= 0:
            # Simple is negative but avg is positive - switch to avg
            use_avg_lift = True
            t1_mo = t1_mo_avg
            print(f"    Auto-switched to avg lift (simple was negative)")
        else:
            # Both negative - use whichever is least negative
            if t1_mo_avg > t1_mo_simple:
                use_avg_lift = True
                t1_mo = t1_mo_avg
                print(f"    Auto-switched to avg lift (least negative)")
            else:
                use_avg_lift = False
                t1_mo = t1_mo_simple
        
        t1_pct = (t1_mo / pre_baseline * 100) if pre_baseline > 0 else 0
        
        result["comparable_count"] = len(comparable)
        result["monthly_lift"] = round(t1_mo, 2)
        result["methodology"] = "avg_lift" if use_avg_lift else "simple_lift"
        
        # Fetch compliance data
        pid_lookup = _build_property_id_lookup(all_charges)
        all_pids = list(set(pid_lookup.values()))
        comp_data = fetch_compliance_data(tuple(sorted(all_pids))) if all_pids else {}
        
        # Compute adoption
        adopt_key = "resident_adoption" if args.adoption == "resident" else "unit_adoption"
        adopt_type_label = "Resident" if args.adoption == "resident" else "Unit"
        
        projected_100 = {}
        for pname in monthly_by_prop:
            pid = pid_lookup.get(pname)
            if not pid:
                continue
            prop_comp = comp_data.get(pid, {})
            latest_adopt = None
            for m in reversed(months):
                entry = prop_comp.get(m)
                if entry and entry.get(adopt_key) is not None:
                    latest_adopt = entry[adopt_key] * 100
                    break
            latest_rev = monthly_by_prop.get(pname, {}).get(latest_month, 0)
            if latest_adopt and latest_adopt > 0 and latest_rev > 0:
                projected = latest_rev / (latest_adopt / 100)
                projected_100[pname] = {
                    "current_rev": latest_rev,
                    "current_adoption": latest_adopt,
                    "projected_rev_100": projected,
                }
        
        total_projected = sum(p["projected_rev_100"] for p in projected_100.values())
        avg_adoption = (sum(p["current_adoption"] for p in projected_100.values()) / len(projected_100)) if projected_100 else None
        
        # Fetch missing pet rent
        print("    Fetching missing pet rent data...")
        missing_rent_data = fetch_missing_pet_rent_by_property(
            df, selected_codes, property_ids, launch_dates, months
        ) if selected_codes else {}
        
        t2_tenants = sum(v["missing_count"] for v in missing_rent_data.values()) if missing_rent_data else 0
        t2_mo = sum(v["monthly_missing"].get(latest_month, 0) for v in missing_rent_data.values()) if missing_rent_data else 0
        t2_props = sum(1 for v in missing_rent_data.values() if v["missing_count"] > 0) if missing_rent_data else 0
        
        result["uncollected_tenants"] = t2_tenants
        
        # Fetch suspected undisclosed
        print("    Fetching suspected undisclosed data...")
        suspected_data = fetch_suspected_undisclosed_by_property(
            df, selected_codes, property_ids, launch_dates, months
        ) if selected_codes else {}
        
        su_total_profiles = sum(v["missing_count"] for v in suspected_data.values()) if suspected_data else 0
        su_current_mo = sum(v["monthly_missing"].get(latest_month, 0) for v in suspected_data.values()) if suspected_data else 0
        
        result["suspected_undisclosed"] = su_total_profiles
        
        # Combined metrics
        t1_t2_combined = t1_mo + t2_mo
        t1_t2_pct = (t1_t2_combined / pre_baseline * 100) if pre_baseline > 0 else 0
        t3_additional = total_projected - current_monthly_rev if total_projected > 0 else 0
        t3_total_impact = t1_mo + t2_mo + t3_additional
        t3_pct = (t3_total_impact / pre_baseline * 100) if pre_baseline > 0 else 0
        
        # Get total units — same logic as app: QBR table first, then D_PROPERTIES fallback
        total_units = 0
        property_doors = {}  # {PROPERTY_ID: door_count}
        property_doors_by_name = {}  # {PROPERTY_NAME: door_count}
        _units_from_doors = False
        
        try:
            import snowflake.connector
            _prop_names_for_doors = list(monthly_by_prop.keys())
            _pids_str = ",".join(str(p) for p in property_ids)
            
            if _prop_names_for_doors:
                _doors_cur = conn.cursor(snowflake.connector.DictCursor)
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
                            property_doors[_pid] = _num
                        if _pn:
                            property_doors_by_name[_pn] = _num
                    total_units = sum(property_doors_by_name.values())
                    _units_from_doors = True
                _doors_cur.close()
            
            # Fallback: D_PROPERTIES by property_id
            if not _units_from_doors and property_ids:
                _doors_cur2 = conn.cursor(snowflake.connector.DictCursor)
                _doors_cur2.execute(f"""
                    SELECT PROPERTY_ID, PROPERTY_NUMBER_OF_DOORS AS NUM_DOORS
                    FROM PROD.COMMON.D_PROPERTIES
                    WHERE PROPERTY_ID IN ({_pids_str})
                      AND PROPERTY_NUMBER_OF_DOORS IS NOT NULL
                      AND PROPERTY_NUMBER_OF_DOORS > 0
                """)
                _doors_rows2 = _doors_cur2.fetchall()
                if _doors_rows2:
                    for r in _doors_rows2:
                        property_doors[int(r["PROPERTY_ID"])] = int(r["NUM_DOORS"])
                    total_units = sum(property_doors.values())
                    _units_from_doors = True
                _doors_cur2.close()
        except Exception as e:
            if args.verbose:
                print(f"    Warning: Could not fetch unit counts: {e}")
        
        # Build property_name -> door count mapping from pid-based data
        if not property_doors_by_name:
            for pname, pid_val in pid_lookup.items():
                if pid_val in property_doors:
                    property_doors_by_name[pname] = property_doors[pid_val]
        
        # Last resort: unique unit_codes from charges
        if not _units_from_doors and "unit_code" in df.columns:
            _unit_df = df[df["property_name"].isin(monthly_by_prop.keys())]
            _unit_df = _unit_df[_unit_df["unit_code"].notna() & (_unit_df["unit_code"] != "")]
            total_units = _unit_df.drop_duplicates(subset=["property_name", "unit_code"]).shape[0]
        
        # Generate PDF
        print("    Generating PDF...")
        today_str = datetime.now().strftime("%B %d, %Y")
        
        pdf_bytes = generate_tranche_pdf(
            label=label,
            today_str=today_str,
            pre_baseline=pre_baseline,
            comparable_count=len(comparable),
            t1_mo=t1_mo,
            t1_total=t1_total,
            t1_pct=t1_pct,
            t1_months=t1_months,
            t2_tenants=t2_tenants,
            t2_mo=t2_mo,
            t2_props=t2_props,
            t1_t2_combined=t1_t2_combined,
            t1_t2_pct=t1_t2_pct,
            t3_adoption=avg_adoption,
            t3_additional=t3_additional,
            t3_total_impact=t3_total_impact,
            t3_pct=t3_pct,
            adopt_type_label=adopt_type_label,
            current_monthly_rev=current_monthly_rev,
            n_props_total=len(property_ids),
            n_with_launch=len(launch_dates),
            n_props_with_data=len(monthly_by_prop),
            include_pm=args.include_pm,
            pm_rows=None,
            su_total_profiles=su_total_profiles,
            su_current_mo=su_current_mo,
            total_projected=total_projected,
            missing_rent_data=missing_rent_data,
            total_units=total_units,
            comparable_data=comparable,
            total_portfolio_units=total_units,
            property_doors=property_doors_by_name,
            monthly_revenue_series=None,
            comparable_current_rev=sum(monthly_by_prop[p].get(latest_month, 0) for p in comparable.keys()) if comparable else 0,
            pmc_system=pmc_system,
            use_avg_lift=use_avg_lift,
            asset_class="conventional",
            monthly_by_prop=monthly_by_prop,
            latest_month=latest_month,
            selected_charge_codes=selected_codes,
        )
        
        # Save PDF with detailed naming and PMC folder structure
        # Get parent company info
        parent_name = ""
        parent_id = ""
        for prop in properties:
            pn = prop.get("PARENT_COMPANY_NAME") or prop.get("parent_company_name") or ""
            pi = prop.get("PARENT_COMPANY_ANCESTRY_ID") or prop.get("parent_company_ancestry_id") or prop.get("PARENT_COMPANY_ID") or prop.get("parent_company_id") or ""
            if pn:
                parent_name = pn
            if pi:
                parent_id = str(pi)
            break
        
        # Create PMC subfolder (yardi/entrata)
        pmc_folder = output_path / pmc_system.lower()
        pmc_folder.mkdir(parents=True, exist_ok=True)
        
        # Build filename with all searchable info
        # Format: PropertyName_PropID_ParentName_ParentID_YYYYMMDD.pdf
        def sanitize(s, max_len=40):
            return s.replace(" ", "_").replace("/", "-").replace("\\", "-").replace(":", "-").replace(",", "")[:max_len]
        
        date_str = datetime.now().strftime('%Y%m%d')
        prop_name_safe = sanitize(label.split(" - ", 1)[-1] if " - " in label else label, 35)
        parent_name_safe = sanitize(parent_name, 25) if parent_name else "NoParent"
        
        filename = f"{prop_name_safe}_{id_val}_{parent_name_safe}_{parent_id}_{date_str}.pdf"
        filepath = pmc_folder / filename
        
        with open(filepath, "wb") as f:
            f.write(pdf_bytes)
        
        result["status"] = "success"
        result["pdf_filename"] = f"{pmc_system}/{filename}"
        result["parent_company_name"] = parent_name
        result["parent_company_id"] = parent_id
        
    except Exception as e:
        import traceback
        result["status"] = "failed"
        result["error"] = str(e)
        if args.verbose if hasattr(args, 'verbose') else False:
            traceback.print_exc()
    
    return result


def main():
    parser = argparse.ArgumentParser(description="Batch generate PetScreening Value Report PDFs")
    parser.add_argument("--ids", type=str, help="Comma-separated IDs")
    parser.add_argument("--file", type=str, help="File with one ID per line")
    parser.add_argument("--type", type=str, choices=["property", "parent"], default="property",
                        help="ID type: 'property' (default) or 'parent' (parent_company)")
    parser.add_argument("--adoption", type=str, choices=["unit", "resident"], default="unit",
                        help="Adoption metric: 'unit' or 'resident'")
    parser.add_argument("--output", type=str, default="./batch_pdfs", help="Output folder")
    parser.add_argument("--avg-lift", action="store_true", help="Use average monthly lift methodology")
    parser.add_argument("--include-pm", action="store_true", help="Include property manager appendix")
    parser.add_argument("--verbose", action="store_true", help="Show detailed error traces")
    parser.add_argument("--pmc", type=str, choices=["yardi", "entrata"], default=None,
                        help="Force PMC system (yardi or entrata). Overrides auto-detection.")
    
    args = parser.parse_args()
    
    # Get IDs
    ids = []
    if args.ids:
        ids = [x.strip() for x in args.ids.split(",") if x.strip()]
    elif args.file:
        with open(args.file, "r") as f:
            ids = [line.strip() for line in f if line.strip() and not line.startswith("#")]
    else:
        print("Error: Provide --ids or --file")
        sys.exit(1)
    
    if not ids:
        print("Error: No IDs provided")
        sys.exit(1)
    
    print(f"Processing {len(ids)} {args.type} IDs...")
    print(f"Adoption metric: {args.adoption}")
    print(f"Methodology: {'Average Monthly Lift' if args.avg_lift else 'Simple Lift (Current - Pre)'}")
    if args.pmc:
        print(f"PMC forced: {args.pmc}")
    print(f"Output folder: {args.output}")
    print("-" * 60)
    
    # Create output folder
    output_path = Path(args.output)
    output_path.mkdir(parents=True, exist_ok=True)
    
    # Import app functions
    from app import (
        get_snowflake_connection,
        load_properties_for_selection,
        load_entrata_properties_for_selection,
        compute_launch_analysis,
        fetch_compliance_data,
        fetch_missing_pet_rent_by_property,
        fetch_suspected_undisclosed_by_property,
        generate_tranche_pdf,
        _build_property_id_lookup,
        fetch_rentroll_for_properties,
        fetch_entrata_for_properties,
        parse_date,
    )
    
    app_imports = {
        "get_snowflake_connection": get_snowflake_connection,
        "load_properties_for_selection": load_properties_for_selection,
        "load_entrata_properties_for_selection": load_entrata_properties_for_selection,
        "compute_launch_analysis": compute_launch_analysis,
        "fetch_compliance_data": fetch_compliance_data,
        "fetch_missing_pet_rent_by_property": fetch_missing_pet_rent_by_property,
        "fetch_suspected_undisclosed_by_property": fetch_suspected_undisclosed_by_property,
        "generate_tranche_pdf": generate_tranche_pdf,
        "_build_property_id_lookup": _build_property_id_lookup,
        "fetch_rentroll_for_properties": fetch_rentroll_for_properties,
        "fetch_entrata_for_properties": fetch_entrata_for_properties,
        "parse_date": parse_date,
    }
    
    # Get a connection
    conn = get_snowflake_connection()
    
    results = []
    success_count = 0
    error_count = 0
    
    for idx, id_val in enumerate(ids, 1):
        print(f"\n[{idx}/{len(ids)}] Processing {args.type} ID: {id_val}")
        
        # Auto-detect PMC from PROPERTY_SOURCE_NAME (or use forced --pmc)
        pmc_system, prop_count, label = detect_pmc_for_id(conn, id_val, args.type)
        
        if args.pmc:
            pmc_system = args.pmc
            print(f"  PMC forced to: {args.pmc}")
        
        if pmc_system is None:
            print(f"  ✗ Not found in database")
            results.append({
                "id": id_val,
                "type": args.type,
                "pmc": "not_found",
                "label": "",
                "parent_company_name": "",
                "parent_company_id": "",
                "status": "failed",
                "methodology": "",
                "properties_found": 0,
                "properties_with_data": 0,
                "comparable_count": 0,
                "monthly_lift": 0,
                "uncollected_tenants": 0,
                "suspected_undisclosed": 0,
                "pdf_filename": "",
                "error": "ID not found in PROD.COMMON.D_PROPERTIES",
            })
            error_count += 1
            continue
        
        print(f"  Found: {label} ({pmc_system.upper()}, {prop_count} properties)")
        
        # Check if PMC is supported
        if pmc_system not in ("yardi", "entrata"):
            print(f"  ✗ Unsupported PMC: {pmc_system.upper()} (only Yardi/Entrata supported)")
            results.append({
                "id": id_val,
                "type": args.type,
                "pmc": pmc_system,
                "label": label,
                "parent_company_name": "",
                "parent_company_id": "",
                "status": "skipped",
                "methodology": "",
                "properties_found": prop_count,
                "properties_with_data": 0,
                "comparable_count": 0,
                "monthly_lift": 0,
                "uncollected_tenants": 0,
                "suspected_undisclosed": 0,
                "pdf_filename": "",
                "error": f"Unsupported PMC: {pmc_system}",
            })
            error_count += 1
            continue
        
        # Process the ID
        result = process_single_id(id_val, args.type, pmc_system, label, args, output_path, app_imports, conn)
        results.append(result)
        
        if result["status"] == "success":
            print(f"  ✓ Saved: {result['pdf_filename']}")
            print(f"    {result['comparable_count']} comparable, ${result['monthly_lift']:,.0f}/mo lift, "
                  f"{result['uncollected_tenants']} uncollected, {result['suspected_undisclosed']} suspected")
            success_count += 1
        else:
            print(f"  ✗ Error: {result['error']}")
            error_count += 1
    
    # Write summary CSV
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_filename = f"batch_log_{timestamp}.csv"
    csv_path = output_path / csv_filename
    
    with open(csv_path, "w", newline="") as f:
        fieldnames = [
            "id", "type", "pmc", "label", "parent_company_name", "parent_company_id",
            "status", "methodology",
            "properties_found", "properties_with_data", "comparable_count",
            "monthly_lift", "uncollected_tenants", "suspected_undisclosed",
            "pdf_filename", "error"
        ]
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(results)
    
    print("\n" + "=" * 60)
    print(f"Complete: {success_count} succeeded, {error_count} failed")
    print(f"PDFs saved to: {output_path.absolute()}")
    print(f"Log saved to: {csv_path.absolute()}")


if __name__ == "__main__":
    main()
