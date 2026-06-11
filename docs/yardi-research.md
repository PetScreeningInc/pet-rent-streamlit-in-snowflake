# Yardi Research & Validation

Consolidated validation findings and step-by-step SQL queries for the Yardi integration in the Pet Rent app. Based on the HQ Asset Living deep dive (2026-03-13).

## Table of Contents

- [Validation Findings](#validation-findings)
- [HQ Asset Living: Step-by-Step Validation](#hq-asset-living-step-by-step-validation)

---

## Validation Findings

**Date:** 2026-03-13  
**PMC tested:** HQ - Asset Living  
**Table:** `RAW.MISC.CHARGES_HQ_ASSET_LIVING_2026_03_13`

### Summary

Recreated all Streamlit app logic in raw Snowflake SQL to validate numbers end to end. Identified bugs, enhancements, and open questions about lift methodology.

### What Was Validated ✅

| Check | Result |
|-------|--------|
| Charge code grouping (pet-related codes via ILIKE `%pet%`, `%animal%`) | ✅ Matched |
| Portfolio-level monthly revenue (month over month) | ✅ Matched |
| Per-property monthly revenue | ✅ Matched |
| Pre/post launch lift aggregates | ✅ Matched (after fixes below) |
| Missing pet rent tenant count | ✅ Matched (after property set fix) |
| Roommate/unit-level expansion | ✅ Working correctly |

### Bugs Found

#### 🐛 1. Missing Pet Rent Overcounting — ✅ FIXED

**Problem (resolved):** The missing pets section previously queried ALL properties with active Yardi integrations under the parent company (`D_PROPERTIES` + `STG_PETSCREENING__INTEGRATIONS`), not just those with charge data. If a property wasn't in the Yardi export but had PetScreening profiles → every profile showed as "missing."

**Impact:** HQ Asset Living: 3,116 → 2,615 missing tenants (500 fewer).

**Fix applied:** This was fixed by scoping `property_ids` to only those present in the charge data. The app now filters: `property_ids = [pid for pid in property_ids if str(pid).strip() in _charge_pids]`. Properties without charge data are no longer included in the missing pet rent denominator.

**Code location:** `generate_missing_pet_rent_report()` and `fetch_missing_pet_rent_by_property()` — property scoping now applied at lines ~4023, ~4202, ~4498.

#### 🐛 2. One-Time Deposits Spread Across Months in Revenue Charts — ✅ FIXED

**Problem (resolved):** The main revenue charts previously treated ALL charges identically. A $300 pet deposit with no `charge_to_date` on a current tenant got counted in EVERY month from `charge_from_date` to today.

**Fix applied:** Charge classification logic is now applied to revenue charts (lines ~4217-4239 in `app.py`). The app distinguishes recurring vs one-time charges:
- **Yardi:** inferred from median date span per charge code per property (median span > 60 days → recurring, ≤ 60 days → one-time)
- **Entrata:** uses the explicit `frequency` field (`One-Time` vs `Monthly`)

One-time charges are no longer spread across months in the revenue charts.

**Note:** If deposits DO have proper `charge_to_date` values (same as `charge_from_date`), this was never a problem for that data. Run the diagnostic query below to check per PMC.

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

#### 🐛 3. Insufficient Baselines Still Show Lift in Individual Graphs — ✅ FIXED

**Problem (resolved):** Properties with < 3 months of pre-launch data (or $0 in all pre months) previously displayed a +$X lift value in the per-property graphs. They didn't count toward the aggregate totals, but the UI was misleading.

**Fix applied:** The app now filters aggregates by both `baseline_reliable` AND `baseline_meaningful` (lines ~2189, ~2206, ~5781, ~5910). Properties with insufficient baselines are labeled but excluded from aggregate totals, and the lift values are properly gated.

#### 🐛 4. Date Columns Had Wrong Centuries

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

### Enhancements To Build

#### 1. Recurring vs One-Time Classification for Revenue Charts — ✅ DONE
- ~~Extend existing median-span inference to revenue chart calculations~~ ✅ Implemented — charge classification logic (lines ~4217-4239) now applied to revenue charts
- ~~Add manual override toggle per charge code~~ Still a nice-to-have but not critical
- ~~This affects lift calculations too~~ ✅ One-time deposits no longer inflate monthly averages

#### 2. Remove Lookback Slider
- ~~Currently capped at 61 months with a slider~~
- The app now fetches a full 10-year window regardless
- Slider may still exist in UI but data is not artificially capped

#### 3. Don't Take Credit for Organic Rent Increases (Andrew's point)
- If pet rent was already trending up before PetScreening launched, the lift calculation attributes all of it to PS
- Need to think about how to separate organic growth from PS-driven growth
- Possible approach: look at the trend slope pre-launch and project it forward?

#### 4. Flag "No Charge Data" Properties in Missing Pets
- Distinguish "property genuinely not charging pet rent" from "property wasn't in the data export"
- Show them but with a different indicator/grouping

### How The Lift Calculation Works (Reference)

**Updated 2026-03-16:** Changed from 3-month pre / all-post-avg to 6-and-6 windowed approach.

#### Per Property:
```
Pre-launch baseline = avg of up to 6 months before launch (uses whatever is available)
Post-launch current avg = avg of up to 6 most recent COMPLETED months (excludes current partial month)
Monthly lift = post_recent_avg − pre_avg  (the "current lift")
Total lift = sum(all post revenue) − (pre_avg × n_post_months)  (actual observed cumulative)
```

#### Aggregate:
```
Agg monthly change = SUM of each property's monthly lift (where pre_months > 0 and baseline reliable)
Agg total change = SUM of each property's total lift (where pre_months > 0 and baseline reliable)
```

#### Example:
```
Property A launched June 2024:
  Pre avg (Dec 2023–May 2024, 6mo): $1,000/mo
  Recent post avg (Sep 2025–Feb 2026, 6 completed mo): $2,100/mo
  Monthly lift: +$1,100/mo  ← reflects mature adoption, not dragged by ramp
  Post months total: 22
  Actual post revenue total: $39,600
  Total lift: $39,600 − ($1,000 × 22) = +$17,600  ← real observed dollars

Property B launched Jan 2026:
  Pre avg (Jul-Dec 2025, 6mo): $500/mo
  Recent post avg (Jan-Feb 2026, 2 completed mo): $900/mo
  Monthly lift: +$400/mo
  Post months total: 3
  Actual post revenue total: $2,700
  Total lift: $2,700 − ($500 × 3) = +$1,200

Aggregate:
  Monthly change: $1,100 + $400 = +$1,500/mo
  Total change: $17,600 + $1,200 = +$18,800
```

#### Key Detail: $0 Months Matter
The app generates $0 rows for months where a property had no pet charges (via `.get(m, 0)`). The SQL must do the same — cross join properties × months, then LEFT JOIN charges — otherwise the pre-launch baseline is inflated by excluding $0 months.

#### Window Matters
The lookback window determines which months are in scope. The app uses `timedelta(days=N*30)` which is approximate. For 61 months from March 2026: window starts ~Feb 2021, snapped to month start. Properties that launched before the window start have 0 pre months and are excluded from comparable.

### Q&A Reference (Common Questions)

#### "Are we only looking at household active?"
**Yes.** The profile query filters: `compliance_status = 'compliant'`, `user_pet_type = 'household'`, `user_pet_status = 'active'`. Same in both the app and the SQL.

#### "What about pet deposits — are those one-time $300 charges being spread across months?"
**Yes, in the revenue charts.** No recurring vs one-time distinction there. But in the missing rent estimation, Yardi charges ARE classified (median span > 60 days = recurring, ≤ 60 days = one-time). Fix: extend that logic to revenue charts.

#### "Does the app filter on charge_amount > 0 for the paying set?"
**No.** `_build_paying_sets` does `pet_rows = charges_df[charges_df['charge_code'].isin(selected_codes)]` with no amount filter. Even a $0 pet charge marks someone as "paying." The SQL had `AND charge_amount > 0` which was stricter.

#### "Are roommates handled?"
**Yes.** Unit-level expansion: if any tenant on a `(property_id, unit_code)` has a pet charge, ALL tenants on that unit are marked as paying. Edge case: if a tenant has zero rows in the charges table at all (not just no pet charges), the unit expansion can't find them — falls back to email matching only.

#### "Does insufficient baseline count in the aggregates?"
**Update (2026-03-18):** The app now filters aggregates by both `baseline_reliable` AND `baseline_meaningful`. Properties with insufficient baselines are excluded from aggregate totals. ~~The app includes everything with `n_pre > 0` — no filter on `baseline_reliable`.~~ This has been fixed.

#### "Why didn't the aggregates match at first?"
Three reasons discovered in sequence:
1. **Month spine mismatch** — SQL started from Jan 2020, app used 61-month lookback (~Feb 2021). Different windows = different pre-launch months = different baselines.
2. **Missing $0 months** — SQL only had rows where `SUM(amount) > 0`. App generates $0 rows for every month. This changes pre-launch averages.
3. **Summing only reliable vs all comparable** — SQL summed only `baseline_quality = 'reliable'`. App sums all with `pre_months > 0`. In practice for 61-month window these were the same, but conceptually different.

#### "How does the total lift work?"
Per property: Total lift = `sum(all post revenue) − (pre_avg × post_months)` (actual observed cumulative). Monthly lift = `post_recent_avg − pre_avg` using the 6-and-6 methodology (up to 6 months pre, up to 6 most recent completed months post). It answers "how much extra revenue did this property generate since launch compared to what they were doing before?" Longer time live + bigger monthly lift = bigger total. A property live for 2 years with $500/mo lift contributes way more than one launched last month with $2,000/mo lift.

#### "What about properties where pet rent was already increasing before PS?"
Not handled currently. The lift calculation attributes all post-launch increase to PetScreening. Andrew flagged this as something to address — need a way to separate organic growth from PS-driven growth.

### ✅ Resolved: Lift Calculation Methodology (2026-03-16)

**Decision:** Switched to 6-and-6 windowed approach per Andy's direction.

| Component | Old Approach | New Approach |
|-----------|-------------|--------------|
| **Pre baseline** | Last 3 months before launch | Up to 6 months before launch |
| **Post comparison (monthly lift)** | Avg of ALL post months | Up to 6 most recent completed months |
| **Total/cumulative lift** | Monthly lift × post months | Actual observed: sum(all post revenue) − (pre_avg × post months) |
| **Current month** | Included in post avg | Excluded (incomplete data) |

**Why:** Old approach had early ramp-up months dragging down the post average, and only 3 pre months made the baseline sensitive to seasonality. The 6-and-6 approach smooths seasonal noise on both sides, and using recent completed months shows the "current lift" after adoption matures.

### SQL Queries Used

All validated queries are in the companion section below:
[HQ Asset Living: Step-by-Step Validation](#hq-asset-living-step-by-step-validation)

Key tables:
- `RAW.MISC.CHARGES_HQ_ASSET_LIVING_2026_03_13` — uploaded Yardi charges
- `PROD.COMMON.D_UNITS` / `D_PROPERTIES` / `F_LEASES` — PetScreening dimensional model
- `PROD.PETSCREENING.PETSCREENING__USER_ENRICHED` — profile data
- `PROD.REPORTING.R_MONTHLY_EXECUTIVE_SUMMARY` — detailed pet-level report data

App code: `~/clawd/projects/pet-rent/app.py` (7584 lines)
Key functions: `_build_paying_sets` (line 3322), `_apply_paying_flag` (line 3474), `generate_missing_pet_rent_report` (line 3504), `fetch_missing_pet_rent_by_property` (line 3668), `compute_launch_analysis` (line ~5000)

---

## HQ Asset Living: Step-by-Step Validation

**Table:** `RAW.MISC.CHARGES_HQ_ASSET_LIVING_2026_03_13`
**Created:** 2026-03-13

### Step 0: Fix the Raw Table

The date columns are DATE type but have wrong centuries (`0025` instead of `2025`).
Run this once to fix:

```sql
-- Check current state first
SELECT launch_date, lease_from, lease_to, move_in, move_out, charge_from_date, charge_to_date
FROM "RAW"."MISC"."CHARGES_HQ_ASSET_LIVING_2026_03_13"
LIMIT 5;

-- Fix all date columns: add 2000 years
UPDATE "RAW"."MISC"."CHARGES_HQ_ASSET_LIVING_2026_03_13"
SET
    launch_date      = DATEADD(YEAR, 2000, launch_date),
    lease_from       = DATEADD(YEAR, 2000, lease_from),
    lease_to         = DATEADD(YEAR, 2000, lease_to),
    move_in          = DATEADD(YEAR, 2000, move_in),
    move_out         = DATEADD(YEAR, 2000, move_out),
    charge_from_date = DATEADD(YEAR, 2000, charge_from_date),
    charge_to_date   = DATEADD(YEAR, 2000, charge_to_date)
WHERE launch_date < '1000-01-01'
   OR lease_from < '1000-01-01'
   OR charge_from_date < '1000-01-01';

-- Verify fix
SELECT launch_date, lease_from, charge_from_date, charge_to_date
FROM "RAW"."MISC"."CHARGES_HQ_ASSET_LIVING_2026_03_13"
LIMIT 5;
```

### Step 1: Find Pet Charge Codes

```sql
-- What pet-related charge codes exist in this data?
SELECT
    charge_code,
    COUNT(*) AS cnt,
    MIN(charge_amount) AS min_amt,
    MAX(charge_amount) AS max_amt,
    AVG(charge_amount) AS avg_amt
FROM "RAW"."MISC"."CHARGES_HQ_ASSET_LIVING_2026_03_13"
WHERE LOWER(charge_code) ILIKE ANY ('%pet%', '%animal%')
GROUP BY 1
ORDER BY 2 DESC;
```

### Step 2: Monthly Pet Fee Revenue (Portfolio Level)

```sql
WITH pet_codes AS (
    -- Auto-detect pet charge codes from the data
    SELECT DISTINCT charge_code
    FROM "RAW"."MISC"."CHARGES_HQ_ASSET_LIVING_2026_03_13"
    WHERE LOWER(charge_code) ILIKE ANY ('%pet%', '%animal%')
),

parsed_charges AS (
    SELECT
        a.property_id,
        a.property_name,
        a.tenant_code,
        a.email,
        a.charge_code,
        a.tenant_status,
        a.charge_amount::DOUBLE                         AS amount,
        DATE_TRUNC('MONTH', a.charge_from_date)         AS from_month,
        a.charge_to_date                                AS raw_to_date,
        a.move_out                                      AS move_out_dt,
        a.lease_to                                      AS lease_to_dt,
        a.launch_date,
        -- Effective end date logic:
        --   1. Explicit charge end date → use it
        --   2. Current tenant → NULL (means still active, use current month)
        --   3. Past tenant → use move_out, then lease_to
        COALESCE(
            a.charge_to_date,
            CASE
                WHEN LOWER(TRIM(a.tenant_status)) = 'current' THEN NULL
                ELSE COALESCE(a.move_out, a.lease_to)
            END
        )                                               AS effective_to_date
    FROM "RAW"."MISC"."CHARGES_HQ_ASSET_LIVING_2026_03_13" a
    INNER JOIN pet_codes pc
        ON a.charge_code = pc.charge_code
    WHERE a.charge_amount > 0
      AND a.charge_from_date IS NOT NULL
),

month_spine AS (
    SELECT DATEADD(MONTH, seq, '2020-01-01'::DATE) AS month_start
    FROM (
        SELECT SEQ4() AS seq
        FROM TABLE(GENERATOR(ROWCOUNT => 120))
    )
    WHERE DATEADD(MONTH, seq, '2020-01-01'::DATE) <= DATE_TRUNC('MONTH', CURRENT_DATE())
),

charge_months AS (
    SELECT
        pc.property_id,
        pc.property_name,
        pc.tenant_code,
        pc.amount,
        pc.launch_date,
        ms.month_start
    FROM parsed_charges pc
    CROSS JOIN month_spine ms
    WHERE ms.month_start >= DATE_TRUNC('MONTH', pc.from_month)
      AND ms.month_start <= DATE_TRUNC('MONTH', COALESCE(pc.effective_to_date, CURRENT_DATE()))
)

-- Portfolio-level monthly revenue
SELECT
    month_start,
    SUM(amount)  AS monthly_revenue,
    COUNT(*)     AS charge_lines_active
FROM charge_months
GROUP BY 1
ORDER BY 1;
```

### Step 3: Monthly Revenue by Property

```sql
WITH pet_codes AS (
    SELECT DISTINCT charge_code
    FROM "RAW"."MISC"."CHARGES_HQ_ASSET_LIVING_2026_03_13"
    WHERE LOWER(charge_code) ILIKE ANY ('%pet%', '%animal%')
),

parsed_charges AS (
    SELECT
        a.property_id,
        a.property_name,
        a.tenant_code,
        a.email,
        a.charge_code,
        a.tenant_status,
        a.charge_amount::DOUBLE   AS amount,
        a.charge_from_date        AS from_date,
        a.launch_date,
        COALESCE(
            a.charge_to_date,
            CASE
                WHEN LOWER(TRIM(a.tenant_status)) = 'current' THEN NULL
                ELSE COALESCE(a.move_out, a.lease_to)
            END
        ) AS effective_to_date
    FROM "RAW"."MISC"."CHARGES_HQ_ASSET_LIVING_2026_03_13" a
    INNER JOIN pet_codes pc ON a.charge_code = pc.charge_code
    WHERE a.charge_amount > 0
      AND a.charge_from_date IS NOT NULL
),

month_spine AS (
    SELECT DATEADD(MONTH, seq, '2020-01-01'::DATE) AS month_start
    FROM (SELECT SEQ4() AS seq FROM TABLE(GENERATOR(ROWCOUNT => 120)))
    WHERE DATEADD(MONTH, seq, '2020-01-01'::DATE) <= DATE_TRUNC('MONTH', CURRENT_DATE())
),

charge_months AS (
    SELECT
        pc.property_id,
        pc.property_name,
        pc.amount,
        pc.launch_date,
        ms.month_start
    FROM parsed_charges pc
    CROSS JOIN month_spine ms
    WHERE ms.month_start >= DATE_TRUNC('MONTH', pc.from_date)
      AND ms.month_start <= DATE_TRUNC('MONTH', COALESCE(pc.effective_to_date, CURRENT_DATE()))
)

SELECT
    property_name,
    month_start,
    SUM(amount)  AS monthly_revenue,
    COUNT(*)     AS charge_lines_active
FROM charge_months
GROUP BY 1, 2
ORDER BY 1, 2;
```

### Step 4: Pre/Post PetScreening Launch Analysis (6-and-6 Methodology)

**Updated 2026-03-16:** Uses up to 6 months pre, up to 6 most recent completed months post.
Monthly lift = recent post avg − pre avg. Total lift = actual observed (sum post revenue − pre avg × post months).

**Full query with all fixes is in `sql/lift_analysis_asset_living_cleaned.sql`** — includes one-time charge handling, property_id joins, non-current tenant null date handling, and baseline_meaningful flag.

The simplified version below is kept for reference but see the .sql file for the production version.

```sql
WITH pet_codes AS (
    SELECT DISTINCT charge_code
    FROM "RAW"."MISC"."CHARGES_HQ_ASSET_LIVING_2026_03_13"
    WHERE LOWER(charge_code) ILIKE ANY ('%pet%', '%animal%')
),

parsed_charges AS (
    SELECT
        a.property_id,
        a.property_name,
        a.charge_amount::DOUBLE   AS amount,
        a.charge_from_date        AS from_date,
        a.launch_date,
        a.tenant_status,
        COALESCE(
            a.charge_to_date,
            CASE
                WHEN LOWER(TRIM(a.tenant_status)) = 'current' THEN NULL
                ELSE COALESCE(a.move_out, a.lease_to)
            END
        ) AS effective_to_date
    FROM "RAW"."MISC"."CHARGES_HQ_ASSET_LIVING_2026_03_13" a
    INNER JOIN pet_codes pc ON a.charge_code = pc.charge_code
    WHERE a.charge_amount > 0
      AND a.charge_from_date IS NOT NULL
),

month_spine AS (
    SELECT DATEADD(MONTH, seq, '2020-01-01'::DATE) AS month_start
    FROM (SELECT SEQ4() AS seq FROM TABLE(GENERATOR(ROWCOUNT => 120)))
    WHERE DATEADD(MONTH, seq, '2020-01-01'::DATE) <= DATE_TRUNC('MONTH', CURRENT_DATE())
),

charge_months AS (
    SELECT
        pc.property_id,
        pc.property_name,
        pc.amount,
        pc.launch_date,
        ms.month_start
    FROM parsed_charges pc
    CROSS JOIN month_spine ms
    WHERE ms.month_start >= DATE_TRUNC('MONTH', pc.from_date)
      AND ms.month_start <= DATE_TRUNC('MONTH', COALESCE(pc.effective_to_date, CURRENT_DATE()))
),

monthly_by_prop AS (
    SELECT
        property_name,
        launch_date,
        month_start,
        SUM(amount) AS monthly_revenue
    FROM charge_months
    GROUP BY 1, 2, 3
),

-- Pre/post classification
classified AS (
    SELECT
        property_name,
        launch_date,
        month_start,
        monthly_revenue,
        CASE
            WHEN launch_date IS NULL THEN 'no_launch'
            WHEN month_start < DATE_TRUNC('MONTH', launch_date) THEN 'pre'
            ELSE 'post'
        END AS period
    FROM monthly_by_prop
),

-- Pre-launch baseline: up to 6 months before launch
pre_baseline AS (
    SELECT
        property_name,
        AVG(monthly_revenue) AS pre_avg,
        COUNT(*)             AS pre_months
    FROM (
        SELECT *,
            ROW_NUMBER() OVER (
                PARTITION BY property_name
                ORDER BY month_start DESC
            ) AS rn
        FROM classified
        WHERE period = 'pre'
    )
    WHERE rn <= 6  -- up to 6 months pre (was 3)
    GROUP BY 1
),

-- Post-launch: all months (for total observed revenue)
post_all AS (
    SELECT
        property_name,
        launch_date,
        SUM(monthly_revenue)  AS post_total,
        COUNT(*)              AS post_months
    FROM classified
    WHERE period = 'post'
    GROUP BY 1, 2
),

-- Post-launch recent: up to 6 most recent COMPLETED months (excludes current month)
post_recent AS (
    SELECT
        property_name,
        AVG(monthly_revenue)  AS post_recent_avg,
        COUNT(*)              AS recent_post_months
    FROM (
        SELECT *,
            ROW_NUMBER() OVER (
                PARTITION BY property_name
                ORDER BY month_start DESC
            ) AS rn
        FROM classified
        WHERE period = 'post'
          AND month_start < DATE_TRUNC('MONTH', CURRENT_DATE())  -- exclude current partial month
    )
    WHERE rn <= 6  -- up to 6 most recent completed months
    GROUP BY 1
)

SELECT
    pa.property_name,
    pa.launch_date,
    COALESCE(b.pre_months, 0)                                       AS pre_months,
    COALESCE(pr.recent_post_months, 0)                               AS recent_post_months,
    pa.post_months                                                   AS total_post_months,
    ROUND(COALESCE(b.pre_avg, 0), 2)                                AS pre_avg_monthly,
    ROUND(COALESCE(pr.post_recent_avg, 0), 2)                       AS current_avg_monthly,
    ROUND(COALESCE(pr.post_recent_avg, 0) - COALESCE(b.pre_avg, 0), 2) AS monthly_lift,
    ROUND(pa.post_total - (COALESCE(b.pre_avg, 0) * pa.post_months), 2) AS cumulative_impact,
    CASE WHEN COALESCE(b.pre_months, 0) >= 3 THEN 'reliable' ELSE 'insufficient' END AS baseline_quality
FROM post_all pa
LEFT JOIN pre_baseline b ON pa.property_name = b.property_name
LEFT JOIN post_recent pr ON pa.property_name = pr.property_name
ORDER BY monthly_lift DESC;
```

### Step 5: Missing Pet Rent (Confirmed Profiles Not Paying)

This joins the uploaded charge data against PetScreening profiles in Snowflake.

```sql
WITH pet_codes AS (
    SELECT DISTINCT charge_code
    FROM "RAW"."MISC"."CHARGES_HQ_ASSET_LIVING_2026_03_13"
    WHERE LOWER(charge_code) ILIKE ANY ('%pet%', '%animal%')
),

-- Build paying tenant set (same logic as app.py)
direct_payers AS (
    SELECT DISTINCT
        TRIM(CAST(property_id AS VARCHAR))          AS property_id,
        UPPER(TRIM(CAST(tenant_code AS VARCHAR)))   AS tenant_code,
        LOWER(TRIM(CAST(email AS VARCHAR)))          AS email,
        UPPER(TRIM(CAST(unit_code AS VARCHAR)))     AS unit_code
    FROM "RAW"."MISC"."CHARGES_HQ_ASSET_LIVING_2026_03_13"
    WHERE charge_code IN (SELECT charge_code FROM pet_codes)
      AND charge_amount > 0
),

paying_units AS (
    SELECT DISTINCT property_id, unit_code
    FROM direct_payers
    WHERE unit_code IS NOT NULL
      AND unit_code NOT IN ('', 'NAN', 'NONE')
),

-- Expand: all tenants on a paying unit count as paying
unit_expanded AS (
    SELECT DISTINCT
        TRIM(CAST(a.property_id AS VARCHAR))          AS property_id,
        UPPER(TRIM(CAST(a.tenant_code AS VARCHAR)))   AS tenant_code,
        LOWER(TRIM(CAST(a.email AS VARCHAR)))          AS email
    FROM "RAW"."MISC"."CHARGES_HQ_ASSET_LIVING_2026_03_13" a
    INNER JOIN paying_units pu
        ON TRIM(CAST(a.property_id AS VARCHAR)) = pu.property_id
       AND UPPER(TRIM(CAST(a.unit_code AS VARCHAR))) = pu.unit_code
),

all_paying AS (
    SELECT property_id, tenant_code, email FROM direct_payers
    UNION
    SELECT property_id, tenant_code, email FROM unit_expanded
),

paying_by_tc AS (
    SELECT DISTINCT property_id, tenant_code
    FROM all_paying
    WHERE tenant_code IS NOT NULL AND tenant_code NOT IN ('', 'NAN', 'NONE')
),

paying_by_email AS (
    SELECT DISTINCT property_id, email
    FROM all_paying
    WHERE email IS NOT NULL AND email NOT IN ('', 'nan', 'none')
),

-- PetScreening profiles with active household pets
ps_profiles AS (
    SELECT DISTINCT
        du.property_id,
        p.property_name,
        COALESCE(
            l.lease_source_external_id:tenant_code::STRING,
            l.lease_source_external_id:"customerId"::STRING,
            l.lease_source_external_id:"customer_id"::STRING
        ) AS tenant_code,
        ue.user_email,
        ue.user_first_name,
        ue.user_last_name,
        ue.compliance_status,
        ue.user_pet_type,
        ue.user_pet_status,
        ue.user_profile_url
    FROM PROD.COMMON.D_UNITS du
    JOIN PROD.COMMON.D_PROPERTIES p ON du.property_id = p.property_id
    JOIN PROD.PETSCREENING.PETSCREENING__USER_ENRICHED ue ON ue.unit_id = du.unit_id
    JOIN PROD.COMMON.F_LEASES l ON du.unit_key = l.unit_key AND l.user_key = ue.user_key
    WHERE du.unit_source = 'yardi'
      AND du.property_id IN (
          SELECT DISTINCT property_id
          FROM "RAW"."MISC"."CHARGES_HQ_ASSET_LIVING_2026_03_13"
      )
      AND ue.compliance_status = 'compliant'
      AND ue.user_pet_type = 'household'
      AND ue.user_pet_status = 'active'
      AND ue.user_email IS NOT NULL
      AND TRIM(ue.user_email) <> ''
      AND LOWER(TRIM(ue.user_email)) NOT IN (
          'none@none.com', 'noemail@noemail.com', 'none@nowhere.com',
          'none@gmail.com', 'noemail@gmail.com', 'na@na.com',
          'no@email.com', 'na@gmail.com', 'noemail@greystar.com',
          'no@no.com', 'n/a@gmail.com', 'none@aol.com',
          'none@embreydc.com', 'noemail@comcapp.com'
      )
),

-- Match profiles to paying set
profiles_flagged AS (
    SELECT
        prof.*,
        CASE
            WHEN ptc.tenant_code IS NOT NULL THEN 1
            WHEN pte.email IS NOT NULL THEN 1
            ELSE 0
        END AS pet_rent_paid_raw
    FROM ps_profiles prof
    LEFT JOIN paying_by_tc ptc
        ON TRIM(CAST(prof.property_id AS VARCHAR)) = ptc.property_id
       AND UPPER(TRIM(CAST(prof.tenant_code AS VARCHAR))) = ptc.tenant_code
    LEFT JOIN paying_by_email pte
        ON TRIM(CAST(prof.property_id AS VARCHAR)) = pte.property_id
       AND LOWER(TRIM(prof.user_email)) = pte.email
),

-- Cross-lease propagation: if ANY row for (property, email) is paying, ALL are
user_level AS (
    SELECT
        property_id,
        LOWER(TRIM(user_email)) AS user_email,
        MAX(pet_rent_paid_raw)  AS is_paying
    FROM profiles_flagged
    GROUP BY 1, 2
),

final_flagged AS (
    SELECT
        pf.*,
        COALESCE(ul.is_paying, pf.pet_rent_paid_raw) AS pet_rent_paid
    FROM profiles_flagged pf
    LEFT JOIN user_level ul
        ON pf.property_id = ul.property_id
       AND LOWER(TRIM(pf.user_email)) = ul.user_email
)

-- Missing pet rent: has pet profile, NOT paying
SELECT
    property_name,
    user_first_name,
    user_last_name,
    user_email,
    tenant_code,
    user_profile_url,
    pet_rent_paid
FROM final_flagged
WHERE pet_rent_paid = 0
ORDER BY property_name, user_last_name;
```

### Step 6: Summary — Missing Count by Property

```sql
-- Uses the same CTEs as Step 5, just different final SELECT
-- (copy CTEs from Step 5, then replace the final SELECT with:)

SELECT
    property_name,
    COUNT(DISTINCT user_email) AS missing_tenants,
    SUM(CASE WHEN pet_rent_paid = 1 THEN 1 ELSE 0 END) AS paying_tenants
FROM final_flagged
GROUP BY 1
ORDER BY missing_tenants DESC;
```

### Quick Stats

```sql
-- Row count and date ranges
SELECT
    COUNT(*)                    AS total_rows,
    COUNT(DISTINCT property_id) AS properties,
    COUNT(DISTINCT tenant_code) AS tenants,
    MIN(charge_from_date)       AS earliest_charge,
    MAX(charge_from_date)       AS latest_charge,
    MIN(launch_date)            AS earliest_launch,
    MAX(launch_date)            AS latest_launch
FROM "RAW"."MISC"."CHARGES_HQ_ASSET_LIVING_2026_03_13";
```
