# Calculation Audit — July 2026

**Date:** 2026-07-05
**Scope:** lift/baseline methodology, revenue aggregation, missing pet rent
matching, suspected undisclosed estimation, fee estimation. Companion to
[realpage-accuracy-audit.md](realpage-accuracy-audit.md) (data-source
accuracy) and [app-audit.md](app-audit.md) (March backlog).

---

## Verdict summary

| Area | Verdict |
|---|---|
| Lift / baseline (`compute_launch_analysis`) | ✅ Sound |
| Lift consistency across Charts KPI / HTML report / PDF | ✅ Consistent (toggle-aware everywhere) |
| Revenue aggregation (spreading, one-time classification) | ✅ Sound; negative concessions now surfaced |
| Paying-set matching (unit/lease expansion, propagation) | ✅ Sound; timing hole **fixed** |
| Fee estimation (missing/suspected $ estimates) | 🔴 Was wrong — **fixed** |
| Missing/suspected dollar loops | 🔴 Overcounted duplicates — **fixed** |
| Organic-growth attribution | ⚠️ Open (see below) |

---

## What was verified sound (no changes)

1. **`compute_launch_analysis`**: pre-baseline averages only *observed*
   months (missing months are not treated as $0); launch month counts as
   post; monthly lift = cumulative impact ÷ completed post months (excludes
   the current partial month); total lift = observed post revenue −
   (pre_avg × post months). Internally consistent: monthly × months ≈ total.
2. **`data_starts_after_launch` guard**: properties whose charge history
   begins ≥3 months after launch (the RealPage EKS backfill case) are
   excluded from lift attribution instead of showing a fabricated $0
   baseline. Confirmed still necessary by the RealPage audit.
3. **Lift display consistency**: the default "Monthly Lift" (latest observed
   month − pre-PS baseline) and the toggled "Average Monthly Lift"
   (diff_monthly) are computed with the same formulas in the Charts KPI row,
   `generate_html_report`, and `generate_tranche_pdf`, all keyed off the
   same `use_avg_lift` flag (auto-enabled for student housing/seasonal).
   README's methodology section matches the code.
4. **Revenue aggregation**: one-time charges (median span ≤ 60 days, or
   explicit Entrata frequency) count only in their start month; recurring
   spread from charge start to end (or window end when open-ended);
   RealPage/AppFolio rows forced recurring because they are monthly
   snapshots. Matches README and yardi-validation fixes.
5. **Matching design**: (property, tenant_code) primary + email fallback,
   unit-level and lease-level expansion for roommates/co-signers, and
   any-lease→all-rows propagation per user. Design confirmed correct.

## What was fixed (commit a0cb5cc)

1. **Fee estimation was a row-weighted mean, not per-tenant.** The comment
   said "avg per tenant, then avg across tenants" but the code flattened all
   charge rows. A RealPage tenant with 18 monthly rows outweighed 17
   single-row tenants. Now: median amount per tenant per code → sum of a
   tenant's concurrent recurring codes → **median across tenants**
   (`_estimate_property_fees`). Also fixes app-audit.md issue #5 (mean vs
   median skew). Portfolio fallback is now a median across properties.
2. **Missing/suspected dollar loops counted duplicate rows.** A user with
   several f_leases rows (renewals, tenant_code variants survive SELECT
   DISTINCT) added the estimated fee once per row per month. Tenant *counts*
   already deduped by email; the *dollars* did not. Now `_dedup_missing_rows`
   keeps one row per user, preferring the row with live lease-date info.
3. **Paying sets ignored time.** Built from the full fetched history (up to
   24 months), so a *previous* tenant's pet charge marked the unit covered
   and hid the *current* non-paying resident. Missing/suspected matching now
   uses `_recent_paying_charges` (charges active in the last 90 days;
   open-ended charges kept unless tenant is Past/Former). Revenue charts
   intentionally keep full history.
4. **Negative concession rows (e.g. CONCPET) were silently dropped** from
   revenue aggregation. Still excluded (a concession is not collected
   revenue), but the Charts tab now shows a caption with the excluded row
   count and total so the story is checkable.
5. **Code-classification logic was triplicated** (Charts tab, missing,
   suspected) — consolidated into `_classify_pet_charge_codes`, removing the
   silent-divergence risk flagged in app-audit.md #6 for this piece.

## Open items

1. **Organic growth attribution** (app-audit.md question B): all post-launch
   growth is still attributed to PetScreening. If pet rent was trending up
   pre-launch, lift is overstated. Options: trend-adjusted baseline,
   disclaimer, or both numbers. Needs a team decision — changes headline
   numbers.
2. **`batch_pdf.py` summary lift** uses `diff_monthly` (average lift) while
   the app's default headline is simple lift (latest − baseline). Not wrong,
   but the batch summary CSV should label it "average monthly lift"
   explicitly to avoid cross-referencing confusion.
3. **Expected-value framing for suspected pets**: suspected estimates assume
   every suspected profile converts at the property's typical fee. Fine as
   "opportunity ceiling"; consider labeling as such in the PDF.

## Effect on reported numbers

- Missing/suspected **$ estimates change** (usually downward and more
  defensible): median fees remove deposit/outlier skew, dedup removes
  per-user double counting.
- Missing/suspected **tenant lists can grow**: tenants previously hidden by
  a prior tenant's stale charge are now correctly flagged.
- Revenue charts and lift **unchanged**.
