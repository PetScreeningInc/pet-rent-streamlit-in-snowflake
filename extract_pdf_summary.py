#!/usr/bin/env python3
"""
extract_pdf_summary.py — Scan all PDFs in the output folder, extract the
key metrics from pages 1-2 (At a Glance + Revenue Opportunity), and write
a single clean CSV summarizing every parent/property report.

Handles duplicates: when the same parent ID appears in multiple PDFs
(different run dates), keeps the MOST RECENT one only.

Output columns:
    parent_id, parent_name, pmc, run_date, pdf_filename,
    comparable_count, comparable_units,
    pre_revenue, current_revenue, post_avg_revenue,
    monthly_lift, lift_per_unit, asset_value_impact,
    uncollected_tenants, missing_monthly_rent,
    suspected_undisclosed, suspected_monthly,
    combined_opportunity_monthly, annual_revenue_impact,
    total_properties, properties_with_data

Usage:
    python extract_pdf_summary.py batch_pdfs/             # scan folder
    python extract_pdf_summary.py batch_pdfs/ -o report.csv
    python extract_pdf_summary.py batch_pdfs/ --dry-run   # preview, no write
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from pypdf import PdfReader
except ImportError:
    try:
        from PyPDF2 import PdfReader
    except ImportError:
        print("❌ Install pypdf: pip install pypdf")
        sys.exit(1)


# ─── PDF Text ───────────────────────────────────────────────────────
def read_pdf_text(pdf_path: Path, max_pages: int = 4) -> str:
    try:
        reader = PdfReader(str(pdf_path))
        parts = []
        for page in reader.pages[:max_pages]:
            parts.append(page.extract_text() or "")
        return "\n".join(parts)
    except Exception as e:
        print(f"  ⚠ Could not read {pdf_path.name}: {e}")
        return ""


# ─── Number helpers ─────────────────────────────────────────────────
def _parse_money(s: Optional[str]) -> Optional[float]:
    if not s:
        return None
    s = s.replace(",", "").replace("$", "").replace("+", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def _parse_int(s: Optional[str]) -> Optional[int]:
    if not s:
        return None
    s = s.replace(",", "").strip()
    try:
        return int(float(s))
    except ValueError:
        return None


def _first_match(text: str, patterns: List[str], group: int = 1, flags=re.IGNORECASE) -> Optional[str]:
    """Try each regex in order, return first match's capture group."""
    for pat in patterns:
        m = re.search(pat, text, flags)
        if m:
            return m.group(group)
    return None


# ─── Filename parser ────────────────────────────────────────────────
def parse_filename(name: str) -> Dict[str, str]:
    """
    Filename format from batch_pdf.py:
        PropertyName_PropID_ParentName_ParentID_YYYYMMDD.pdf

    Tokens between the trailing date and the start are name/id mixed.
    The two numeric tokens (excluding the date) are PropID and ParentID,
    in left-to-right order in the filename.
    """
    stem = Path(name).stem
    parts = stem.split("_")
    info: Dict[str, str] = {"raw_stem": stem}

    if not parts:
        return info

    # Date = trailing 8-digit token, if present
    if re.match(r"^\d{8}$", parts[-1]):
        info["run_date"] = parts[-1]
        body = parts[:-1]
    else:
        body = parts

    # Numeric tokens in the body (excluding date) are property_id then parent_id.
    # Filter min length=2 to avoid garbage like '0' but allow legitimate short ids.
    nums = [p for p in body if p.isdigit() and len(p) >= 2]

    if len(nums) >= 2:
        info["prop_id"]   = nums[0]   # first numeric = property id
        info["parent_id"] = nums[-1]  # last numeric in body = parent id
    elif len(nums) == 1:
        info["parent_id"] = nums[0]
        info["prop_id"]   = nums[0]

    return info


