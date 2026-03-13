# Yardi Validation Deep Dive — HQ Asset Living
**Date:** 2026-03-13  
**PMC tested:** HQ - Asset Living  
**Table:** `RAW.MISC.CHARGES_HQ_ASSET_LIVING_2026_03_13`

---

## Summary

Recreated all Streamlit app logic in raw Snowflake SQL to validate numbers end to end. Identified bugs, enhancements, and open questions about lift methodology.

---

## What Was Validated ✅

| Check | Result |
|-------|--------|
| Charge code grouping (pet-related codes via ILIKE `%pet%`, `%animal%`) | ✅ Matched |
| Portfolio-level monthly revenue (month over month) | ✅ Matched |
| Per-property monthly revenue | ✅ Matched |
| Pre/post launch lift aggregates | ✅ Matched (after fixes below) |
| Missing pet rent tenant count | ✅ Matched (after property set fix) |
| Roommate/unit-level expansion | ✅ Working correctly |

---

## Bugs Found

### 🐛 1. Missing Pet Rent Overcounting (FIXED)

**Problem:** The missing pets section queries ALL properties with active Yardi integrations under the parent company (`D_PROPERTIES` + `STG_PETSCREENING__INTEGRATIONS`), not just those with charge data. If a property wasn't in the Yardi export but had PetScreening profiles → every profile showed as "missing."

**Impact:** HQ Asset Living: 3,116 → 2,615 missing tenants (500 fewer).

**Fix:** Scope the profile query to only properties that appear in the charges data. Or at minimum, flag "no charge data" properties separately so the user knows the confidence level.

**Code location:** `generate_missing_pet_rent_report()` and `fetch_missing_pet_rent_by_property()` — the `property_ids` passed in come from `load_properties_for_selection()` which pulls all properties under the parent company, not just those with charges.

**Nuance:** The current behavior IS correct if the goal is "catch every property not charging pet rent." But it creates noise when a property was simply excluded from the data export. A middle ground: show them but flag them as "no charge data available."

---

### 🐛 2. One-Time Deposits Spread Across Months in Revenue Charts

**Problem:** The main revenue charts treat ALL charges identically. A $300 pet deposit with no `charge_to_date` on a current tenant gets counted in EVERY month from `charge_from_date` to today.

**How it works in revenue charts (broken):**
- Every charge: `from_date` → `effective_to_date` → count full `charge_amount` in every month of that range
- No distinction between recurring and one-time
- A one-time $300 deposit could show up as $300/month for years

**How it works in missing rent estimation (correct):**
- Yardi: inferred from median date span per charge code per property
  - Median span > 60 days → **recurring**
  - Median span ≤ 60 days → **one-time**
- Entrata: uses the explicit `frequency` field (`One-Time` vs `Monthly`)

**Fix:** Extend the recurring vs one-time classification to the revenue charts. Also add manual override (similar to how charge codes are selected) so users can flag codes as one-time.

**Note:** If deposits DO have proper `charge_to_date` values (same as `charge_from_date`), this isn't a problem for that data. Run the diagnostic query below to check per PMC.

**Diagnostic query:**
```sql
SELECT charge_code, charge_amount, charge_from_date, charge_to_date, tenant_status,
    CASE
        WHEN charge_to_date IS NOT NULL THEN 'has end date'
        WHEN LOWER(TRIM(tenant_status)) = 'current' THEN 'NULL → spreads to today'
        ELSE 'NULL → uses move_out/lease_to'
    END AS behavior
FROM "RAW"."MISC"."CHARGES_HQ_ASSET_LIVING_2026_03_13"
WHERE LOWER(charge_code) ILIKE ANY ('%pet%', '%animal%')
    AND charge_amount >= 200
ORDER BY charge_amount DESC
LIMIT 20;
```

---

### 🐛 3. Insufficient Baselines Still Show Lift in Individual Graphs

**Problem:** Properties with < 3 months of pre-launch data (or $0 in all pre months) still display a +$X lift value in the per-property graphs. They don't count toward the aggregate totals (correct), but the UI is misleading.

**Fix:** Omit the lift indicator entirely for properties flagged as `insufficient` baseline. Don't show the + number at the top of the individual graph.

---

### 🐛 4. Date Columns Had Wrong Centuries

**Problem:** Raw Yardi data had dates like `0025-07-01` instead of `2025-07-01`. The years lost their "20" prefix during import.

**Fix applied (one-time):**
```sql
UPDATE "RAW"."MISC"."CHARGES_HQ_ASSET_LIVING_2026_03_13"
SET
    launch_date = DATEADD(YEAR, 2000, launch_date),
    lease_from = DATEADD(YEAR, 2000, lease_from),
    -- (all date columns)
WHERE launch_date < '1000-01-01'
   OR lease_from < '1000-01-01'
   OR charge_from_date < '1000-01-01';
```

**Note:** Date columns are already DATE type in Snowflake, not VARCHAR. So `TRY_TO_DATE(col, 'YYYY-MM-DD')` fails with "too many arguments." Just reference the columns directly.

---

## Enhancements To Build

### 1. Recurring vs One-Time Classification for Revenue Charts
- Extend existing median-span inference to revenue chart calculations
- Add manual override toggle per charge code (like the existing charge code selector)
- This affects lift calculations too — one-time deposits shouldn't inflate monthly averages

### 2. Remove Lookback Slider
- Currently capped at 61 months with a slider
- We're pulling all the data via API anyway
- Just show all available data, no artificial cap

### 3. Don't Take Credit for Organic Rent Increases (Andrew's point)
- If pet rent was already trending up before PetScreening launched, the lift calculation attributes all of it to PS
- Need to think about how to separate organic growth from PS-driven growth
- Possible approach: look at the trend slope pre-launch and project it forward?

