#!/usr/bin/env python3
"""
Batch PDF Generator for PetScreening Value Reports

Auto-detects PMC system (Yardi/Entrata) for each ID.

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
from datetime import datetime
from pathlib import Path

# Add the app directory to path so we can import from app.py
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd
import numpy as np
from collections import defaultdict


def detect_pmc_for_id(conn, id_val, id_type="property"):
    """
    Auto-detect which PMC system has this ID.
    Returns: (pmc_system, prop_count, label) or (None, 0, None) if not found.
    For parent IDs, picks the PMC with more properties.
    """
    cur = conn.cursor()
    results = {}
    
    if id_type == "property":
        # Check Yardi
        cur.execute("""
            SELECT PROPERTY_NAME, 1 as cnt
            FROM PROD.COMMON.D_PROPERTIES
            WHERE PROPERTY_ID = %s
        """, (id_val,))
        row = cur.fetchone()
        if row:
            results["yardi"] = (1, row[0])
        
        # Check Entrata
        cur.execute("""
            SELECT PROPERTY_NAME, 1 as cnt
            FROM PROD.ENTRATA.D_PROPERTIES
            WHERE PROPERTY_ID = %s
        """, (id_val,))
        row = cur.fetchone()
        if row:
            results["entrata"] = (1, row[0])
    
    else:  # parent
        # Check Yardi
        cur.execute("""
            SELECT PARENT_COMPANY_NAME, COUNT(DISTINCT PROPERTY_ID) as cnt
            FROM PROD.YARDI.YARDI_PROPERTIES
            WHERE PARENT_COMPANY_ID = %s
            GROUP BY PARENT_COMPANY_NAME
        """, (id_val,))
        row = cur.fetchone()
        if row:
            results["yardi"] = (row[1], row[0])
        
        # Check Entrata
        cur.execute("""
            SELECT PARENT_COMPANY_NAME, COUNT(DISTINCT PROPERTY_ID) as cnt
            FROM PROD.ENTRATA.D_PROPERTIES
            WHERE PARENT_COMPANY_ID = %s
            GROUP BY PARENT_COMPANY_NAME
        """, (id_val,))
        row = cur.fetchone()
        if row:
            results["entrata"] = (row[1], row[0])
    
    cur.close()
    
    if not results:
        return None, 0, None
    
    # Pick the one with more properties (or just the one found)
    best_pmc = max(results.keys(), key=lambda k: results[k][0])
    return best_pmc, results[best_pmc][0], results[best_pmc][1]


def process_single_id(id_val, id_type, pmc_system, label, args, output_path, app_imports):
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
        else:  # yardi
            if id_type == "parent":
                props_df = load_properties_for_selection(parent_company_name=label)
            else:
                props_df = load_properties_for_selection(property_id=id_val)
        
        if props_df is None or props_df.empty:
            result["status"] = "failed"
            result["error"] = "No properties found"
            return result
        
        property_ids = props_df["PROPERTY_ID"].tolist()
        result["properties_found"] = len(property_ids)
        
        # Fetch charges
        conn = get_snowflake_connection()
        cur = conn.cursor()
        
        pids_str = ",".join(str(p) for p in property_ids)
        
        # Get launch dates
        cur.execute(f"""
            SELECT PROPERTY_ID, PROPERTY_NAME, PS_LIVE_DATE
            FROM PROD.COMMON.D_PROPERTIES
            WHERE PROPERTY_ID IN ({pids_str})
        """)
        launch_rows = cur.fetchall()
        launch_dates = {}
        prop_names = {}
        for r in launch_rows:
            pid, pname, ldate = r
            prop_names[pid] = pname
            if ldate:
                launch_dates[pname] = datetime(ldate.year, ldate.month, 1)
        
        # Get charges
        if pmc_system == "entrata":
            charge_query = f"""
                SELECT 
                    p.PROPERTY_ID,
                    p.PROPERTY_NAME,
                    c.CHARGE_CODE,
                    c.AMOUNT,
                    c.FROM_DATE,
                    c.TO_DATE,
                    c.UNIT_CODE
                FROM PROD.ENTRATA.F_LEASE_CHARGES c
                JOIN PROD.ENTRATA.D_PROPERTIES p ON c.PROPERTY_ID = p.PROPERTY_ID
                WHERE c.PROPERTY_ID IN ({pids_str})
                  AND c.AMOUNT > 0
                  AND c.FROM_DATE IS NOT NULL
                  AND LOWER(c.CHARGE_CODE) LIKE '%pet%'
                ORDER BY p.PROPERTY_NAME, c.FROM_DATE
            """
        else:
            charge_query = f"""
                SELECT 
                    p.PROPERTY_ID,
                    p.PROPERTY_NAME,
                    c.CHARGE_CODE,
                    c.AMOUNT,
                    c.FROM_DATE,
                    c.TO_DATE,
                    c.UNIT_CODE
                FROM PROD.YARDI.F_LEASE_CHARGES c
                JOIN PROD.COMMON.D_PROPERTIES p ON c.PROPERTY_ID = p.PROPERTY_ID
                WHERE c.PROPERTY_ID IN ({pids_str})
                  AND c.AMOUNT > 0
                  AND c.FROM_DATE IS NOT NULL
                  AND LOWER(c.CHARGE_CODE) LIKE '%pet%'
                ORDER BY p.PROPERTY_NAME, c.FROM_DATE
            """
        
        cur.execute(charge_query)
        charge_rows = cur.fetchall()
        cur.close()
        
        if not charge_rows:
            result["status"] = "failed"
            result["error"] = "No pet charges found"
            return result
        
        # Build monthly_by_prop
        today = datetime.now()
        window_end = datetime(today.year, today.month, 1)
        window_start = datetime(today.year - 5, today.month, 1)
        months = [m.to_pydatetime() for m in pd.date_range(start=window_start, end=window_end, freq='MS')]
        
        monthly_by_prop = defaultdict(lambda: defaultdict(float))
        parsed_charges = []
        
        for row in charge_rows:
            pid, pname, cc, amt, from_dt, to_dt, unit = row
            if from_dt is None or amt <= 0:
                continue
            
            cs = datetime(from_dt.year, from_dt.month, 1)
            ce = datetime(to_dt.year, to_dt.month, 1) if to_dt else window_end
            
            for month in months:
                if cs <= month <= ce:
                    monthly_by_prop[pname][month] += amt
            
            parsed_charges.append({
                "property_id": pid,
                "property_name": pname,
                "charge_code": cc,
                "amount": amt,
                "from_date": from_dt,
                "to_date": to_dt,
                "unit_code": unit,
            })
        
        monthly_by_prop = {p: dict(v) for p, v in monthly_by_prop.items()}
        result["properties_with_data"] = len(monthly_by_prop)
        
        if not monthly_by_prop:
            result["status"] = "failed"
            result["error"] = "No monthly revenue data"
            return result
        
        # Compute launch analysis
        launch_analysis = compute_launch_analysis(monthly_by_prop, months, launch_dates)
        
        comparable = {p: a for p, a in launch_analysis.items()
                      if a["n_pre"] > 0 and a.get("baseline_reliable", True) and a.get("baseline_meaningful", True)}
        
        # Compute metrics
        pre_baseline = sum(a["pre_avg"] for a in comparable.values()) if comparable else 0
        t1_mo = sum(a["diff_monthly"] for a in comparable.values()) if comparable else 0
        t1_total = sum(a["diff_total"] for a in comparable.values()) if comparable else 0
        t1_months = max(a["n_post"] for a in comparable.values()) if comparable else 0
        t1_pct = (t1_mo / pre_baseline * 100) if pre_baseline > 0 else 0
        
        latest_month = months[-1]
        current_monthly_rev = sum(monthly_by_prop[p].get(latest_month, 0) for p in monthly_by_prop)
        
        result["comparable_count"] = len(comparable)
        result["monthly_lift"] = round(t1_mo, 2)
        
        # Fetch compliance data
        pid_lookup = _build_property_id_lookup(parsed_charges)
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
        df = pd.DataFrame(parsed_charges)
        selected_codes = df["charge_code"].unique().tolist() if "charge_code" in df.columns else []
        
        missing_rent_data = fetch_missing_pet_rent_by_property(
            df, selected_codes, property_ids, launch_dates, months
        ) if selected_codes else {}
        
        t2_tenants = sum(v["missing_count"] for v in missing_rent_data.values()) if missing_rent_data else 0
        t2_mo = sum(v["monthly_missing"].get(latest_month, 0) for v in missing_rent_data.values()) if missing_rent_data else 0
        t2_props = sum(1 for v in missing_rent_data.values() if v["missing_count"] > 0) if missing_rent_data else 0
        
        result["uncollected_tenants"] = t2_tenants
        
        # Fetch suspected undisclosed
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
        
        # Get total units
        total_units = 0
        property_doors = {}
        try:
            conn2 = get_snowflake_connection()
            cur2 = conn2.cursor()
            cur2.execute(f"""
                SELECT PROPERTY_ID, PROPERTY_NUMBER_OF_DOORS
                FROM PROD.COMMON.D_PROPERTIES
                WHERE PROPERTY_ID IN ({pids_str})
                  AND PROPERTY_NUMBER_OF_DOORS IS NOT NULL
            """)
            for r in cur2.fetchall():
                property_doors[r[0]] = r[1]
            total_units = sum(property_doors.values())
            cur2.close()
        except:
            pass
        
        property_doors_by_name = {}
        for pname, pid in pid_lookup.items():
            if pid in property_doors:
                property_doors_by_name[pname] = property_doors[pid]
        
        # Generate PDF
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
            use_avg_lift=args.avg_lift,
            asset_class="conventional",
            monthly_by_prop=monthly_by_prop,
            latest_month=latest_month,
            selected_charge_codes=selected_codes,
        )
        
        # Save PDF
        safe_label = label.replace(" ", "_").replace("/", "-").replace("\\", "-")[:50]
        filename = f"{safe_label}_{id_val}_{datetime.now().strftime('%Y%m%d')}.pdf"
        filepath = output_path / filename
        
        with open(filepath, "wb") as f:
            f.write(pdf_bytes)
        
        result["status"] = "success"
        result["pdf_filename"] = filename
        
    except Exception as e:
        result["status"] = "failed"
        result["error"] = str(e)
    
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
    print(f"Output folder: {args.output}")
    print("PMC detection: Auto")
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
    }
    
    # Get a connection for PMC detection
    conn = get_snowflake_connection()
    
    results = []
    success_count = 0
    error_count = 0
    
    for idx, id_val in enumerate(ids, 1):
        print(f"\n[{idx}/{len(ids)}] Processing {args.type} ID: {id_val}")
        
        # Auto-detect PMC
        pmc_system, prop_count, label = detect_pmc_for_id(conn, id_val, args.type)
        
        if pmc_system is None:
            print(f"  ✗ Not found in Yardi or Entrata")
            results.append({
                "id": id_val,
                "type": args.type,
                "pmc": "not_found",
                "label": "",
                "status": "failed",
                "properties_found": 0,
                "properties_with_data": 0,
                "comparable_count": 0,
                "monthly_lift": 0,
                "uncollected_tenants": 0,
                "suspected_undisclosed": 0,
                "pdf_filename": "",
                "error": "ID not found in any PMC system",
            })
            error_count += 1
            continue
        
        print(f"  Found in {pmc_system.upper()}: {label} ({prop_count} properties)")
        
        # Process the ID
        result = process_single_id(id_val, args.type, pmc_system, label, args, output_path, app_imports)
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
            "id", "type", "pmc", "label", "status",
            "properties_found", "properties_with_data", "comparable_count",
            "monthly_lift", "uncollected_tenants", "suspected_undisclosed",
            "pdf_filename", "error"
        ]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)
    
    print("\n" + "=" * 60)
    print(f"Complete: {success_count} succeeded, {error_count} failed")
    print(f"PDFs saved to: {output_path.absolute()}")
    print(f"Log saved to: {csv_path.absolute()}")


if __name__ == "__main__":
    main()
