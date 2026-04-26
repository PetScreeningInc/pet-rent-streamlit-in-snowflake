#!/usr/bin/env python3
"""
fix_batch_log.py — Recompute monthly_lift in a batch_log CSV by reading the PDFs.

Use when batch_pdf.py logged an incorrect monthly_lift but the PDFs are correct.

Usage:
    python fix_batch_log.py batch_pdfs/batch_log_YYYYMMDD_HHMMSS.csv

Creates a corrected copy at batch_log_*_FIXED.csv (does NOT overwrite original).
Reads each PDF and extracts the actual lift number from the "At a Glance" text.
"""

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


def extract_lift_from_pdf(pdf_path: Path):
    """Extract monthly lift from PDF 'At a Glance' section.

    Looks for patterns like:
      "a $1,100/mo increase"
      "a $-462/mo decrease"
    """
    try:
        reader = PdfReader(str(pdf_path))
        text = ""
        for page in reader.pages[:2]:  # First 2 pages have "At a Glance"
            text += page.extract_text() + "\n"

        # Match: "a $1,100/mo increase" or "a $-462/mo decrease"
        # Also handle: "a $1,100/mo" (sometimes wraps)
        match = re.search(r"\$(-?[\d,]+)/mo\s+(increase|decrease)?", text)
        if match:
            num_str = match.group(1).replace(",", "")
            return float(num_str)

        # Try alternate format: "Monthly Lift\n$1,100/mo"
        match = re.search(r"Monthly Lift[\s\n]*\$(-?[\d,]+)", text)
        if match:
            return float(match.group(1).replace(",", ""))

        return None
    except Exception as e:
        print(f"  Error reading {pdf_path.name}: {e}")
        return None


def main():
    if len(sys.argv) < 2:
        print("Usage: python fix_batch_log.py <path_to_batch_log.csv>")
        sys.exit(1)

    csv_path = Path(sys.argv[1])
    if not csv_path.exists():
        print(f"Error: {csv_path} not found")
        sys.exit(1)

    output_path = csv_path.parent / csv_path.stem.replace("_FIXED", "")
    output_path = output_path.with_name(output_path.name + "_FIXED.csv")

    pdfs_dir = csv_path.parent  # PDFs in same dir or subfolder

    print(f"Reading: {csv_path}")
    print(f"Writing: {output_path}")
    print("-" * 60)

    fixed_count = 0
    error_count = 0

    with open(csv_path, "r", newline="") as fin, open(output_path, "w", newline="") as fout:
        reader = csv.DictReader(fin)
        fieldnames = reader.fieldnames or []
        writer = csv.DictWriter(fout, fieldnames=fieldnames)
        writer.writeheader()

        for row in reader:
            pdf_filename = row.get("pdf_filename", "")
            if not pdf_filename or row.get("status") != "success":
                writer.writerow(row)
                continue

            # Find the PDF (may be in subfolder)
            pdf_path = pdfs_dir / pdf_filename
            if not pdf_path.exists():
                # Try just filename in any subdir
                matches = list(pdfs_dir.rglob(Path(pdf_filename).name))
                if matches:
                    pdf_path = matches[0]
                else:
                    print(f"  PDF not found: {pdf_filename}")
                    error_count += 1
                    writer.writerow(row)
                    continue

            old_lift = row.get("monthly_lift", "")
            new_lift = extract_lift_from_pdf(pdf_path)

            if new_lift is not None:
                if abs(float(old_lift or 0) - new_lift) > 0.5:  # Only update if different
                    print(f"  {row.get('id'):>8} {row.get('label', '')[:40]:<40} "
                          f"OLD: ${float(old_lift or 0):>10,.2f}  NEW: ${new_lift:>10,.2f}")
                    row["monthly_lift"] = round(new_lift, 2)
                    fixed_count += 1
            else:
                print(f"  Could not extract lift from {pdf_filename}")
                error_count += 1

            writer.writerow(row)

    print("-" * 60)
    print(f"Fixed {fixed_count} rows, {error_count} errors")
    print(f"Output: {output_path}")


if __name__ == "__main__":
    main()
