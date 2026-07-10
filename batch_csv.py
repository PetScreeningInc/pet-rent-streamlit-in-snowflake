#!/usr/bin/env python3
"""
Batch CSV exporter for PetScreening pet-rent opportunity reports.

Mirrors the Streamlit app's downloadable CSV outputs for:
  - Missing Pet Rent
  - Suspected Undisclosed Pets

It uses the same live-charge fetch path as batch_pdf.py, then calls the same
app report builders used by the UI downloads.

Examples:
    python batch_csv.py --ids 123,456 --type property --report missing
    python batch_csv.py --ids 61610 --type parent --report both --workers 8
    python batch_csv.py --file ids.txt --type parent --report suspected --output ./batch_csvs
    python batch_csv.py --ids 123 --charge-codes PETRENT,PETFEE --pmc appfolio
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
import warnings
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

# Suppress Streamlit warnings when importing app.py in script mode.
os.environ["STREAMLIT_SERVER_HEADLESS"] = "true"
warnings.filterwarnings("ignore", message=".*missing ScriptRunContext.*")
warnings.filterwarnings("ignore", message=".*Session state does not function.*")
warnings.filterwarnings("ignore", category=UserWarning)
logging.getLogger("streamlit").setLevel(logging.ERROR)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


PET_KEYWORDS = ["pet", "animal", "petnr", "concpet"]
EXCLUDED_KEYWORDS = ["carpet", "puppet", "trumpet"]


def parse_ids(args: argparse.Namespace) -> list[str]:
    if args.ids:
        return [x.strip() for x in args.ids.split(",") if x.strip()]
    if args.file:
        with open(args.file, "r", encoding="utf-8") as f:
            return [line.strip() for line in f if line.strip() and not line.lstrip().startswith("#")]
    raise SystemExit("Error: Provide --ids or --file")


def sanitize_filename(value: Any, max_len: int = 80) -> str:
    text = str(value or "").strip() or "unknown"
    for ch in ["/", "\\", ":", "*", "?", '"', "<", ">", "|", ","]:
        text = text.replace(ch, "-")
    text = "_".join(text.split())
    return text[:max_len].strip("._-") or "unknown"


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
        found = set(df["charge_code"].dropna().astype(str).unique())
        missing = [c for c in selected if c not in found]
        if missing:
            print(f"    Warning: manual charge codes not present in fetched data: {', '.join(missing)}")
        return selected

    mask = df["charge_code"].apply(is_pet_charge_code)
    selected = sorted(df.loc[mask, "charge_code"].dropna().astype(str).unique().tolist())
    if not selected:
        raise ValueError("No pet-related charge codes found")
    return selected


def load_app_imports() -> dict[str, Any]:
    from app import (  # noqa: WPS433 - intentional lazy import after warning setup
        ENTRATA_API_KEY,
        SOAP_HEADERS,
        YARDI_LICENSE_TOKEN,
        _extract_ar_charges_from_entrata_lease,
        _extract_charges_from_entrata_lease,
        _fetch_entrata_leases_for_property,
        build_soap_payload,
        extract_charges_from_property,
        extract_property,
        fetch_entrata_for_properties,
        fetch_appfolio_for_properties,
        fetch_realpage_for_properties,
        fetch_rentroll_for_properties,
        generate_missing_pet_rent_report,
        generate_suspected_undisclosed_report,
        get_snowflake_connection,
        load_entrata_properties_for_selection,
        load_appfolio_properties_for_selection,
        load_properties_for_selection,
        load_realpage_properties_for_selection,
        parse_date,
        xml_to_dict,
    )

    return {
        "ENTRATA_API_KEY": ENTRATA_API_KEY,
        "SOAP_HEADERS": SOAP_HEADERS,
        "YARDI_LICENSE_TOKEN": YARDI_LICENSE_TOKEN,
        "_extract_ar_charges_from_entrata_lease": _extract_ar_charges_from_entrata_lease,
        "_extract_charges_from_entrata_lease": _extract_charges_from_entrata_lease,
        "_fetch_entrata_leases_for_property": _fetch_entrata_leases_for_property,
        "build_soap_payload": build_soap_payload,
        "extract_charges_from_property": extract_charges_from_property,
        "extract_property": extract_property,
        "fetch_entrata_for_properties": fetch_entrata_for_properties,
        "fetch_appfolio_for_properties": fetch_appfolio_for_properties,
        "fetch_realpage_for_properties": fetch_realpage_for_properties,
        "fetch_rentroll_for_properties": fetch_rentroll_for_properties,
        "generate_missing_pet_rent_report": generate_missing_pet_rent_report,
        "generate_suspected_undisclosed_report": generate_suspected_undisclosed_report,
        "get_snowflake_connection": get_snowflake_connection,
        "load_entrata_properties_for_selection": load_entrata_properties_for_selection,
        "load_appfolio_properties_for_selection": load_appfolio_properties_for_selection,
        "load_properties_for_selection": load_properties_for_selection,
        "load_realpage_properties_for_selection": load_realpage_properties_for_selection,
        "parse_date": parse_date,
        "xml_to_dict": xml_to_dict,
    }


def load_properties(
    *,
    id_val: str,
    id_type: str,
    pmc_system: str,
    label: str,
    app_imports: dict[str, Any],
) -> list[dict[str, Any]]:
    pd = __import__("pandas")

    if pmc_system == "all":
        # Combined mode: load from every system and tag each property with
        # its source so the fetcher can route it correctly.
        out: list[dict[str, Any]] = []
        for _sys in ("yardi", "entrata", "real_page", "appfolio"):
            try:
                rows = load_properties(
                    id_val=id_val, id_type=id_type, pmc_system=_sys,
                    label=label, app_imports=app_imports,
                )
            except Exception:
                rows = []
            for r in rows:
                tagged = dict(r)
                tagged["_PMC_SYSTEM"] = _sys
                out.append(tagged)
        return out

    if pmc_system == "entrata":
        loader = app_imports["load_entrata_properties_for_selection"]
        if id_type == "parent":
            props_df = loader(ancestry_id=str(id_val))
            if props_df is None or len(props_df) == 0:
                props_df = loader(parent_company_name=label)
        else:
            props_df = loader(property_id=id_val)
    elif pmc_system == "real_page":
        loader = app_imports["load_realpage_properties_for_selection"]
        if id_type == "parent":
            props_df = loader(ancestry_id=str(id_val))
            if props_df is None or len(props_df) == 0:
                props_df = loader(parent_company_name=label)
        else:
            props_df = loader(property_id=id_val)
    elif pmc_system == "appfolio":
        loader = app_imports["load_appfolio_properties_for_selection"]
        if id_type == "parent":
            props_df = loader(ancestry_id=str(id_val))
            if props_df is None or len(props_df) == 0:
                props_df = loader(parent_company_name=label)
        else:
            props_df = loader(property_id=id_val)
    else:
        loader = app_imports["load_properties_for_selection"]
        if id_type == "parent":
            props_df = loader(parent_company_name=label)
        else:
            props_df = loader(property_id=id_val)

    if props_df is None:
        return []
    if isinstance(props_df, pd.DataFrame):
        return props_df.to_dict("records")
    if isinstance(props_df, list):
        return props_df
    raise TypeError(f"Unexpected properties return type: {type(props_df)}")


def property_ids_from(properties: list[dict[str, Any]]) -> list[Any]:
    ids: list[Any] = []
    for prop in properties:
        pid = prop.get("PROPERTY_ID") or prop.get("property_id")
        if pid is not None and str(pid).strip():
            ids.append(pid)
    return ids


def save_report_csv(
    *,
    report_df: Any,
    report_type: str,
    output_dir: Path,
    pmc_system: str,
    label: str,
    id_val: str,
    timestamp: str,
) -> str:
    pmc_dir = output_dir / pmc_system.lower()
    pmc_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{report_type}_{sanitize_filename(label, 55)}_{sanitize_filename(id_val, 24)}_{timestamp}.csv"
    path = pmc_dir / filename
    report_df.to_csv(path, index=False)
    return str(path.relative_to(output_dir))


def existing_reports_for_id(output_dir: Path, id_val: str, report: str,
                            resume_days: float) -> list[str]:
    """Recent CSVs already produced for this ID (all requested types), or []."""
    cutoff = datetime.now() - timedelta(days=resume_days)
    needed = ["missing", "suspected"] if report == "both" else [report]
    found: list[str] = []
    id_part = sanitize_filename(id_val, 24)
    for rt in needed:
        hits = [
            p for p in output_dir.glob(f"*/{rt}_*_{id_part}_*.csv")
            if datetime.fromtimestamp(p.stat().st_mtime) >= cutoff
        ]
        if not hits:
            return []
        found.append(sorted(hits)[-1].name)
    return found


def process_single_id(
    *,
    id_val: str,
    args: argparse.Namespace,
    output_dir: Path,
    app_imports: dict[str, Any],
    conn: Any,
    timestamp: str,
) -> dict[str, Any]:
    import streamlit as st  # noqa: WPS433

    row: dict[str, Any] = {
        "id": id_val,
        "type": args.type,
        "pmc": "",
        "label": "",
        "status": "pending",
        "properties_found": 0,
        "charges_fetched": 0,
        "selected_charge_codes": "",
        "missing_rows": 0,
        "missing_csv": "",
        "suspected_rows": 0,
        "suspected_csv": "",
        "error": "",
        "processed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    try:
        from batch_pdf import detect_pmc_for_id, fetch_charges_for_properties

        pmc_system, prop_count, label = detect_pmc_for_id(conn, id_val, args.type)
        if args.pmc:
            pmc_system = args.pmc
        if args.label:
            label = args.label
        if pmc_system is None:
            raise ValueError("ID not found in PROD.COMMON.D_PROPERTIES")
        if pmc_system not in ("yardi", "entrata", "real_page", "appfolio", "all"):
            raise ValueError(f"Unsupported PMC: {pmc_system}")

        label_text = str(label or id_val)
        row.update({"pmc": pmc_system, "label": label_text, "properties_found": prop_count or 0})
        st.session_state["pmc_system"] = pmc_system

        # Yardi API is only served weekdays 9:00-18:00 local — defer instead
        # of failing property by property.
        from batch_pdf import yardi_window_open, id_needs_yardi
        if id_needs_yardi(pmc_system) and not getattr(args, "ignore_yardi_window", False) \
                and not yardi_window_open():
            row["status"] = "deferred"
            row["error"] = "outside Yardi API window (weekdays 9:00-18:00 local)"
            return row

        print(f"  Found: {label_text} ({pmc_system.upper()}, {prop_count} properties)")
        properties = load_properties(
            id_val=id_val,
            id_type=args.type,
            pmc_system=pmc_system,
            label=label_text,
            app_imports=app_imports,
        )
        if not properties:
            raise ValueError("No properties found in selection")

        # pid → source system map for combined-mode report builders
        st.session_state["property_system_by_pid"] = {
            str(p.get("PROPERTY_ID")): p.get("_PMC_SYSTEM", pmc_system)
            for p in properties
        }

        property_ids = property_ids_from(properties)
        row["properties_found"] = len(property_ids)
        if not property_ids:
            raise ValueError("No property IDs found in selection")

        print(f"    Fetching charges via {pmc_system.upper()}...")
        all_charges, fetch_log = fetch_charges_for_properties(
            properties,
            pmc_system,
            app_imports,
            verbose=args.verbose,
            workers=args.workers or 1,
            use_cache=not getattr(args, "no_cache", False),
            cache_ttl_days=getattr(args, "cache_days", 7.0),
        )
        del fetch_log  # retained only for parity with batch_pdf fetch helper

        if not all_charges:
            raise ValueError("No charges returned from API/Snowflake source")

        pd = __import__("pandas")
        all_charges_df = pd.DataFrame(all_charges)
        row["charges_fetched"] = len(all_charges_df)
        selected_codes = select_charge_codes(all_charges_df, args.charge_codes)
        row["selected_charge_codes"] = ",".join(selected_codes)
        print(f"    Using {len(selected_codes)} charge codes: {', '.join(selected_codes)}")

        wanted = {"missing", "suspected"} if args.report == "both" else {args.report}

        if "missing" in wanted:
            print("    Building Missing Pet Rent CSV...")
            missing_df = app_imports["generate_missing_pet_rent_report"](
                all_charges_df,
                selected_codes,
                property_ids,
            )
            row["missing_rows"] = 0 if missing_df is None else len(missing_df)
            if missing_df is not None and not missing_df.empty:
                row["missing_csv"] = save_report_csv(
                    report_df=missing_df,
                    report_type="missing_pet_rent",
                    output_dir=output_dir,
                    pmc_system=pmc_system,
                    label=label_text,
                    id_val=id_val,
                    timestamp=timestamp,
                )

        if "suspected" in wanted:
            print("    Building Suspected Undisclosed Pets CSV...")
            suspected_df = app_imports["generate_suspected_undisclosed_report"](
                all_charges_df,
                selected_codes,
                property_ids,
            )
            row["suspected_rows"] = 0 if suspected_df is None else len(suspected_df)
            if suspected_df is not None and not suspected_df.empty:
                row["suspected_csv"] = save_report_csv(
                    report_df=suspected_df,
                    report_type="suspected_undisclosed_pets",
                    output_dir=output_dir,
                    pmc_system=pmc_system,
                    label=label_text,
                    id_val=id_val,
                    timestamp=timestamp,
                )

        # ── Auto-append the person rows to RAW.MISC.PET_VALUE_USERS_SNAPSHOT ──
        # Same pattern as the fetch cache: batch exports land in Snowflake on
        # their own, so no more backfill runs for future pulls.
        try:
            import snapshot_tables as _snap_tbl
            _is_parent = str(getattr(args, "type", "")) == "parent"
            _n_snap = _snap_tbl.append_users_snapshot(
                missing_df=locals().get("missing_df"),
                suspected_df=locals().get("suspected_df"),
                pmc_system=pmc_system,
                run_label=f"{label_text} {id_val} {timestamp}",
                parent_id=id_val if _is_parent else "",
                parent_name=label_text if _is_parent else "",
            )
            if _n_snap:
                print(f"    Users snapshot: +{_n_snap} rows appended to Snowflake")
        except Exception as _snap_exc:  # noqa: BLE001 — never fail the export
            print(f"    Users snapshot not appended: {str(_snap_exc)[:120]}")

        row["status"] = "success"
        if not row["missing_csv"] and not row["suspected_csv"]:
            row["status"] = "success_empty"
        return row

    except Exception as exc:  # noqa: BLE001 - batch script should log per-ID failures
        if args.verbose:
            import traceback

            traceback.print_exc()
        row["status"] = "failed"
        row["error"] = str(exc)
        return row


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Batch export Missing Pet Rent / Suspected Undisclosed Pets CSVs",
    )
    parser.add_argument("--ids", type=str, help="Comma-separated IDs")
    parser.add_argument("--file", type=str, help="File with one ID per line")
    parser.add_argument(
        "--type",
        choices=["property", "parent"],
        default="property",
        help="ID type: property (default) or parent (parent company ancestry ID)",
    )
    parser.add_argument(
        "--report",
        choices=["missing", "suspected", "both"],
        default="both",
        help="Which CSV report to export (default: both)",
    )
    parser.add_argument("--output", default="./batch_csvs", help="Output folder")
    parser.add_argument(
        "--charge-codes",
        default=None,
        help="Comma-separated pet charge codes to use instead of keyword auto-detection",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Concurrent property fetches per parent, same as batch_pdf.py (default: 1)",
    )
    parser.add_argument(
        "--pmc",
        choices=["yardi", "entrata", "real_page", "appfolio", "all"],
        default=None,
        help="Force PMC system; overrides auto-detection",
    )
    parser.add_argument(
        "--label",
        default=None,
        help="Override label for output filenames when grouping arbitrary IDs",
    )
    parser.add_argument("--verbose", action="store_true", help="Show detailed traces and fetch output")
    parser.add_argument("--no-cache", action="store_true",
                        help="Ignore the per-property fetch cache and re-hit the APIs")
    parser.add_argument("--cache-days", type=float, default=7.0,
                        help="Max age in days for cached per-property fetches (default 7)")
    parser.add_argument("--resume", action="store_true",
                        help="Skip IDs whose requested CSV(s) already exist in the output "
                             "folder from the last N days (see --resume-days)")
    parser.add_argument("--resume-days", type=float, default=4.0,
                        help="Look-back window for --resume (default 4 days)")
    parser.add_argument("--retries", type=int, default=2,
                        help="Retries per ID on transient errors (timeouts, 5xx, SOAP faults; default 2)")
    parser.add_argument("--retry-wait", type=float, default=30.0,
                        help="Base seconds between retries, doubled each attempt (default 30)")
    parser.add_argument("--ignore-yardi-window", action="store_true",
                        help="Attempt Yardi fetches even outside weekdays 9:00-18:00")

    args = parser.parse_args()
    ids = parse_ids(args)
    if not ids:
        raise SystemExit("Error: No IDs provided")

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = output_dir / f"batch_csv_log_{timestamp}.csv"

    print(f"Processing {len(ids)} {args.type} ID(s)")
    print(f"Report: {args.report}")
    print(f"Output folder: {output_dir}")
    if args.workers and args.workers > 1:
        print(f"Concurrent fetches per parent: {args.workers}")
    if args.pmc:
        print(f"PMC forced: {args.pmc}")
    print("-" * 60)

    app_imports = load_app_imports()
    conn = app_imports["get_snowflake_connection"]()

    fieldnames = [
        "id",
        "type",
        "pmc",
        "label",
        "status",
        "properties_found",
        "charges_fetched",
        "selected_charge_codes",
        "missing_rows",
        "missing_csv",
        "suspected_rows",
        "suspected_csv",
        "error",
        "processed_at",
    ]
    with open(log_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()

        from batch_pdf import is_transient_error

        success = failed = empty = skipped = deferred = 0
        for idx, id_val in enumerate(ids, 1):
            print(f"\n[{idx}/{len(ids)}] Processing {args.type} ID: {id_val}")

            if args.resume:
                existing = existing_reports_for_id(output_dir, id_val, args.report, args.resume_days)
                if existing:
                    print(f"  ⟩ SKIPPED (resume: {', '.join(existing)})")
                    writer.writerow({
                        "id": id_val, "type": args.type, "status": "skipped",
                        "error": "", "missing_csv": "", "suspected_csv": "",
                        "processed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    })
                    f.flush()
                    skipped += 1
                    continue

            _max_attempts = 1 + max(0, getattr(args, "retries", 2))
            for _attempt in range(1, _max_attempts + 1):
                result = process_single_id(
                    id_val=id_val,
                    args=args,
                    output_dir=output_dir,
                    app_imports=app_imports,
                    conn=conn,
                    timestamp=timestamp,
                )
                if result["status"] != "failed" or not is_transient_error(result.get("error")):
                    break
                if _attempt < _max_attempts:
                    _backoff = getattr(args, "retry_wait", 30.0) * (2 ** (_attempt - 1))
                    print(f"  ↻ Transient error (attempt {_attempt}/{_max_attempts}): "
                          f"{str(result['error'])[:120]} — retrying in {_backoff:.0f}s")
                    import time as _time
                    _time.sleep(_backoff)
            writer.writerow(result)
            f.flush()

            if result["status"] == "deferred":
                deferred += 1
                print(f"  ⏸ DEFERRED — {result['error']} (re-run with --resume during the window)")
            elif result["status"] == "failed":
                failed += 1
                print(f"  ✗ {result['error']}")
            elif result["status"] == "success_empty":
                empty += 1
                print("  ✓ Completed, but no rows matched the selected report(s)")
            else:
                success += 1
                outputs = [p for p in [result.get("missing_csv"), result.get("suspected_csv")] if p]
                print(f"  ✓ Saved: {', '.join(outputs)}")
                print(
                    f"    Missing rows: {result['missing_rows']:,} | "
                    f"Suspected rows: {result['suspected_rows']:,}"
                )

    print("\n" + "=" * 60)
    summary = f"Complete: {success} succeeded, {empty} empty, {failed} failed"
    if skipped:
        summary += f", {skipped} skipped (resume)"
    if deferred:
        summary += f", {deferred} deferred (Yardi window — re-run weekdays 9:00-18:00)"
    print(summary)
    print(f"CSVs saved under: {output_dir.absolute()}")
    print(f"Log saved to: {log_path.absolute()}")


if __name__ == "__main__":
    main()
