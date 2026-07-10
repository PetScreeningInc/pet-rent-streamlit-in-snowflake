#!/usr/bin/env python3
"""
backfill_flags.py — turn already-exported missing/suspected CSVs into
reclaim-tracking baseline cohorts.

Why: the big all-portfolio pull for HubSpot was run BEFORE reclaim
tracking existed, so those runs never wrote cohorts to Snowflake. The
CSVs themselves contain everything a cohort needs (property, tenant
email, reason) and the filename carries the original run timestamp —
so this script ingests a folder of them and backdates the cohorts to the
original pull. That pull becomes the H2 baseline without re-running
anything.

Usage:
    python backfill_flags.py ./batch_csvs --dry-run     # show what it would do
    python backfill_flags.py ./batch_csvs               # write cohorts

Expects the batch_csv.py layout/naming:
    <folder>/<pmc_system>/missing_pet_rent_<label>_<id>_<YYYYMMDD_HHMMSS>.csv
    <folder>/<pmc_system>/suspected_undisclosed_pets_<label>_<id>_<YYYYMMDD_HHMMSS>.csv

Idempotent: a run whose (label, timestamp) already has a flag_run row in
Snowflake is skipped, so re-running after adding files is safe.

Notes:
  - est_fee_mo is 0 for backfilled flags (the old CSVs don't carry the
    per-property fee). Reclaim *rates* are unaffected; only the $/mo
    reclaimed column will undercount for backfilled cohorts.
  - The label is reconstructed as "<pmc>::<label with underscores as
    spaces>" to line up with the labels future batch runs write. Check
    the printed labels match `reclaim_report.py` output before trusting
    cross-run comparisons.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Two naming variants exist: batch_csv's `<type>_<label>_<id>_<ts>.csv`
# and the Hermes runs' `<type>_<label>_<id>_<ts>_<pid>.csv` (extra numeric
# process suffix after the timestamp).
FNAME_RE = re.compile(
    r"^(missing_pet_rent|suspected_undisclosed_pets)_(.+)_([^_]+)_(\d{8}_\d{6})(?:_\d+)?\.csv$"
)


def parse_filename(name: str):
    m = FNAME_RE.match(name)
    if not m:
        return None
    report_type, label_part, id_part, ts_part = m.groups()
    ts = datetime.strptime(ts_part, "%Y%m%d_%H%M%S")
    label = label_part.replace("_", " ").strip()
    return report_type, label, id_part, ts


def read_flags(path: Path, report_type: str):
    """One flag per (property_id, email) — CSVs are per-pet granularity."""
    flags = {}
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            get = lambda *keys: next(
                (str(row[k]).strip() for k in keys if k in row and str(row[k]).strip()), "")
            email = get("USER_EMAIL", "user_email", "EMAIL", "email").lower()
            pid = get("PROPERTY_ID", "property_id")
            if not email or not pid:
                continue
            if report_type == "missing_pet_rent":
                reason = "missing_pet_rent"
            else:
                reason = get("SUSPECTED_REASON", "suspected_reason") or "Other suspected"
            flags[(pid, email, reason)] = {
                "property_id": pid,
                "property_name": get("PROPERTY_NAME", "property_name"),
                "email": email,
                "name": f"{get('USER_FIRST_NAME', 'user_first_name')} "
                        f"{get('USER_LAST_NAME', 'user_last_name')}".strip(),
                "reason": reason,
                "est_fee_mo": 0.0,
                "backfilled": True,
            }
    return list(flags.values())


def existing_runs(backend):
    """{(label, 'YYYY-MM-DD HH:MM'): True} for already-saved flag runs."""
    cur = backend._connection().cursor()
    try:
        cur.execute(f"""
            SELECT DATA:label::STRING, TO_CHAR(FETCHED_AT, 'YYYY-MM-DD HH24:MI')
            FROM {backend.table}
            WHERE PMC_SYSTEM = '_flags' AND RECORD_TYPE = 'flag_run'
        """)
        return {(r[0], r[1]) for r in cur.fetchall()}
    finally:
        cur.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder", help="Folder containing the exported CSVs (searched recursively)")
    ap.add_argument("--dry-run", action="store_true", help="Show what would be written")
    args = ap.parse_args()

    root = Path(args.folder)
    if not root.exists():
        raise SystemExit(f"Folder not found: {root}")

    # Group files by run: (pmc_dir, label, id, timestamp)
    runs = defaultdict(list)
    skipped_names = []
    for path in sorted(root.rglob("*.csv")):
        parsed = parse_filename(path.name)
        if not parsed:
            if not path.name.startswith("batch_csv_log"):
                skipped_names.append(path.name)
            continue
        report_type, label, id_part, ts = parsed
        pmc_dir = path.parent.name if path.parent != root else ""
        runs[(pmc_dir, label, id_part, ts)].append((report_type, path))

    if skipped_names:
        print(f"Skipped {len(skipped_names)} unrecognized filenames "
              f"(e.g. {skipped_names[0]})")
    if not runs:
        raise SystemExit("No missing/suspected CSVs recognized in that folder.")

    from fetch_cache import get_backend, SnowflakeCacheBackend, save_flagged_tenants
    backend = get_backend()
    if not isinstance(backend, SnowflakeCacheBackend):
        raise SystemExit("Backfill needs the Snowflake cache backend.")
    backend._ensure_table()
    already = existing_runs(backend)

    written = skipped = 0
    for (pmc_dir, label, id_part, ts), files in sorted(runs.items()):
        snap_label = f"{pmc_dir}::{label}" if pmc_dir else label
        key = (snap_label, ts.strftime("%Y-%m-%d %H:%M"))
        flags = []
        for report_type, path in files:
            flags.extend(read_flags(path, report_type))
        prop_ids = sorted({f["property_id"] for f in flags})
        reasons = defaultdict(int)
        for f in flags:
            reasons[f["reason"]] += 1

        status = "SKIP (already saved)" if key in already else (
            "DRY-RUN" if args.dry_run else "WRITE")
        print(f"{status:<20} {snap_label}  @ {ts:%Y-%m-%d %H:%M}  — "
              f"{len(flags)} tenants across {len(prop_ids)} properties {dict(reasons)}")
        if key in already:
            skipped += 1
            continue
        if args.dry_run:
            continue
        save_flagged_tenants(snap_label, pmc_dir, prop_ids, flags, as_of=ts)
        written += 1

    print(f"\nDone: {written} cohorts written, {skipped} already existed"
          f"{' (dry run — nothing written)' if args.dry_run else ''}.")
    if written and not args.dry_run:
        print("Verify with: python reclaim_report.py")


if __name__ == "__main__":
    main()
