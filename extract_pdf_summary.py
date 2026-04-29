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
    """Parse a money-like string: '$13,816/mo', '+$14,824', '$177,890/yr', '~$4,471', etc."""
    if not s:
        return None
    raw = s.strip()
    # Strip suffixes that get attached to dollar values in PDFs
    raw = re.sub(r"\s*/\s*(?:mo|month|yr|year)\b", "", raw, flags=re.IGNORECASE)
    raw = raw.replace(",", "").replace("$", "").replace("+", "").replace("~", "").strip()
    try:
        return float(raw)
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


def _suffix_to_dollars(s: Optional[str]) -> Optional[float]:
    """Convert '$3.6M', '$1.5M', '$500K', or plain '$3,557,800' to a number."""
    if not s:
        return None
    raw = s.replace(",", "").replace("$", "").replace("~", "").strip()
    try:
        if raw.endswith("M"):
            return float(raw[:-1]) * 1_000_000
        if raw.endswith("K"):
            return float(raw[:-1]) * 1_000
        if raw.endswith("B"):
            return float(raw[:-1]) * 1_000_000_000
        return float(raw)
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
def parse_key_metrics_table(text: str) -> Dict[str, str]:
    """
    Parse the Key Metrics table on page 3, rendered as alternating
    METRIC / VALUE lines:
        Pre-PS Baseline
        $13,816/mo
        Current Monthly Pet-Related Revenue
        $30,340/mo
        ...
    Returns dict mapping a clean key to the raw value string.
    """
    result: Dict[str, str] = {}

    METRICS = [
        ("Total Properties in Portfolio",       "km_total_properties"),
        ("Comparable Properties (in analysis)", "km_comparable_properties"),
        ("Properties with Charge Data",         "km_properties_with_data"),
        ("Properties with Launch Date",         "km_properties_with_launch"),
        ("Total Portfolio Units",               "km_total_portfolio_units"),
        ("Units (in analysis)",                 "km_units_in_analysis"),
        ("Combined Lift per Unit",              "km_combined_lift_per_unit"),
        ("Projected Revenue at 100% Adoption",  "km_projected_rev_100pct"),
        ("Additional Opportunity at 100%",      "km_additional_opp_100pct"),
        ("Suspected Undisclosed Revenue",       "km_suspected_revenue"),
        ("Suspected Undisclosed Pets",          "km_suspected_count"),
        ("Tenants Not Paying Pet Rent",         "km_tenants_not_paying"),
        ("Est. Asset Value Impact (5% Cap)",    "km_asset_value"),
        ("Current Monthly Pet-Related Revenue", "km_current_rev"),
        ("Pre-PS Baseline",                     "km_pre_baseline"),
        ("Uncollected Revenue",                 "km_uncollected_revenue"),
        ("Annualized Lift",                     "km_annualized_lift"),
        ("Monthly Lift",                        "km_monthly_lift"),
        ("Unit Adoption",                       "km_unit_adoption"),
    ]

    km_idx = text.find("Key Metrics")
    if km_idx < 0:
        return result
    section = text[km_idx:]
    for stop in ("Impact by Property", "PetScreening Value Report | "):
        i = section.find(stop, 50)
        if i > 0:
            section = section[:i]
            break

    lines = [l.strip() for l in section.split("\n") if l.strip()]

    i = 0
    while i < len(lines) - 1:
        line = lines[i]
        for label, key in METRICS:
            if line == label and key not in result:
                result[key] = lines[i + 1]
                break
        i += 1

    return result