# ─── Extractors ─────────────────────────────────────────────────────
def extract_all(text: str) -> Dict[str, Any]:
    d: Dict[str, Any] = {}

    # ── Label (PDF header) ──
    # The label is the first bold line in the header. pypdf may render it
    # as the first line of text. Try to grab it.
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    if lines:
        d["label"] = lines[0]  # Best guess — may need manual cleanup

    # ── Comparable count + units ──
    m = re.search(
        r"(?:Across|across)\s+(\d{1,5})\s+comparable\s+propert\w+\s*\((\d[\d,]*)\s*units?\)",
        text, re.IGNORECASE,
    )
    if m:
        d["comparable_count"] = _parse_int(m.group(1))
        d["comparable_units"] = _parse_int(m.group(2))
    else:
        d["comparable_count"] = _parse_int(
            _first_match(text, [
                r"(?:Across|across)\s+(\d{1,5})\s+comparable\s+propert",
                r"(\d{1,5})\s+properties?\s+with\s+pre\s*(?:&|and)\s*post",
            ])
        )
        d["comparable_units"] = _parse_int(
            _first_match(text, [
                r"Comparable\s+Units[\s\n]*(\d[\d,]*)",
                r"Live\s+Units[\s\n]*(\d[\d,]*)",
            ])
        )

    # ── Pre-PS Revenue ──
    d["pre_revenue"] = _parse_money(
        _first_match(text, [
            r"pet\s+revenue\s+before\s+PetScreening\s+was\s+\$([\d,]+)\s*/\s*mo",
            r"pet\s+revenue\s+was\s+\$([\d,]+)\s*/\s*mo\s+before\s+PetScreening",
            r"Pre-PS\s+Revenue[\s\n]*\$([\d,]+)\s*/?\s*mo",
        ])
    )

    # ── Current / Post-avg Revenue ──
    d["post_avg_revenue"] = _parse_money(
        _first_match(text, [
            r"those\s+properties\s+average\s+\$([\d,]+)\s*/\s*mo",
            r"Post-launch\s+average\s+is\s+\$([\d,]+)\s*/\s*mo",
            r"Post-PS\s+Avg\s+Revenue[\s\n]*\$([\d,]+)",
        ])
    )
    d["current_revenue"] = _parse_money(
        _first_match(text, [
            r"(?:generate|now)\s+\$([\d,]+)\s*/\s*mo",
            r"generates?\s+\$([\d,]+)\s*/\s*mo\s+in\s+pet\s+fee",
            r"Current\s+Revenue[\s\n]*\$([\d,]+)",
        ])
    )

    # ── Monthly Lift ──
    lift_raw = _first_match(text, [
        r"\$(-?[\d,]+)\s*/\s*mo\s+average\s+monthly\s+lift",
        r"\$(-?[\d,]+)\s*/\s*mo\s+increase",
        r"\$(-?[\d,]+)\s*/\s*mo\s+decrease",
        r"(?:Average\s+)?Monthly\s+Lift[\s\n]*\+?\$(-?[\d,]+)",
    ])
    d["monthly_lift"] = _parse_money(lift_raw)
    # Handle "decrease" as negative
    if d["monthly_lift"] and d["monthly_lift"] > 0:
        m = re.search(r"\$[\d,]+\s*/\s*mo\s+decrease", text, re.IGNORECASE)
        if m:
            d["monthly_lift"] = -d["monthly_lift"]

    # ── Lift per unit ──
    d["lift_per_unit"] = _parse_money(
        _first_match(text, [
            r"\$(-?[\d,.]+)\s*/\s*unit\s*/\s*mo",
            r"\$(-?[\d,.]+)\s+per\s+unit\s+per\s+month",
            r"Lift\s+per\s+Unit[\s\n]*\+?\$(-?[\d,.]+)",
        ])
    )

    # ── Asset value impact ──
    avi = _first_match(text, [
        r"Est\.?\s*Asset\s+Value\s+Impact[\s\n]*~?\$?([\d,.]+[MKB]?)",
        r"~?\$([\d,.]+[MKB]?)\s+in\s+added\s+asset\s+value",
        r"Unrealized\s+Property\s+Value:\s*\$([\d,.]+[MKB]?)",
    ])
    if avi:
        # Handle M/K suffixes
        avi_clean = avi.replace(",", "")
        if avi_clean.endswith("M"):
            d["asset_value_impact"] = float(avi_clean[:-1]) * 1_000_000
        elif avi_clean.endswith("K"):
            d["asset_value_impact"] = float(avi_clean[:-1]) * 1_000
        else:
            d["asset_value_impact"] = _parse_money(avi)
    else:
        d["asset_value_impact"] = None

    # ── Uncollected tenants + missing monthly rent ──
    d["uncollected_tenants"] = _parse_int(
        _first_match(text, [
            r"(\d[\d,]*)\s+tenants?\s+have\s+completed\s+PetScreening\s+profiles\s+but\s+are\s+not",
            r"Tenants?\s+Not\s+Paying[\s\S]{0,60}?(\d[\d,]*)",
        ])
    )
    d["missing_monthly_rent"] = _parse_money(
        _first_match(text, [
            r"Missing\s+Monthly\s+Pet\s+Rent[\s\n]*\$([\d,]+)",
            r"\$([\d,]+)\s*/\s*mo\s+(?:in\s+)?uncollected",
        ])
    )

    # ── Suspected undisclosed ──
    d["suspected_undisclosed"] = _parse_int(
        _first_match(text, [
            r"(\d[\d,]*)\s+tenants?\s+show\s+signals?\s+of\s+undisclosed",
            r"(\d[\d,]*)\s+suspected\s+undisclosed",
        ])
    )
    d["suspected_monthly"] = _parse_money(
        _first_match(text, [
            r"signals?\s+of\s+undisclosed\s+pets[^$]*\$([\d,]+)\s*/\s*mo",
            r"estimated\s+\$([\d,]+)\s*/\s*mo\s+in\s+potential\s+additional",
        ])
    )

    # ── Combined opportunity ──
    d["combined_opportunity_monthly"] = _parse_money(
        _first_match(text, [
            r"Pet\s+Revenue\s+Found[\s\n]*\$([\d,]+)\s*/?\s*mo",
            r"Total\s+Opportunity[\s\n]*\$([\d,]+)",
        ])
    )
    # Fallback: compute if both pieces are present
    if d["combined_opportunity_monthly"] is None:
        mr = d.get("missing_monthly_rent") or 0
        sm = d.get("suspected_monthly") or 0
        if mr > 0 or sm > 0:
            d["combined_opportunity_monthly"] = mr + sm

    # ── Annual revenue impact ──
    d["annual_revenue_impact"] = _parse_money(
        _first_match(text, [
            r"Annual\s+Revenue\s+Impact[\s\n]*\$([\d,]+)",
            r"annual\s+basis[^$]*\$([\d,]+)\s+in\s+revenue",
        ])
    )

    # ── Total / data properties ──
    d["total_properties"] = _parse_int(
        _first_match(text, [
            r"Total\s+Properties?\s+in\s+Portfolio[\s\n]*(\d[\d,]*)",
            r"of\s+(\d[\d,]*)\s+total",
        ])
    )
    d["properties_with_data"] = _parse_int(
        _first_match(text, [
            r"Properties?\s+with\s+Charge\s+Data[\s\n]*(\d[\d,]*)",
            r"Across\s+(\d[\d,]*)\s+properties",
        ])
    )

    return d


