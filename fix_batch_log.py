#!/usr/bin/env python3
"""
fix_batch_log.py — Recompute batch_log fields from the actual PDFs.

Use when batch_pdf.py logged incorrect numbers but the PDFs are correct.

Recovers (from PDF text):
    monthly_lift          from "At a Glance" narrative
    comparable_count      from "Across N comparable properties" / KPI cards
    uncollected_tenants   from "Tenants Not Paying" card / Opportunity narrative
    suspected_undisclosed from "N suspected undisclosed" / Opportunity narrative

Fields NOT touched (PDFs don't render these as standalone numbers):
    properties_found, properties_with_data, parent_company_*

Usage:
    python fix_batch_log.py batch_pdfs/batch_log_YYYYMMDD_HHMMSS.csv
    python fix_batch_log.py batch_pdfs/batch_log_*.csv --dry-run

Creates a corrected copy at batch_log_*_FIXED.csv (does NOT overwrite original).
"""

import argparse
import csv
import re
import sys
from pathlib import Path

try:
    from pypdf import PdfReader
except ImportError:
    try:
        from PyPDF2 import PdfReader
    except ImportError:
        print("Error: install pypdf or PyPDF2: pip install pypdf")
        sys.exit(1)


# ─── PDF text extraction ────────────────────────────────────────────
def read_pdf_text(pdf_path: Path, max_pages: int = 4) -> str:
    """Read first N pages — At a Glance + Revenue Opportunity sit there."""
    try:
        reader = PdfReader(str(pdf_path))
        text_parts = []
        for page in reader.pages[:max_pages]:
            text_parts.append(page.extract_text() or "")
        return "\n".join(text_parts)
    except Exception as e:
        print(f"  Error reading {pdf_path.name}: {e}")
        return ""


# ─── Extractors (each returns Optional[number] or None if not found) ─
def extract_monthly_lift(text: str):
    """
    Find the headline lift dollar figure.
    Patterns (in priority order):
      "$X,XXX/mo average monthly lift"
      "$X,XXX/mo increase"
      "$X,XXX/mo decrease"           (treat as positive — magnitude is what's logged)
      "Monthly Lift\n$X,XXX/mo"      (KPI card layout)
      "Average Monthly Lift\n$X,XXX/mo"
    Negative values can show as "$-462/mo" — handle that too.
    """
    # Highest signal: explicit "average monthly lift" or "increase"/"decrease"
    m = re.search(r"\$(-?[\d,]+)\s*/\s*mo\s+average\s+monthly\s+lift", text, re.IGNORECASE)
    if m:
        return _to_float(m.group(1))

    m = re.search(r"\$(-?[\d,]+)\s*/\s*mo\s+(?:increase|decrease)", text, re.IGNORECASE)
    if m:
        val = _to_float(m.group(1))
        # "decrease" + positive number → negative lift
        if "decrease" in text[m.start():m.start() + 80].lower() and val and val > 0:
            val = -val
        return val

    # KPI card form
    m = re.search(r"(?:Average\s+)?Monthly\s+Lift[\s\n]*\$(-?[\d,]+)", text, re.IGNORECASE)
    if m:
        return _to_float(m.group(1))

    return None


def extract_comparable_count(text: str):
    """
    "Across N comparable properties" — most reliable.
    Falls back to KPI sub-text "N properties with pre & post data".
    """
    m = re.search(r"Across\s+(\d{1,5})\s+comparable\s+propert", text, re.IGNORECASE)
    if m:
        return int(m.group(1))

    m = re.search(r"(\d{1,5})\s+properties?\s+with\s+pre\s*(?:&|and)\s*post\s+data", text, re.IGNORECASE)
    if m:
        return int(m.group(1))

    return None


def extract_uncollected_tenants(text: str):
    """
    Patterns:
      "Tenants Not Paying" card → next number
      "N tenants have completed PetScreening profiles but are not being charged"
      "N tenants are not paying"
    """
    # Narrative form is the clearest
    m = re.search(
        r"(\d[\d,]{0,7})\s+tenants?\s+have\s+completed\s+PetScreening\s+profiles\s+but\s+are\s+not",
        text, re.IGNORECASE,
    )
    if m:
        return int(m.group(1).replace(",", ""))

    # Card form: "Tenants Not Paying" appears on its own line, then a number nearby
    m = re.search(r"Tenants\s+Not\s+Paying[\s\S]{0,80}?(\d[\d,]{0,7})", text, re.IGNORECASE)
    if m:
        return int(m.group(1).replace(",", ""))

    return None


def extract_suspected_undisclosed(text: str):
    """
    "N tenants show signals of undisclosed pets"
    "N suspected undisclosed"
    """
    m = re.search(
        r"(\d[\d,]{0,7})\s+tenants?\s+show\s+signals\s+of\s+undisclosed",
        text, re.IGNORECASE,
    )
    if m:
        return int(m.group(1).replace(",", ""))

    m = re.search(r"(\d[\d,]{0,7})\s+suspected\s+undisclosed", text, re.IGNORECASE)
    if m:
        return int(m.group(1).replace(",", ""))

    return None


def _to_float(s):
    try:
        return float(s.replace(",", ""))
    except (ValueError, AttributeError):
        return None


