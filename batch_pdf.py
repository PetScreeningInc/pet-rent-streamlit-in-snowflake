#!/usr/bin/env python3
"""
Batch PDF Generator for PetScreening Value Reports

Usage:
    python batch_pdf.py --ids 123,456,789 --type property
    python batch_pdf.py --ids 123,456 --type parent --adoption resident
    python batch_pdf.py --file ids.txt --type property --output ./reports

Options:
    --ids           Comma-separated list of IDs
    --file          Path to file with one ID per line
    --type          'property' or 'parent' (parent_company)
    --adoption      'unit' (default) or 'resident'
    --output        Output folder (default: ./batch_pdfs)
    --avg-lift      Use average monthly lift methodology (for seasonal portfolios)
    --include-pm    Include property manager appendix
"""

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

# Add the app directory to path so we can import from app.py
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd
import numpy as np
from collections import defaultdict


def main():
    parser = argparse.ArgumentParser(description="Batch generate PetScreening Value Report PDFs")
    parser.add_argument("--ids", type=str, help="Comma-separated IDs")
    parser.add_argument("--file", type=str, help="File with one ID per line")
    parser.add_argument("--type", type=str, choices=["property", "parent"], default="parent",
                        help="ID type: 'property' or 'parent' (parent_company)")
    parser.add_argument("--adoption", type=str, choices=["unit", "resident"], default="unit",
                        help="Adoption metric: 'unit' or 'resident'")
    parser.add_argument("--output", type=str, default="./batch_pdfs", help="Output folder")
    parser.add_argument("--avg-lift", action="store_true", help="Use average monthly lift methodology")
    parser.add_argument("--include-pm", action="store_true", help="Include property manager appendix")
    parser.add_argument("--pmc", type=str, choices=["yardi", "entrata", "realpage"], default="yardi",
                        help="PMC system")
    
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
    print("-" * 50)
    
    # Create output folder
    output_path = Path(args.output)
    output_path.mkdir(parents=True, exist_ok=True)
    
    # Import app functions (after parsing args so --help works without DB)
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
    
    success_count = 0
    error_count = 0
    
    for idx, id_val in enumerate(ids, 1):
        try:
            print(f"\n[{idx}/{len(ids)}] Processing {args.type} ID: {id_val}")
            
            # Load properties based on type
            if args.pmc == "entrata":
                if args.type == "parent":
                    # Need to get parent company name first
                    conn = get_snowflake_connection()
                    cur = conn.cursor()
                    cur.execute("""
                        SELECT DISTINCT PARENT_COMPANY_NAME 
                        FROM PROD.ENTRATA.D_PROPERTIES 
                        WHERE PARENT_COMPANY_ID = %s
                    """, (id_val,))
                    row = cur.fetchone()
                    cur.close()
                    if not row:
                        print(f"  ✗ Parent company {id_val} not found")
                        error_count += 1
                        continue
                    parent_name = row[0]
                    props_df = load_entrata_properties_for_selection(parent_company_name=parent_name)
                    label = parent_name
                else:
                    props_df = load_entrata_properties_for_selection(property_id=id_val)
                    label = f"Property {id_val}"
            else:  # yardi
                if args.type == "parent":
                    conn = get_snowflake_connection()
                    cur = conn.cursor()
                    cur.execute("""
                        SELECT DISTINCT PARENT_COMPANY_NAME 
                        FROM PROD.YARDI.YARDI_PROPERTIES 
                        WHERE PARENT_COMPANY_ID = %s
                    """, (id_val,))
                    row = cur.fetchone()
                    cur.close()
                    if not row:
                        print(f"  ✗ Parent company {id_val} not found")
                        error_count += 1
                        continue
                    parent_name = row[0]
                    props_df = load_properties_for_selection(parent_company_name=parent_name)
                    label = parent_name
                else:
                    props_df = load_properties_for_selection(property_id=id_val)
                    label = f"Property {id_val}"
            
            if props_df is None or props_df.empty:
                print(f"  ✗ No properties found for {args.type} {id_val}")
                error_count += 1
                continue
            
            property_ids = props_df["PROPERTY_ID"].tolist()
            print(f"  Found {len(property_ids)} properties")
            
            # Fetch charges (simplified — using existing app logic)
            # This is the heavy lift — we need to replicate the charge fetching
            print("  Fetching charge data...")
            
            conn = get_snowflake_connection()
            cur = conn.cursor()
            
            # Get launch dates
            pids_str = ",".join(str(p) for p in property_ids)
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
            
            # Get charges — simplified query for pet-related charges
            # This queries the same data the app uses
            cur.execute(f"""
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
            """)
            charge_rows = cur.fetchall()
            cur.close()
            
            if not charge_rows:
                print(f"  ✗ No pet charges found")
                error_count += 1
                continue
            
            print(f"  Found {len(charge_rows):,} charge records")
            
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
            
            if not monthly_by_prop:
                print(f"  ✗ No monthly revenue data")
                error_count += 1
                continue
            
            # Compute launch analysis
            print("  Computing launch analysis...")
            launch_analysis = compute_launch_analysis(monthly_by_prop, months, launch_dates)
            
            # Get comparable properties
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
            
            # Fetch compliance data
            print("  Fetching compliance data...")
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
            print("  Fetching missing pet rent data...")
            df = pd.DataFrame(parsed_charges)
            selected_codes = df["charge_code"].unique().tolist() if "charge_code" in df.columns else []
            
            missing_rent_data = fetch_missing_pet_rent_by_property(
                df, selected_codes, property_ids, launch_dates, months
            ) if selected_codes else {}
            
            t2_tenants = sum(v["missing_count"] for v in missing_rent_data.values()) if missing_rent_data else 0
            t2_mo = sum(v["monthly_missing"].get(latest_month, 0) for v in missing_rent_data.values()) if missing_rent_data else 0
            t2_props = sum(1 for v in missing_rent_data.values() if v["missing_count"] > 0) if missing_rent_data else 0
            
            # Fetch suspected undisclosed
            print("  Fetching suspected undisclosed data...")
            suspected_data = fetch_suspected_undisclosed_by_property(
                df, selected_codes, property_ids, launch_dates, months
            ) if selected_codes else {}
            
            su_total_profiles = sum(v["missing_count"] for v in suspected_data.values()) if suspected_data else 0
            su_current_mo = sum(v["monthly_missing"].get(latest_month, 0) for v in suspected_data.values()) if suspected_data else 0
            
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
            
            # Build property_doors by name
            property_doors_by_name = {}
            for pname, pid in pid_lookup.items():
                if pid in property_doors:
                    property_doors_by_name[pname] = property_doors[pid]
            
            # Generate PDF
            print("  Generating PDF...")
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
                pmc_system=args.pmc,
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
            
            print(f"  ✓ Saved: {filename}")
            print(f"    {len(comparable)} comparable props, ${t1_mo:,.0f}/mo lift, {t2_tenants} uncollected, {su_total_profiles} suspected")
            success_count += 1
            
        except Exception as e:
            print(f"  ✗ Error: {e}")
            import traceback
            traceback.print_exc()
            error_count += 1
    
    print("\n" + "=" * 50)
    print(f"Complete: {success_count} succeeded, {error_count} failed")
    print(f"PDFs saved to: {output_path.absolute()}")


if __name__ == "__main__":
    main()