### 4. Flag "No Charge Data" Properties in Missing Pets
- Distinguish "property genuinely not charging pet rent" from "property wasn't in the data export"
- Show them but with a different indicator/grouping

---

## How The Lift Calculation Works (Reference)

### Per Property:
```
Pre-launch baseline = avg of last 3 months before launch (within lookback window)
Post-launch avg = avg of ALL post-launch months
Monthly lift = post avg − pre baseline
Total lift = monthly lift × number of post-launch months
```

### Aggregate:
```
Agg monthly change = SUM of each property's monthly lift (where pre_months > 0)
Agg total change = SUM of each property's total lift (where pre_months > 0)
```

### Example:
```
Property A launched June 2024:
  Pre avg (Mar-May 2024): $1,000/mo
  Post avg (Jun 2024–Mar 2026): $1,800/mo
  Monthly lift: +$800/mo
  Post months: 22
  Total lift: $800 × 22 = +$17,600

Property B launched Jan 2026:
  Pre avg (Oct-Dec 2025): $500/mo
  Post avg (Jan-Mar 2026): $900/mo
  Monthly lift: +$400/mo
  Post months: 3
  Total lift: $400 × 3 = +$1,200

Aggregate:
  Monthly change: $800 + $400 = +$1,200/mo
  Total change: $17,600 + $1,200 = +$18,800
```

### Key Detail: $0 Months Matter
The app generates $0 rows for months where a property had no pet charges (via `.get(m, 0)`). The SQL must do the same — cross join properties × months, then LEFT JOIN charges — otherwise the pre-launch baseline is inflated by excluding $0 months.

### Window Matters
The lookback window determines which months are in scope. The app uses `timedelta(days=N*30)` which is approximate. For 61 months from March 2026: window starts ~Feb 2021, snapped to month start. Properties that launched before the window start have 0 pre months and are excluded from comparable.

---

## Q&A Reference (Common Questions)

### "Are we only looking at household active?"
**Yes.** The profile query filters: `compliance_status = 'compliant'`, `user_pet_type = 'household'`, `user_pet_status = 'active'`. Same in both the app and the SQL.

### "What about pet deposits — are those one-time $300 charges being spread across months?"
**Yes, in the revenue charts.** No recurring vs one-time distinction there. But in the missing rent estimation, Yardi charges ARE classified (median span > 60 days = recurring, ≤ 60 days = one-time). Fix: extend that logic to revenue charts.

### "Does the app filter on charge_amount > 0 for the paying set?"
**No.** `_build_paying_sets` does `pet_rows = charges_df[charges_df['charge_code'].isin(selected_codes)]` with no amount filter. Even a $0 pet charge marks someone as "paying." The SQL had `AND charge_amount > 0` which was stricter.

### "Are roommates handled?"
**Yes.** Unit-level expansion: if any tenant on a `(property_id, unit_code)` has a pet charge, ALL tenants on that unit are marked as paying. Edge case: if a tenant has zero rows in the charges table at all (not just no pet charges), the unit expansion can't find them — falls back to email matching only.

### "Does insufficient baseline count in the aggregates?"
**The app includes everything with `n_pre > 0`** — no filter on `baseline_reliable`. But with a long enough lookback window (61 months), most properties either have 3+ pre months (reliable) or 0 (excluded), so there's rarely a 1-2 month gap.

### "Why didn't the aggregates match at first?"
Three reasons discovered in sequence:
1. **Month spine mismatch** — SQL started from Jan 2020, app used 61-month lookback (~Feb 2021). Different windows = different pre-launch months = different baselines.
2. **Missing $0 months** — SQL only had rows where `SUM(amount) > 0`. App generates $0 rows for every month. This changes pre-launch averages.
3. **Summing only reliable vs all comparable** — SQL summed only `baseline_quality = 'reliable'`. App sums all with `pre_months > 0`. In practice for 61-month window these were the same, but conceptually different.

### "How does the total lift work?"
Per property: `(post_avg_monthly - pre_avg_monthly) × post_months`. It answers "how much extra revenue did this property generate since launch compared to what they were doing before?" Longer time live + bigger monthly lift = bigger total. A property live for 2 years with $500/mo lift contributes way more than one launched last month with $2,000/mo lift.

### "What about properties where pet rent was already increasing before PS?"
Not handled currently. The lift calculation attributes all post-launch increase to PetScreening. Andrew flagged this as something to address — need a way to separate organic growth from PS-driven growth.

---

## Open Question for the Team

**How should we calculate the monthly lift?**

| Approach | Pre Baseline | Post Comparison | Pros | Cons |
|----------|-------------|-----------------|------|------|
| Current | Last 3 months pre-launch | Avg of ALL post months | Smooths out volatility | Early ramp-up drags down the number |
| Alternative | Last 3 months pre-launch | Most recent completed month | Shows current state vs before | Single month can be noisy |

---

## SQL Queries Used

All validated queries are in the companion file:
`projects/pet-rent/validation_hq_asset_living.md`

Key tables:
- `RAW.MISC.CHARGES_HQ_ASSET_LIVING_2026_03_13` — uploaded Yardi charges
- `PROD.COMMON.D_UNITS` / `D_PROPERTIES` / `F_LEASES` — PetScreening dimensional model
- `PROD.PETSCREENING.PETSCREENING__USER_ENRICHED` — profile data
- `PROD.REPORTING.R_MONTHLY_EXECUTIVE_SUMMARY` — detailed pet-level report data

App code: `~/clawd/projects/pet-rent/app.py` (7584 lines)
Key functions: `_build_paying_sets` (line 3322), `_apply_paying_flag` (line 3474), `generate_missing_pet_rent_report` (line 3504), `fetch_missing_pet_rent_by_property` (line 3668), `compute_launch_analysis` (line ~5000)