# ─── Diff helpers ───────────────────────────────────────────────────
def _diff_num(old, new, tol=0.5):
    """Returns True if the values differ by more than tol."""
    if new is None:
        return False
    try:
        old_v = float(old) if old not in (None, "", "None") else 0.0
    except (ValueError, TypeError):
        old_v = 0.0
    return abs(old_v - float(new)) > tol


def _diff_int(old, new):
    if new is None:
        return False
    try:
        old_v = int(float(old)) if old not in (None, "", "None") else 0
    except (ValueError, TypeError):
        old_v = 0
    return old_v != int(new)


# ─── Main ──────────────────────────────────────────────────────────
def main():
    pa = argparse.ArgumentParser(description="Fix batch_log CSV by re-reading PDFs.")
    pa.add_argument("csv", help="Path to batch_log_*.csv")
    pa.add_argument("--dry-run", action="store_true",
                    help="Print proposed changes without writing FIXED file")
    pa.add_argument("--max-pages", type=int, default=4,
                    help="PDF pages to scan (default 4)")
    pa.add_argument("--quiet", action="store_true",
                    help="Only print rows that changed")
    args = pa.parse_args()

    csv_path = Path(args.csv)
    if not csv_path.exists():
        sys.exit(f"❌ {csv_path} not found")

    output_path = csv_path.parent / (csv_path.stem.replace("_FIXED", "") + "_FIXED.csv")
    pdfs_dir = csv_path.parent

    print(f"Reading: {csv_path}")
    if args.dry_run:
        print("DRY RUN — no file will be written")
    else:
        print(f"Writing: {output_path}")
    print("-" * 80)

    fields_changed = {
        "monthly_lift": 0,
        "comparable_count": 0,
        "uncollected_tenants": 0,
        "suspected_undisclosed": 0,
    }
    rows_changed = 0
    rows_processed = 0
    pdf_missing = 0
    extract_failures = 0

    rows_out = []

    with open(csv_path, "r", newline="", encoding="utf-8") as fin:
        reader = csv.DictReader(fin)
        fieldnames = reader.fieldnames or []

        for row in reader:
            pdf_filename = row.get("pdf_filename", "")
            status = row.get("status", "")

            # Only attempt fix on successful rows with a PDF
            if not pdf_filename or status != "success":
                rows_out.append(row)
                continue

            rows_processed += 1

            # Resolve PDF location (may be in pmc subfolder)
            pdf_path = pdfs_dir / pdf_filename
            if not pdf_path.exists():
                matches = list(pdfs_dir.rglob(Path(pdf_filename).name))
                if matches:
                    pdf_path = matches[0]
                else:
                    pdf_missing += 1
                    if not args.quiet:
                        print(f"  ! PDF missing: {pdf_filename}")
                    rows_out.append(row)
                    continue

            text = read_pdf_text(pdf_path, max_pages=args.max_pages)
            if not text:
                extract_failures += 1
                rows_out.append(row)
                continue

            # Extract each field
            new_lift  = extract_monthly_lift(text)
            new_comp  = extract_comparable_count(text)
            new_unc   = extract_uncollected_tenants(text)
            new_susp  = extract_suspected_undisclosed(text)

            old_lift = row.get("monthly_lift", "")
            old_comp = row.get("comparable_count", "")
            old_unc  = row.get("uncollected_tenants", "")
            old_susp = row.get("suspected_undisclosed", "")

            changes = []
            if _diff_num(old_lift, new_lift):
                changes.append(f"lift ${_safe_float(old_lift):,.0f} → ${new_lift:,.0f}")
                row["monthly_lift"] = round(new_lift, 2)
                fields_changed["monthly_lift"] += 1

            if _diff_int(old_comp, new_comp):
                changes.append(f"comp {old_comp or 0} → {new_comp}")
                row["comparable_count"] = int(new_comp)
                fields_changed["comparable_count"] += 1

            if _diff_int(old_unc, new_unc):
                changes.append(f"unc {old_unc or 0} → {new_unc}")
                row["uncollected_tenants"] = int(new_unc)
                fields_changed["uncollected_tenants"] += 1

            if _diff_int(old_susp, new_susp):
                changes.append(f"susp {old_susp or 0} → {new_susp}")
                row["suspected_undisclosed"] = int(new_susp)
                fields_changed["suspected_undisclosed"] += 1

            if changes:
                rows_changed += 1
                lab = (row.get("label", "") or "")[:35]
                rid = row.get("id", "")
                print(f"  {rid:>8}  {lab:<35}  " + "  ·  ".join(changes))
            elif not args.quiet:
                # Note: silently OK rows are skipped unless verbose
                pass

            rows_out.append(row)

    # Write output
    if not args.dry_run:
        with open(output_path, "w", newline="", encoding="utf-8") as fout:
            writer = csv.DictWriter(fout, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows_out)

    print("-" * 80)
    print(f"Rows processed: {rows_processed}")
    print(f"Rows changed:   {rows_changed}")
    for fld, n in fields_changed.items():
        if n:
            print(f"  {fld:<25} {n} updated")
    if pdf_missing:
        print(f"PDFs missing:    {pdf_missing}")
    if extract_failures:
        print(f"Extract failures: {extract_failures}")
    if args.dry_run:
        print("\n(dry run — no file written)")
    else:
        print(f"\nOutput: {output_path}")


def _safe_float(v):
    try:
        return float(v) if v not in (None, "", "None") else 0.0
    except (ValueError, TypeError):
        return 0.0


if __name__ == "__main__":
    main()