# ─── Dedup logic ────────────────────────────────────────────────────
def dedup_key(info: Dict, data: Dict) -> str:
    """
    Build a dedup key for collapsing the same report across multiple run dates.

    Strategy:
      - prop_id + parent_id together (handles property AND parent reports cleanly).
      - Falls back to the filename stem WITHOUT the trailing _YYYYMMDD,
        so two PDFs with the same content but different run dates collapse.
    """
    pid    = info.get("prop_id", "")
    parent = info.get("parent_id", "")

    if pid and parent and pid != parent:
        return f"pp_{pid}_{parent}"
    if parent:
        return f"id_{parent}"
    if pid:
        return f"id_{pid}"

    # Fallback: strip the trailing _YYYYMMDD from the stem and use that
    stem = info.get("raw_stem", "")
    stem_no_date = re.sub(r"_\d{8}$", "", stem)
    return f"stem_{stem_no_date}"


# ─── Main ───────────────────────────────────────────────────────────
def main():
    pa = argparse.ArgumentParser(description="Extract key metrics from batch PDFs into a clean CSV.")
    pa.add_argument("folder", help="Folder containing PDFs (searches recursively)")
    pa.add_argument("-o", "--output", default=None,
                    help="Output CSV path (default: <folder>/pdf_summary_<date>.csv)")
    pa.add_argument("--dry-run", action="store_true",
                    help="Print results but don't write CSV")
    pa.add_argument("--max-pages", type=int, default=4,
                    help="PDF pages to scan (default 4)")
    pa.add_argument("--keep-dupes", action="store_true",
                    help="Keep all versions (don't dedup by parent_id)")
    args = pa.parse_args()

    folder = Path(args.folder)
    if not folder.is_dir():
        sys.exit(f"❌ {folder} is not a directory")

    pdfs = sorted(folder.rglob("*.pdf"))
    if not pdfs:
        sys.exit(f"❌ No PDFs found in {folder}")

    print(f"Scanning {len(pdfs)} PDFs in {folder}...")
    print("-" * 80)

    FIELDS = [
        "parent_id", "parent_name", "pmc", "run_date", "pdf_filename",
        "comparable_count", "comparable_units",
        "pre_revenue", "current_revenue", "post_avg_revenue",
        "monthly_lift", "lift_per_unit", "asset_value_impact",
        "uncollected_tenants", "missing_monthly_rent",
        "suspected_undisclosed", "suspected_monthly",
        "combined_opportunity_monthly", "annual_revenue_impact",
        "total_properties", "properties_with_data",
    ]

    rows_by_key: Dict[str, Dict] = {}
    errors = 0

    for pdf_path in pdfs:
        # Skip batch_log PDFs, temp files
        if pdf_path.name.startswith("batch_log") or pdf_path.name.startswith("."):
            continue

        info = parse_filename(pdf_path.name)
        text = read_pdf_text(pdf_path, max_pages=args.max_pages)
        if not text:
            errors += 1
            continue

        data = extract_all(text)

        # Figure out PMC from parent subfolder
        pmc = ""
        rel = pdf_path.relative_to(folder)
        if rel.parts and rel.parts[0].lower() in ("entrata", "yardi"):
            pmc = rel.parts[0].lower()

        row = {
            "parent_id": info.get("parent_id", ""),
            "parent_name": data.get("label", ""),
            "pmc": pmc,
            "run_date": info.get("run_date", ""),
            "pdf_filename": str(pdf_path.relative_to(folder)),
        }
        for f in FIELDS:
            if f not in row:
                row[f] = data.get(f, "")

        # Format numbers for readability (keep as numbers in CSV)
        for f in FIELDS:
            v = row.get(f)
            if v is None:
                row[f] = ""
            elif isinstance(v, float) and v == int(v):
                row[f] = int(v)

        key = dedup_key(info, data)

        if args.keep_dupes or key not in rows_by_key:
            rows_by_key[key] = row
        else:
            # Keep the one with the later run_date
            existing_date = rows_by_key[key].get("run_date", "")
            new_date = row.get("run_date", "")
            if new_date > existing_date:
                old_file = rows_by_key[key].get("pdf_filename", "")
                print(f"  ↻ Dedup {key}: keeping {new_date} over {existing_date} ({old_file})")
                rows_by_key[key] = row

    rows = sorted(rows_by_key.values(), key=lambda r: (r.get("pmc", ""), r.get("parent_name", "")))

    # Print preview
    print(f"\n{'='*80}")
    print(f"Extracted {len(rows)} unique reports ({len(pdfs)} PDFs scanned, {errors} errors)")
    duped = len(pdfs) - errors - len(rows)
    if duped > 0:
        print(f"  {duped} duplicate PDFs dropped (older run dates)")

    # Show top-level summary
    total_lift = sum(r.get("monthly_lift", 0) or 0 for r in rows if isinstance(r.get("monthly_lift"), (int, float)))
    total_opp = sum(r.get("combined_opportunity_monthly", 0) or 0 for r in rows if isinstance(r.get("combined_opportunity_monthly"), (int, float)))
    print(f"\n  Total monthly lift across all reports:       ${total_lift:,.0f}/mo")
    print(f"  Total combined opportunity across all reports: ${total_opp:,.0f}/mo")

    if args.dry_run:
        print("\n(dry run — no file written)")
        # Print a few sample rows
        print(f"\nSample rows (first 10):")
        print(f"  {'parent_id':>10}  {'date':>8}  {'name':<35}  {'lift':>10}  {'unc':>5}  {'susp':>5}")
        for r in rows[:10]:
            pid = str(r.get('parent_id', '?'))
            print(f"  {pid:>10}  {str(r.get('run_date','')):>8}  "
                  f"{str(r.get('parent_name',''))[:35]:<35}  "
                  f"${str(r.get('monthly_lift','--')):>9}  "
                  f"{str(r.get('uncollected_tenants','--')):>5}  "
                  f"{str(r.get('suspected_undisclosed','--')):>5}")
        return

    out_path = Path(args.output) if args.output else folder / f"pdf_summary_{datetime.now():%Y%m%d_%H%M%S}.csv"
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)

    print(f"\n✅ Saved: {out_path}")
    print(f"   {len(rows)} rows × {len(FIELDS)} columns")


if __name__ == "__main__":
    main()
