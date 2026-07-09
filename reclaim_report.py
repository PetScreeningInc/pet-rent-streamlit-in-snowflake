#!/usr/bin/env python3
"""
reclaim_report.py — the reclaim-rate KPI for the solutions-org OKR.

Answers: of the tenants we flagged at baseline (pet profile with no pet
rent, abandoned/unresolved ESA), how many are PAYING now?

    reclaim rate = reclaimed / (baseline cohort - moved out)

Data comes from the flag cohorts saved on every report run
(fetch_cache.save_flagged_tenants -> CACHED_PET_VALUE_DATA, PMC_SYSTEM
'_flags') plus the latest cached charge rows for the same properties.

Usage:
    python reclaim_report.py                          # every label with 2+ runs
    python reclaim_report.py --label Greystar         # labels containing text
    python reclaim_report.py --baseline 2026-07-01 --as-of 2026-10-01
    python reclaim_report.py --csv reclaim_detail.csv # per-tenant classification

Classification per baseline-flagged tenant:
    reclaimed    -- no longer flagged, and a pet charge now exists for them
    still_flagged-- flagged again in the latest run
    unclear      -- no longer flagged and still present, but no pet charge
                    matched (charge-code selection changed, or roommate now
                    covers the lease) — verify manually
    moved_out    -- not in the latest charge data at all (excluded from the
                    net reclaim-rate denominator)
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PET_KEYWORDS = ("pet", "animal", "petnr", "concpet")
EXCLUDED_KEYWORDS = ("carpet", "puppet", "trumpet")

# Reason grouping for the OKR readout. "Unresolved assistance request" is
# the abandoned-ESA bucket the AA work targets. "assistance_non_responsive"
# is historical: a short-lived July 2026 experiment tagged non-responsive ESA
# inside the missing-pet-rent report under this reason before being reverted;
# backfilled cohorts still carry it, so it must keep reporting.
REASON_GROUPS = (
    "missing_pet_rent",
    "assistance_non_responsive",
    "Unresolved assistance request",
    "Abandoned household profile",
)


def is_pet_code(code) -> bool:
    c = str(code or "").lower()
    return any(k in c for k in PET_KEYWORDS) and not any(b in c for b in EXCLUDED_KEYWORDS)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--label", help="Only labels containing this text")
    ap.add_argument("--baseline", help="Baseline run = first run on/after this date (YYYY-MM-DD)")
    ap.add_argument("--as-of", dest="as_of", help="Latest run = last run on/before this date")
    ap.add_argument("--csv", help="Write per-tenant classification to this CSV")
    ap.add_argument("--global", dest="global_mode", action="store_true",
                    help="Ignore labels: baseline = latest flag per (property, tenant) "
                         "up to --baseline date; measured against everything after. "
                         "Use when the baseline was property-level runs but new runs "
                         "are parent-level (labels differ).")
    args = ap.parse_args()

    from fetch_cache import get_backend, SnowflakeCacheBackend
    backend = get_backend()
    if not isinstance(backend, SnowflakeCacheBackend):
        raise SystemExit("reclaim_report needs the Snowflake cache backend "
                         "(unset PET_VALUE_CACHE_BACKEND=local).")
    backend._ensure_table()
    conn = backend._connection()
    cur = conn.cursor()

    # ── 1. All flag runs, grouped by label ──
    label_filter = ""
    params = []
    if args.label:
        label_filter = "AND DATA:label::STRING ILIKE %s"
        params.append(f"%{args.label}%")
    cur.execute(f"""
        SELECT FETCH_ID, FETCHED_AT, DATA
        FROM {backend.table}
        WHERE PMC_SYSTEM = '_flags' AND RECORD_TYPE = 'flag_run' {label_filter}
        ORDER BY FETCHED_AT
    """, params)
    runs_by_label = defaultdict(list)
    for fetch_id, fetched_at, data in cur.fetchall():
        marker = json.loads(data) if isinstance(data, str) else data
        runs_by_label[marker.get("label", "?")].append(
            {"fetch_id": fetch_id, "at": fetched_at, "marker": marker})

    if not runs_by_label:
        raise SystemExit("No flag cohorts found yet — generate a report first "
                         "(app Summary tab or batch_pdf) to establish baselines.")

    if args.global_mode:
        _run_global(cur, backend, args, runs_by_label)
        cur.close()
        return

    def _load_flags(fetch_id):
        cur.execute(f"""
            SELECT PROPERTY_ID, DATA FROM {backend.table}
            WHERE PMC_SYSTEM = '_flags' AND RECORD_TYPE = 'flag' AND FETCH_ID = %s
        """, [fetch_id])
        out = {}
        for pid, data in cur.fetchall():
            f = json.loads(data) if isinstance(data, str) else data
            email = str(f.get("email") or "").lower().strip()
            if email:
                out[(str(pid), email)] = f
        return out

    detail_rows = []
    grand = defaultdict(lambda: defaultdict(int))
    grand_fee = defaultdict(float)

    for label, runs in sorted(runs_by_label.items()):
        baseline_run = runs[0]
        latest_run = runs[-1]
        if args.baseline:
            cands = [r for r in runs if str(r["at"])[:10] >= args.baseline]
            if cands:
                baseline_run = cands[0]
        if args.as_of:
            cands = [r for r in runs if str(r["at"])[:10] <= args.as_of]
            if cands:
                latest_run = cands[-1]

        if latest_run["fetch_id"] == baseline_run["fetch_id"]:
            print(f"\n== {label} ==")
            print(f"  1 run only ({str(baseline_run['at'])[:16]}) — baseline cohort "
                  f"established: {baseline_run['marker'].get('n_flags', 0)} flagged "
                  f"({baseline_run['marker'].get('reason_counts', {})}). "
                  f"Re-run the report later to measure reclaim.")
            continue

        base_flags = _load_flags(baseline_run["fetch_id"])
        latest_flags = _load_flags(latest_run["fetch_id"])

        # ── 2. Latest charge presence + pet-paying sets for covered properties ──
        prop_ids = [p for p in latest_run["marker"].get("property_ids", []) if p]
        present, paying = set(), set()
        if prop_ids:
            placeholders = ", ".join(["%s"] * len(prop_ids))
            cur.execute(f"""
                SELECT PROPERTY_ID,
                       LOWER(TRIM(DATA:email::STRING)) AS email,
                       DATA:charge_code::STRING AS code
                FROM {backend.table}
                WHERE RECORD_TYPE = 'charge'
                  AND PMC_SYSTEM NOT IN ('_flags', '_report')
                  AND PROPERTY_ID IN ({placeholders})
                QUALIFY FETCH_ID = FIRST_VALUE(FETCH_ID) OVER (
                    PARTITION BY PMC_SYSTEM, FETCH_MODE, PROPERTY_ID
                    ORDER BY FETCHED_AT DESC)
            """, prop_ids)
            for pid, email, code in cur.fetchall():
                if not email or email in ("none", "nan"):
                    continue
                key = (str(pid), email)
                present.add(key)
                if is_pet_code(code):
                    paying.add(key)

        # ── 3. Classify every baseline flag ──
        stats = defaultdict(lambda: defaultdict(int))
        fee_reclaimed = defaultdict(float)
        for key, f in base_flags.items():
            reason = f.get("reason") or "unknown"
            group = reason if reason in REASON_GROUPS else "Other suspected"
            if key in latest_flags:
                cls = "still_flagged"
            elif key in paying:
                cls = "reclaimed"
            elif key in present:
                cls = "unclear"
            else:
                cls = "moved_out"
            stats[group][cls] += 1
            stats[group]["cohort"] += 1
            grand[group][cls] += 1
            grand[group]["cohort"] += 1
            if cls == "reclaimed":
                fee_reclaimed[group] += float(f.get("est_fee_mo") or 0)
                grand_fee[group] += float(f.get("est_fee_mo") or 0)
            detail_rows.append({
                "label": label, "property_id": key[0],
                "property_name": f.get("property_name", ""),
                "tenant": f.get("name", ""), "email": key[1],
                "reason": reason, "classification": cls,
                "est_fee_mo": f.get("est_fee_mo", 0),
                "baseline_run": str(baseline_run["at"])[:16],
                "latest_run": str(latest_run["at"])[:16],
            })

        print(f"\n== {label} ==")
        print(f"  baseline {str(baseline_run['at'])[:16]}  ->  latest {str(latest_run['at'])[:16]}")
        _print_stats(stats, fee_reclaimed)

    if sum(g.get("cohort", 0) for g in grand.values()):
        print("\n== ALL LABELS COMBINED ==")
        _print_stats(grand, grand_fee)

    if args.csv and detail_rows:
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(detail_rows[0].keys()))
            w.writeheader()
            w.writerows(detail_rows)
        print(f"\nPer-tenant detail -> {args.csv}")

    cur.close()


def _run_global(cur, backend, args, runs_by_label):
    """Label-agnostic cohort comparison keyed on (property_id, email).

    Baseline = for each (property, tenant), their LATEST flag on/before the
    --baseline date (default: the median run date splits baseline/current).
    Current side = flags after that date plus the newest cached charges for
    the baseline properties.
    """
    cutoff = args.baseline
    if not cutoff:
        all_dates = sorted(str(r["at"])[:10] for runs in runs_by_label.values() for r in runs)
        cutoff = all_dates[len(all_dates) // 2]
    asof = args.as_of or "9999-12-31"
    print(f"GLOBAL mode: baseline = flags on/before {cutoff}; "
          f"measured against flags/charges through {asof}")

    cur.execute(f"""
        SELECT PROPERTY_ID, DATA, FETCHED_AT FROM {backend.table}
        WHERE PMC_SYSTEM = '_flags' AND RECORD_TYPE = 'flag'
        ORDER BY FETCHED_AT
    """)
    base_flags, later_flags = {}, {}
    for pid, data, at in cur.fetchall():
        f = json.loads(data) if isinstance(data, str) else data
        email = str(f.get("email") or "").lower().strip()
        if not email or not str(pid).strip():
            continue
        key = (str(pid), email)
        d = str(at)[:10]
        if d <= cutoff:
            base_flags[key] = f          # latest flag wins per tenant
        elif d <= asof:
            later_flags[key] = f

    prop_ids = sorted({k[0] for k in base_flags})
    print(f"baseline cohort: {len(base_flags)} tenants across {len(prop_ids)} properties")

    present, paying = set(), set()
    for start in range(0, len(prop_ids), 5000):
        chunk = prop_ids[start:start + 5000]
        placeholders = ", ".join(["%s"] * len(chunk))
        cur.execute(f"""
            SELECT PROPERTY_ID,
                   LOWER(TRIM(DATA:email::STRING)) AS email,
                   DATA:charge_code::STRING AS code
            FROM {backend.table}
            WHERE RECORD_TYPE = 'charge'
              AND PMC_SYSTEM NOT IN ('_flags', '_report')
              AND PROPERTY_ID IN ({placeholders})
              AND FETCHED_AT::DATE > TO_DATE(%s)
            QUALIFY FETCH_ID = FIRST_VALUE(FETCH_ID) OVER (
                PARTITION BY PMC_SYSTEM, FETCH_MODE, PROPERTY_ID
                ORDER BY FETCHED_AT DESC)
        """, chunk + [cutoff])
        for pid, email, code in cur.fetchall():
            if not email or email in ("none", "nan"):
                continue
            key = (str(pid), email)
            present.add(key)
            if is_pet_code(code):
                paying.add(key)

    measured_props = {k[0] for k in later_flags} | {k[0] for k in present}
    stats = defaultdict(lambda: defaultdict(int))
    fee_reclaimed = defaultdict(float)
    detail = []
    unmeasured = 0
    for key, f in base_flags.items():
        if key[0] not in measured_props:
            unmeasured += 1
            continue  # property not re-pulled since baseline — can't judge yet
        reason = f.get("reason") or "unknown"
        group = reason if reason in REASON_GROUPS else "Other suspected"
        if key in later_flags:
            cls = "still_flagged"
        elif key in paying:
            cls = "reclaimed"
        elif key in present:
            cls = "unclear"
        else:
            cls = "moved_out"
        stats[group][cls] += 1
        stats[group]["cohort"] += 1
        if cls == "reclaimed":
            fee_reclaimed[group] += float(f.get("est_fee_mo") or 0)
        detail.append({"property_id": key[0], "property_name": f.get("property_name", ""),
                       "tenant": f.get("name", ""), "email": key[1], "reason": reason,
                       "classification": cls, "est_fee_mo": f.get("est_fee_mo", 0)})

    if unmeasured:
        print(f"not yet measurable (property not re-pulled since baseline): {unmeasured} tenants")
    _print_stats(stats, fee_reclaimed)

    if args.csv and detail:
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(detail[0].keys()))
            w.writeheader()
            w.writerows(detail)
        print(f"\nPer-tenant detail -> {args.csv}")


def _print_stats(stats, fee_reclaimed):
    hdr = f"  {'reason':<32} {'cohort':>6} {'reclaimed':>9} {'still':>6} {'unclear':>7} {'moved':>6} {'net rate':>8} {'$/mo':>8}"
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    order = list(REASON_GROUPS) + ["Other suspected"]
    for group in order:
        if group not in stats:
            continue
        st = stats[group]
        cohort = st.get("cohort", 0)
        rec = st.get("reclaimed", 0)
        moved = st.get("moved_out", 0)
        denom = max(1, cohort - moved)
        print(f"  {group:<32} {cohort:>6} {rec:>9} {st.get('still_flagged', 0):>6} "
              f"{st.get('unclear', 0):>7} {moved:>6} {rec / denom:>7.1%} "
              f"{fee_reclaimed.get(group, 0):>8,.0f}")


if __name__ == "__main__":
    main()