def extract_all(text: str) -> Dict[str, Any]:
    d: Dict[str, Any] = {}

    # Pull Key Metrics table values first — most reliable source
    km = parse_key_metrics_table(text)

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

    # ── Lift per unit (Actuals lift, NOT found per unit or combined) ──
    # Specific patterns to avoid grabbing FOUND PER UNIT / COMBINED LIFT PER UNIT
    d["lift_per_unit"] = _parse_money(
        _first_match(text, [
            r"\$(-?[\d,.]+)\s+per\s+unit\s+per\s+month",  # narrative form
            r"\$(-?[\d,.]+)\s*/\s*unit\s*/\s*mo[\s\n]+LIFT\s+PER\s+UNIT(?!\s+\(FOUND\))",
            r"^Lift\s+per\s+Unit[\s\n]*\+?\$(-?[\d,.]+)",
            r"\+?\$(-?[\d,.]+)\s*/\s*unit\s*/\s*mo[\s\n]+\$[\d,]+\s*/\s*[\d,]+\s+units",
        ])
    )

    # ── Asset value impact ──
    # Try the precise number first (~$3,557,800), then the M/K rounded form ($3.6M)
    avi_precise = _first_match(text, [
        r"~\$([\d,]+)\s+in\s+added\s+asset\s+value",
        r"Unrealized\s+Property\s+Value:\s*\$([\d,]+)",
        r"represents\s+~?\$([\d,]+)\s+in\s+added",
    ])
    if avi_precise:
        d["asset_value_impact"] = _parse_money(avi_precise)
    else:
        avi = _first_match(text, [
            r"Est\.?\s*Asset\s+Value\s+Impact[\s\n]*~?\$([\d,.]+[MKB])",
            r"~?\$([\d,.]+[MKB])\s+in\s+added\s+asset\s+value",
            r"Est\.?\s*Asset\s+Value\s+Impact[\s\n]*~?\$([\d,.]+)",
        ])
        d["asset_value_impact"] = _suffix_to_dollars(avi)

    # ── Uncollected tenants + missing monthly rent ──
    d["uncollected_tenants"] = _parse_int(
        _first_match(text, [
            r"(\d[\d,]*)\s+tenants?\s+have\s+completed\s+PetScreening\s+profiles\s+but\s+are\s+not",
            r"Tenants?\s+Not\s+Paying[\s\S]{0,60}?(\d[\d,]*)",
        ])
    )
    d["missing_monthly_rent"] = _parse_money(
        _first_match(text, [
            r"\$([\d,]+)[\s\n]+MISSING\s+MONTHLY\s+PET\s+RENT",
            r"MISSING\s+MONTHLY\s+PET\s+RENT[\s\n]+\$([\d,]+)",
            r"Missing\s+Monthly\s+Pet\s+Rent[\s\n]*\$([\d,]+)",
            r"\$([\d,]+)\s*/\s*mo\s+(?:in\s+)?uncollected",
            r"=\s*\$([\d,]+)\s*/\s*mo\s+uncollected",
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

    # ── Pet Revenue Found = uncollected + suspected (the "found" opportunity) ──
    d["pet_revenue_found_monthly"] = _parse_money(
        _first_match(text, [
            r"\$([\d,]+)\s*/\s*mo[\s\n]+PET\s+REVENUE\s+FOUND",
            r"PET\s+REVENUE\s+FOUND[\s\n]+\$([\d,]+)\s*/?\s*mo",
            r"Pet\s+Revenue\s+Found[\s\n]+\$([\d,]+)",
        ])
    )
    # Fallback: compute from missing + suspected
    if d["pet_revenue_found_monthly"] is None:
        mr = d.get("missing_monthly_rent") or 0
        sm = d.get("suspected_monthly") or 0
        if mr > 0 or sm > 0:
            d["pet_revenue_found_monthly"] = mr + sm

    # ── Total Opportunity = actuals lift + found (the headline combined number) ──
    d["total_opportunity_monthly"] = _parse_money(
        _first_match(text, [
            r"\$([\d,]+)\s*/\s*mo[\s\n]+TOTAL\s+OPPORTUNITY",
            r"TOTAL\s+OPPORTUNITY[\s\n]+\$([\d,]+)\s*/?\s*mo",
            r"Total\s+Opportunity[\s\n]+\$([\d,]+)",
        ])
    )
    # Fallback: actuals lift + found
    if d["total_opportunity_monthly"] is None:
        lift  = d.get("monthly_lift") or 0
        found = d.get("pet_revenue_found_monthly") or 0
        if lift > 0 or found > 0:
            d["total_opportunity_monthly"] = lift + found

    # Keep old field name as alias to total_opportunity for backward compat
    d["combined_opportunity_monthly"] = d["total_opportunity_monthly"]

    # ── Annual revenue impact (always shown as $X/yr in Opportunity section) ──
    d["annual_revenue_impact"] = _parse_money(
        _first_match(text, [
            r"\$([\d,]+)\s*/\s*yr[\s\n]+ANNUAL\s+REVENUE\s+IMPACT",
            r"ANNUAL\s+REVENUE\s+IMPACT[\s\n]+\$([\d,]+)\s*/?\s*yr",
            r"annual\s+basis[^$]*\$([\d,]+)\s+in\s+revenue",
            r"Annual\s+Revenue\s+Impact[\s\n]*\$([\d,]+)",
        ])
    )
    # Fallback: annualized lift = monthly_lift x 12 (different metric, last resort only)
    # We DON'T fall back to that — leave blank if unparseable, signals a bug to fix

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

    # ── Page 1 — "Opportunity" row (Found Per Unit + Est Value Impact (Found)) ──
    d["found_per_unit"] = _parse_money(
        _first_match(text, [
            r"\$(-?[\d,.]+)\s*/\s*unit\s*/\s*mo[\s\n]+FOUND\s+PER\s+UNIT",
            r"FOUND\s+PER\s+UNIT[\s\n]+\$(-?[\d,.]+)",
        ])
    )
    d["est_value_impact_found"] = _suffix_to_dollars(
        _first_match(text, [
            r"\$([\d,.]+[MKB]?)[\s\n]+EST\.?\s*VALUE\s+IMPACT\s*\(?FOUND\)?",
            r"EST\.?\s*VALUE\s+IMPACT\s*\(?FOUND\)?[\s\n]+\$([\d,.]+[MKB]?)",
        ])
    )
    d["combined_lift_per_unit"] = _parse_money(
        _first_match(text, [
            r"\$(-?[\d,.]+)\s*/\s*unit\s*/\s*mo[\s\n]+COMBINED\s+LIFT\s+PER\s+UNIT",
            r"COMBINED\s+LIFT\s+PER\s+UNIT[\s\n]+\+?\$(-?[\d,.]+)",
        ])
    )
    d["combined_est_value_impact"] = _suffix_to_dollars(
        _first_match(text, [
            r"\$([\d,.]+[MKB]?)[\s\n]+COMBINED\s+EST\.?\s*VALUE\s+IMPACT",
            r"COMBINED\s+EST\.?\s*VALUE\s+IMPACT[\s\n]+\$([\d,.]+[MKB]?)",
        ])
    )

    # ── Page 1 — How This Scales ──
    d["projected_annual_lift"] = _parse_money(
        _first_match(text, [
            r"\$([\d,]+)\s*/\s*yr[\s\n]+PROJECTED\s+ANNUAL\s+LIFT",
            r"approximately\s+\$([\d,]+)\s*/\s*yr\s+in\s+additional",
        ])
    )
    d["projected_asset_value_impact"] = _suffix_to_dollars(
        _first_match(text, [
            r"\$([\d,.]+[MKB]?)[\s\n]+PROJECTED\s+ASSET\s+VALUE\s+IMPACT",
            r"~\$([\d,]+)\s+in\s+added\s+asset\s+value\.\s*\n\s*~?[\d,]+",
        ])
    )

    # ── Page 2 — Portfolio Health ──
    d["avg_unit_adoption_pct"] = _parse_money(
        _first_match(text, [
            r"([\d.]+)\s*%[\s\n]+AVG\s+UNIT\s+ADOPTION",
            r"running\s+at\s+([\d.]+)\s*%\s+unit\s+adoption",
        ])
    )
    d["properties_with_launch"] = _parse_int(
        _first_match(text, [
            r"(\d[\d,]*)\s+of\s+\d[\d,]*\s+properties\s+have\s+an\s+established",
            r"(\d[\d,]*)[\s\n]+PROPERTIES\s+WITH\s+LAUNCH\s+DATE",
        ])
    )
    d["additional_opportunity_at_100pct"] = _parse_money(
        _first_match(text, [
            r"an\s+additional\s+\$([\d,]+)\s*/\s*mo\s+opportunity",
            r"\+?\$([\d,]+)\s*/\s*mo[\s\n]+ADDITIONAL\s+OPPORTUNITY",
        ])
    )
    d["projected_revenue_at_100pct"] = _parse_money(
        _first_match(text, [
            r"projected\s+pet\s+fee\s+revenue\s+would\s+be\s+\$([\d,]+)\s*/\s*mo",
            r"\$([\d,]+)\s*/\s*mo[\s\n]+PROJECTED\s+REVENUE",
        ])
    )

    # ── Page 2 — one-time uncollected fees ──
    d["one_time_uncollected"] = _parse_money(
        _first_match(text, [
            r"plus\s+\$([\d,]+)\s+in\s+uncollected\s+one-time",
            r"\+\s*\$([\d,]+)\s+one-time",
        ])
    )

    # ── Key Metrics table fallbacks — only fill if primary extraction missed ──
    # The table on page 3 is a clean ground-truth source for these numbers.
    if d.get("pre_revenue") is None:
        d["pre_revenue"] = _parse_money(km.get("km_pre_baseline"))
    if d.get("monthly_lift") is None:
        d["monthly_lift"] = _parse_money(km.get("km_monthly_lift"))
    if d.get("asset_value_impact") is None:
        d["asset_value_impact"] = _suffix_to_dollars(km.get("km_asset_value"))
    if d.get("uncollected_tenants") is None:
        d["uncollected_tenants"] = _parse_int(km.get("km_tenants_not_paying"))
    if d.get("missing_monthly_rent") is None:
        d["missing_monthly_rent"] = _parse_money(km.get("km_uncollected_revenue"))
    if d.get("suspected_undisclosed") is None:
        d["suspected_undisclosed"] = _parse_int(km.get("km_suspected_count"))
    if d.get("suspected_monthly") is None:
        d["suspected_monthly"] = _parse_money(km.get("km_suspected_revenue"))
    if d.get("comparable_units") is None:
        d["comparable_units"] = _parse_int(km.get("km_units_in_analysis"))
    if d.get("avg_unit_adoption_pct") is None:
        d["avg_unit_adoption_pct"] = _parse_money(km.get("km_unit_adoption"))
    if d.get("additional_opportunity_at_100pct") is None:
        d["additional_opportunity_at_100pct"] = _parse_money(km.get("km_additional_opp_100pct"))
    if d.get("projected_revenue_at_100pct") is None:
        d["projected_revenue_at_100pct"] = _parse_money(km.get("km_projected_rev_100pct"))

    # ── Key Metrics table — fields ONLY available there ──
    # "Comparable Properties (in analysis)" — form: "15 of 16"
    cp = km.get("km_comparable_properties", "")
    if cp:
        m = re.match(r"\s*(\d[\d,]*)\s*(?:of\s*(\d[\d,]*))?", cp)
        if m:
            if d.get("comparable_count") is None:
                d["comparable_count"] = _parse_int(m.group(1))
            if d.get("total_properties") is None and m.group(2):
                d["total_properties"] = _parse_int(m.group(2))

    pwl = km.get("km_properties_with_launch", "")
    if pwl:
        m = re.match(r"\s*(\d[\d,]*)", pwl)
        if m and d.get("properties_with_launch") is None:
            d["properties_with_launch"] = _parse_int(m.group(1))

    pwd = km.get("km_properties_with_data", "")
    if pwd:
        m = re.match(r"\s*(\d[\d,]*)", pwd)
        if m and d.get("properties_with_data") is None:
            d["properties_with_data"] = _parse_int(m.group(1))

    if km.get("km_total_portfolio_units"):
        d["total_portfolio_units"] = _parse_int(km["km_total_portfolio_units"])
    if km.get("km_total_properties"):
        d["total_properties_in_portfolio"] = _parse_int(km["km_total_properties"])
    if km.get("km_annualized_lift"):
        d["annualized_lift"] = _parse_money(km["km_annualized_lift"])
    if km.get("km_combined_lift_per_unit") and d.get("combined_lift_per_unit") is None:
        d["combined_lift_per_unit"] = _parse_money(km["km_combined_lift_per_unit"])

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
        # Identity
        "parent_id", "parent_name", "pmc", "run_date", "pdf_filename",

        # At a Glance — Actuals row
        "comparable_count", "comparable_units",
        "pre_revenue", "current_revenue", "post_avg_revenue",
        "monthly_lift", "lift_per_unit", "asset_value_impact",

        # At a Glance — Opportunity row (Pet Revenue Found)
        "pet_revenue_found_monthly", "found_per_unit", "est_value_impact_found",

        # At a Glance — Total Opportunity row (Actuals + Found)
        "total_opportunity_monthly", "combined_lift_per_unit", "combined_est_value_impact",
        "combined_opportunity_monthly",  # alias of total_opportunity

        # How This Scales (full-portfolio projections)
        "total_portfolio_units", "projected_annual_lift", "projected_asset_value_impact",

        # Revenue Opportunity (page 2)
        "uncollected_tenants", "missing_monthly_rent", "one_time_uncollected",
        "annual_revenue_impact",
        "suspected_undisclosed", "suspected_monthly",

        # Portfolio Health (page 2)
        "avg_unit_adoption_pct",
        "additional_opportunity_at_100pct", "projected_revenue_at_100pct",

        # Properties counts
        "properties_with_launch", "properties_with_data",
        "total_properties", "total_properties_in_portfolio",

        # From Key Metrics table (page 3) — only here
        "annualized_lift",
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
