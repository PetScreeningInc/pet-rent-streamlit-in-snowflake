# Pet Rent Data Validation Walkthrough

> **Purpose:** Step-by-step SQL guide for validating the pet rent analysis numbers using
> the `all_charges.csv` export from the app. Every query uses only CTEs and joins — no temp
> tables. Comments explain *why* each filter and decision exists.
>
> **Goal:** Recreate the three tranches your boss outlined and verify the counts independently.

---

## Step 0: Load the CSV into Snowflake

Upload `all_charges.csv` via Snowsight (drag-and-drop into a table) or use a stage:

```sql
-- Create a home for validation work
CREATE SCHEMA IF NOT EXISTS sandbox.pet_rent_validation;

-- Option A: Snowsight UI — just drag the CSV into a new table

-- Option B: Stage + COPY INTO
CREATE OR REPLACE FILE FORMAT sandbox.pet_rent_validation.csv_fmt
    TYPE = 'CSV'
    FIELD_OPTIONALLY_ENCLOSED_BY = '"'
    SKIP_HEADER = 1
    NULL_IF = ('', 'None', 'NaN');

CREATE OR REPLACE TABLE sandbox.pet_rent_validation.api_charges (
    parent_company    VARCHAR,
    property_id       VARCHAR,
    property_name     VARCHAR,
    property_code     VARCHAR,
    launch_date       VARCHAR,
    unit_code         VARCHAR,
    unit_type         VARCHAR,
    market_rent       VARCHAR,
    tenant_code       VARCHAR,
    first_name        VARCHAR,
    last_name         VARCHAR,
    tenant_status     VARCHAR,
    lease_from        VARCHAR,
    lease_to          VARCHAR,
    move_in           VARCHAR,
    move_out          VARCHAR,
    email             VARCHAR,
    charge_code       VARCHAR,
    charge_type       VARCHAR,
    charge_amount     VARCHAR,
    charge_from_date  VARCHAR,
    charge_to_date    VARCHAR
);

-- Upload and load
-- PUT file:///path/to/all_charges_yardi_*.csv @sandbox.pet_rent_validation.%api_charges;
-- COPY INTO sandbox.pet_rent_validation.api_charges FILE_FORMAT = sandbox.pet_rent_validation.csv_fmt;
```

Once loaded, sanity check:

```sql
SELECT COUNT(*) AS total_rows,
       COUNT(DISTINCT property_name) AS properties,
       COUNT(DISTINCT tenant_code) AS tenants
FROM sandbox.pet_rent_validation.api_charges;
```

---

## Step 1: Understand the data shape

Before validating numbers, make sure the raw data looks right.

```sql
-- What charge codes exist and how many lines per code?
-- This should match the "Charge Code Summary" table in the app.
SELECT charge_code,
       COUNT(*) AS charge_lines,
       SUM(TRY_TO_DOUBLE(charge_amount)) AS total_amount
FROM sandbox.pet_rent_validation.api_charges
GROUP BY 1
ORDER BY 2 DESC;
```

```sql
-- Which codes are pet-related?
-- Replace this list with whatever you selected in the app.
-- The app auto-selects codes containing 'pet', 'animal', 'petnr', 'concpet'.
SELECT charge_code,
       COUNT(*) AS charge_lines,
       SUM(TRY_TO_DOUBLE(charge_amount)) AS total_amount
FROM sandbox.pet_rent_validation.api_charges
WHERE LOWER(charge_code) ILIKE ANY ('%pet%', '%animal%')
GROUP BY 1
ORDER BY 2 DESC;
```

> **Action:** Note down the exact charge codes you selected in the app. You'll use
> them as the `pet_codes` filter in every query below. I'll use a CTE so you only
> define them once.

---

## Step 2: Monthly revenue time series (the bar chart)

This recreates the portfolio-level monthly revenue chart. The app takes each charge
line's `charge_from_date` through `charge_to_date` and counts it in every month that
falls within that range.

```sql
-- ============================================================================
-- MONTHLY PET REVENUE — Recreates the blue/green bar chart
-- ============================================================================
--
-- HOW IT WORKS:
-- Each charge line has a from_date and to_date. A $25/mo Pet Rent charge from
-- 2024-01-01 to 2025-12-31 contributes $25 to every month in that range.
-- We generate a row per month per charge using a date spine, then sum.
--
-- WHY effective_to_date coalesces to move_out/lease_to:
-- Some charges (especially in Yardi) have no explicit end date. For tenants
-- who have moved out, we use their move_out or lease_to date so their charges
-- don't count in months after they left. For current tenants with no end date,
-- we treat the charge as ongoing through the current month.
-- ============================================================================

WITH pet_codes AS (
    -- *** EDIT THIS LIST to match what you selected in the app ***
    SELECT column1 AS charge_code
    FROM VALUES ('PetRent'), ('Pet Rent'), ('PET RENT'), ('Pet Fee')
    -- Add/remove codes as needed
),

-- Parse the raw charge data and compute effective end dates
parsed_charges AS (
    SELECT
        a.property_id,
        a.property_name,
        a.tenant_code,
        a.email,
        a.charge_code,
        a.tenant_status,
        TRY_TO_DOUBLE(a.charge_amount) AS amount,
        TRY_TO_DATE(a.charge_from_date, 'YYYY-MM-DD') AS from_date,
        TRY_TO_DATE(a.charge_to_date, 'YYYY-MM-DD') AS raw_to_date,
        TRY_TO_DATE(a.move_out, 'YYYY-MM-DD') AS move_out_dt,
        TRY_TO_DATE(a.lease_to, 'YYYY-MM-DD') AS lease_to_dt,
        TRY_TO_DATE(a.launch_date, 'YYYY-MM-DD') AS launch_date,

        -- Effective end date logic:
        -- 1. If the charge has an explicit end date, use it
        -- 2. If the tenant is Current (still living there), treat as ongoing (NULL = current month)
        -- 3. If Past/Notice/etc, coalesce move_out → lease_to so we stop counting after they left
        COALESCE(
            TRY_TO_DATE(a.charge_to_date, 'YYYY-MM-DD'),
            CASE
                WHEN LOWER(TRIM(a.tenant_status)) = 'current' THEN NULL
                ELSE COALESCE(
                    TRY_TO_DATE(a.move_out, 'YYYY-MM-DD'),
                    TRY_TO_DATE(a.lease_to, 'YYYY-MM-DD')
                )
            END
        ) AS effective_to_date

    FROM sandbox.pet_rent_validation.api_charges a
    INNER JOIN pet_codes pc ON a.charge_code = pc.charge_code
    WHERE TRY_TO_DOUBLE(a.charge_amount) > 0
      AND TRY_TO_DATE(a.charge_from_date, 'YYYY-MM-DD') IS NOT NULL
),

-- Date spine: generate one row per month in the analysis window
-- Adjust the range to match your lookback setting in the app
month_spine AS (
    SELECT DATEADD(MONTH, seq, '2020-01-01'::DATE) AS month_start
    FROM TABLE(GENERATOR(ROWCOUNT => 120))
    LET seq = SEQ4()
    WHERE DATEADD(MONTH, seq, '2020-01-01'::DATE) <= DATE_TRUNC('MONTH', CURRENT_DATE())
),

-- Expand each charge into the months it was active
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
    WHERE ms.month_start >= DATE_TRUNC('MONTH', pc.from_date)
      AND ms.month_start <= DATE_TRUNC('MONTH', COALESCE(pc.effective_to_date, CURRENT_DATE()))
)

-- Portfolio-level monthly revenue
SELECT
    month_start,
    SUM(amount) AS monthly_revenue,
    COUNT(*) AS charge_lines_active
FROM charge_months
GROUP BY 1
ORDER BY 1;
```

> **Check:** Compare each month's `monthly_revenue` to the bar heights in the app's chart.
> They should match exactly.

---

## Step 3: Tranche 1 — Revenue change since PetScreening launch

This recreates the "Total Revenue Change" and "Monthly Revenue Change" KPIs.

```sql
-- ============================================================================
-- TRANCHE 1: VALUE ALREADY CREATED
-- ============================================================================
--
-- For each property with a launch date, we compare:
--   - Pre-launch baseline: average of the LAST 3 MONTHS before launch
--   - Post-launch average: total post-launch revenue ÷ number of post months
--   - Monthly change: post avg - pre avg
--   - Total change: monthly change × number of post months
--
-- WHY last 3 months (not all pre-launch)?
-- Using all pre-launch months would dilute the baseline. If a property had
-- very low pet rent 2 years ago and gradually increased, the full average
-- would be artificially low, making the "change" look bigger than reality.
-- The last 3 months give a "business as usual" baseline right before launch.
--
-- WHY launch month counts as post-launch?
-- PetScreening was active for at least part of that month, so any revenue
-- lift in the launch month is partially attributable to the program.
-- ============================================================================

WITH pet_codes AS (
    SELECT column1 AS charge_code
    FROM VALUES ('PetRent'), ('Pet Rent'), ('PET RENT'), ('Pet Fee')
),

parsed_charges AS (
    SELECT
        a.property_name,
        TRY_TO_DOUBLE(a.charge_amount) AS amount,
        TRY_TO_DATE(a.charge_from_date, 'YYYY-MM-DD') AS from_date,
        COALESCE(
            TRY_TO_DATE(a.charge_to_date, 'YYYY-MM-DD'),
            CASE
                WHEN LOWER(TRIM(a.tenant_status)) = 'current' THEN NULL
                ELSE COALESCE(
                    TRY_TO_DATE(a.move_out, 'YYYY-MM-DD'),
                    TRY_TO_DATE(a.lease_to, 'YYYY-MM-DD')
                )
            END
        ) AS effective_to_date,
        TRY_TO_DATE(a.launch_date, 'YYYY-MM-DD') AS launch_date
    FROM sandbox.pet_rent_validation.api_charges a
    INNER JOIN pet_codes pc ON a.charge_code = pc.charge_code
    WHERE TRY_TO_DOUBLE(a.charge_amount) > 0
      AND TRY_TO_DATE(a.charge_from_date, 'YYYY-MM-DD') IS NOT NULL
),

-- Get one launch date per property
property_launches AS (
    SELECT DISTINCT
        property_name,
        launch_date
    FROM parsed_charges
    WHERE launch_date IS NOT NULL
),

month_spine AS (
    SELECT DATEADD(MONTH, seq, '2020-01-01'::DATE) AS month_start
    FROM TABLE(GENERATOR(ROWCOUNT => 120))
    LET seq = SEQ4()
    WHERE DATEADD(MONTH, seq, '2020-01-01'::DATE) <= DATE_TRUNC('MONTH', CURRENT_DATE())
),

-- Monthly revenue per property
monthly_by_prop AS (
    SELECT
        pc.property_name,
        ms.month_start,
        SUM(pc.amount) AS monthly_rev
    FROM parsed_charges pc
    CROSS JOIN month_spine ms
    WHERE ms.month_start >= DATE_TRUNC('MONTH', pc.from_date)
      AND ms.month_start <= DATE_TRUNC('MONTH', COALESCE(pc.effective_to_date, CURRENT_DATE()))
    GROUP BY 1, 2
),

-- Tag each month as pre or post launch
monthly_tagged AS (
    SELECT
        m.property_name,
        m.month_start,
        m.monthly_rev,
        pl.launch_date,
        DATE_TRUNC('MONTH', pl.launch_date) AS launch_month,
        CASE
            WHEN m.month_start >= DATE_TRUNC('MONTH', pl.launch_date) THEN 'post'
            ELSE 'pre'
        END AS period
    FROM monthly_by_prop m
    INNER JOIN property_launches pl ON m.property_name = pl.property_name
),

-- Pre-launch: rank months descending so we can take the last 3 before launch
pre_ranked AS (
    SELECT
        property_name,
        month_start,
        monthly_rev,
        ROW_NUMBER() OVER (
            PARTITION BY property_name
            ORDER BY month_start DESC
        ) AS rn
    FROM monthly_tagged
    WHERE period = 'pre'
),

-- Pre-launch baseline: average of last 3 months before launch
pre_baseline AS (
    SELECT
        property_name,
        AVG(monthly_rev) AS pre_avg,
        COUNT(*) AS pre_months_used
    FROM pre_ranked
    WHERE rn <= 3
    GROUP BY 1
),

-- Post-launch stats
post_stats AS (
    SELECT
        property_name,
        SUM(monthly_rev) AS post_total,
        COUNT(*) AS post_months,
        AVG(monthly_rev) AS post_avg
    FROM monthly_tagged
    WHERE period = 'post'
    GROUP BY 1
),

-- Combine
launch_analysis AS (
    SELECT
        ps.property_name,
        COALESCE(pb.pre_avg, 0) AS pre_avg_monthly,
        ps.post_avg AS post_avg_monthly,
        ps.post_avg - COALESCE(pb.pre_avg, 0) AS monthly_change,
        (ps.post_avg - COALESCE(pb.pre_avg, 0)) * ps.post_months AS total_change,
        ps.post_months,
        COALESCE(pb.pre_months_used, 0) AS pre_months_used
    FROM post_stats ps
    LEFT JOIN pre_baseline pb ON ps.property_name = pb.property_name
)

-- Per-property breakdown
SELECT * FROM launch_analysis
ORDER BY total_change DESC;

-- To get the portfolio-level KPIs (the numbers your boss cited):
-- Only properties with pre-launch data are "comparable"
-- SELECT
--     SUM(monthly_change) AS portfolio_monthly_change,     -- "Monthly Revenue Change"
--     SUM(total_change) AS portfolio_total_change           -- "Total Revenue Change"
-- FROM launch_analysis
-- WHERE pre_months_used > 0;
```

> **Check:** The `portfolio_monthly_change` should match the "$72,933/month" your boss
> cited. The `portfolio_total_change` should match "$2,072,775".

---

## Step 4: Tranche 2 — Missing pet rent (who has a pet but isn't paying)

This is the most important validation. It recreates the "2,569 tenants" and "$76,482/month" numbers.

### Step 4a: Build the paying tenant set from API data

```sql
-- ============================================================================
-- PAYING TENANT IDENTIFICATION
-- ============================================================================
--
-- WHO COUNTS AS "PAYING"?
-- A tenant is paying pet rent if ANY of these are true:
--   1. They directly have a pet charge on their lease
--   2. They live on a unit where ANY tenant has a pet charge (roommate rule)
--
-- WHY the roommate/unit expansion?
-- In multifamily housing, pet rent is often charged to one person on a unit
-- even though multiple people live there. If John and Jane share Unit 101 and
-- the pet rent is on John's lease, Jane should NOT show up as "missing pet
-- rent" — the unit is already paying. The charge covers the unit, not the
-- individual.
--
-- WHY both tenant_code AND email matching?
-- PetScreening stores a tenant_code extracted from the lease JSON in Snowflake
-- (f_leases.lease_source_external_id:tenant_code). This should match the
-- TenantCode from the Yardi API. But sometimes codes change on renewal, or
-- are stored differently. Email is the fallback — if the tenant_code doesn't
-- match but the email does, we still count them as paying.
--
-- WHY case-insensitive + trimmed?
-- Yardi and PetScreening may store "t0012345" vs "T0012345" or have trailing
-- spaces. Normalizing prevents false "missing" flags from formatting differences.
-- ============================================================================

WITH pet_codes AS (
    SELECT column1 AS charge_code
    FROM VALUES ('PetRent'), ('Pet Rent'), ('PET RENT'), ('Pet Fee')
),

-- Step 1: Find tenants who directly have a pet charge
direct_payers AS (
    SELECT DISTINCT
        TRIM(property_id) AS property_id,
        UPPER(TRIM(tenant_code)) AS tenant_code,
        LOWER(TRIM(email)) AS email,
        UPPER(TRIM(unit_code)) AS unit_code
    FROM sandbox.pet_rent_validation.api_charges
    WHERE charge_code IN (SELECT charge_code FROM pet_codes)
),

-- Step 2: Find all units that have at least one pet charge
paying_units AS (
    SELECT DISTINCT
        property_id,
        unit_code
    FROM direct_payers
    WHERE unit_code IS NOT NULL
      AND unit_code NOT IN ('', 'NAN', 'NONE')
),

-- Step 3: Expand — find ALL tenants on paying units (the roommate rule)
unit_expanded AS (
    SELECT DISTINCT
        TRIM(a.property_id) AS property_id,
        UPPER(TRIM(a.tenant_code)) AS tenant_code,
        LOWER(TRIM(a.email)) AS email
    FROM sandbox.pet_rent_validation.api_charges a
    INNER JOIN paying_units pu
        ON TRIM(a.property_id) = pu.property_id
       AND UPPER(TRIM(a.unit_code)) = pu.unit_code
),

-- Step 4: Union direct payers + unit-expanded tenants
all_paying AS (
    SELECT property_id, tenant_code, email FROM direct_payers
    UNION
    SELECT property_id, tenant_code, email FROM unit_expanded
),

-- Deduplicated lookup sets
paying_by_tc AS (
    SELECT DISTINCT property_id, tenant_code
    FROM all_paying
    WHERE tenant_code IS NOT NULL
      AND tenant_code NOT IN ('', 'NAN', 'NONE')
),

paying_by_email AS (
    SELECT DISTINCT property_id, email
    FROM all_paying
    WHERE email IS NOT NULL
      AND email NOT IN ('', 'nan', 'none')
)

-- Verify: how many unique paying tenants do we have?
SELECT
    'by_tenant_code' AS match_type, COUNT(*) AS cnt FROM paying_by_tc
UNION ALL
SELECT
    'by_email' AS match_type, COUNT(*) AS cnt FROM paying_by_email;
```

### Step 4b: Pull PetScreening profiles and join to paying set

```sql
-- ============================================================================
-- MISSING PET RENT — THE FULL JOIN
-- ============================================================================
--
-- This query:
--   1. Pulls PetScreening profiles from Snowflake (compliant, household, active)
--   2. Extracts tenant_code from the lease JSON (the join key back to API data)
--   3. Left-joins to the paying set from the API data
--   4. Propagates the paying flag across all leases for the same person
--   5. Filters to pet_rent_paid = 0 → these are "missing pet rent"
--
-- WHY compliant + household + active?
-- - compliant: the resident has completed their screening (not draft/pending)
-- - household: this is a regular pet, not an assistance/ESA animal
-- - active: the pet profile is current, not archived or expired
-- These three filters together mean "this person has gone through PetScreening,
-- declared a household pet, and that pet profile is currently active."
--
-- WHY the junk email exclusion?
-- Some PMCs use placeholder emails like none@none.com or noemail@gmail.com
-- for residents who don't provide an email. These would cause false matches
-- (multiple residents matching on the same fake email). We exclude them.
--
-- WHY cross-lease propagation?
-- A resident can have multiple rows in f_leases (renewals, transfers). Their
-- tenant_code may differ between leases. If lease A matches as paying but
-- lease B doesn't, without propagation we'd falsely flag lease B as "missing."
-- So if ANY lease for the same (property_id, email) is paying, ALL are.
-- ============================================================================

WITH pet_codes AS (
    SELECT column1 AS charge_code
    FROM VALUES ('PetRent'), ('Pet Rent'), ('PET RENT'), ('Pet Fee')
),

direct_payers AS (
    SELECT DISTINCT
        TRIM(property_id) AS property_id,
        UPPER(TRIM(tenant_code)) AS tenant_code,
        LOWER(TRIM(email)) AS email,
        UPPER(TRIM(unit_code)) AS unit_code
    FROM sandbox.pet_rent_validation.api_charges
    WHERE charge_code IN (SELECT charge_code FROM pet_codes)
),

paying_units AS (
    SELECT DISTINCT property_id, unit_code
    FROM direct_payers
    WHERE unit_code IS NOT NULL AND unit_code NOT IN ('', 'NAN', 'NONE')
),

unit_expanded AS (
    SELECT DISTINCT
        TRIM(a.property_id) AS property_id,
        UPPER(TRIM(a.tenant_code)) AS tenant_code,
        LOWER(TRIM(a.email)) AS email
    FROM sandbox.pet_rent_validation.api_charges a
    INNER JOIN paying_units pu
        ON TRIM(a.property_id) = pu.property_id
       AND UPPER(TRIM(a.unit_code)) = pu.unit_code
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

-- PetScreening profiles from Snowflake
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
    FROM PROD.common.d_units du
    JOIN PROD.common.d_properties p
        ON du.property_id = p.property_id
    JOIN PROD.petscreening.petscreening__user_enriched ue
        ON ue.unit_id = du.unit_id
    JOIN PROD.common.f_leases l
        ON du.unit_key = l.unit_key
       AND l.user_key = ue.user_key
    WHERE du.unit_source = 'yardi'  -- change to 'entrata' for Entrata PMCs
      AND du.property_id IN (
          SELECT DISTINCT TRIM(property_id)::INTEGER
          FROM sandbox.pet_rent_validation.api_charges
      )
      -- Only compliant profiles with an active household pet
      AND ue.compliance_status = 'compliant'
      AND ue.user_pet_type = 'household'
      AND ue.user_pet_status = 'active'
      -- Exclude junk/placeholder emails
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

-- Join profiles to paying sets
-- Primary key: (property_id, tenant_code) — case insensitive
-- Fallback key: (property_id, email) — for when tenant_code doesn't match
profiles_with_raw_flag AS (
    SELECT
        prof.*,
        CASE
            WHEN ptc.tenant_code IS NOT NULL THEN 1
            WHEN pte.email IS NOT NULL THEN 1
            ELSE 0
        END AS pet_rent_paid_raw,
        CASE
            WHEN ptc.tenant_code IS NOT NULL THEN 'tenant_code'
            WHEN pte.email IS NOT NULL THEN 'email'
            ELSE 'no_match'
        END AS match_method
    FROM ps_profiles prof
    LEFT JOIN paying_by_tc ptc
        ON TRIM(CAST(prof.property_id AS VARCHAR)) = ptc.property_id
       AND UPPER(TRIM(CAST(prof.tenant_code AS VARCHAR))) = ptc.tenant_code
    LEFT JOIN paying_by_email pte
        ON TRIM(CAST(prof.property_id AS VARCHAR)) = pte.property_id
       AND LOWER(TRIM(prof.user_email)) = pte.email
),

-- Cross-lease propagation:
-- If ANY row for the same (property_id, email) is paying, ALL rows are.
-- This prevents false positives from renewal/transfer lease mismatches.
user_level_paying AS (
    SELECT
        property_id,
        LOWER(TRIM(user_email)) AS user_email,
        MAX(pet_rent_paid_raw) AS is_paying
    FROM profiles_with_raw_flag
    GROUP BY 1, 2
),

profiles_final AS (
    SELECT
        prof.*,
        COALESCE(ulp.is_paying, prof.pet_rent_paid_raw) AS pet_rent_paid
    FROM profiles_with_raw_flag prof
    LEFT JOIN user_level_paying ulp
        ON prof.property_id = ulp.property_id
       AND LOWER(TRIM(prof.user_email)) = ulp.user_email
)

-- ============================================================
-- THE MISSING PET RENT POPULATION
-- pet_rent_paid = 0 → has a PetScreening household pet, NOT paying
-- ============================================================

-- Summary counts
SELECT
    COUNT(DISTINCT user_email) AS missing_tenant_count,
    COUNT(DISTINCT CASE WHEN pet_rent_paid = 1 THEN user_email END) AS paying_tenant_count,
    COUNT(DISTINCT user_email) AS total_profiles
FROM profiles_final;

-- Uncomment below for per-property breakdown:
-- SELECT
--     property_name,
--     COUNT(DISTINCT CASE WHEN pet_rent_paid = 0 THEN user_email END) AS missing,
--     COUNT(DISTINCT CASE WHEN pet_rent_paid = 1 THEN user_email END) AS paying,
--     COUNT(DISTINCT user_email) AS total
-- FROM profiles_final
-- GROUP BY 1
-- ORDER BY missing DESC;

-- Uncomment below to see the match method breakdown (useful for debugging):
-- SELECT
--     match_method,
--     COUNT(DISTINCT user_email) AS profiles
-- FROM profiles_with_raw_flag
-- GROUP BY 1;
```

> **Check:** The `missing_tenant_count` should match the "2,569 tenants" your boss cited.

### Step 4c: Estimate the missing revenue (the $76,482/month)

```sql
-- ============================================================================
-- MISSING REVENUE ESTIMATION
-- ============================================================================
--
-- HOW WE ESTIMATE:
-- We take the average pet rent that PAYING tenants are charged at each
-- property, and multiply by the number of MISSING tenants at that property.
--
-- WHY per-property average (not portfolio-wide)?
-- Pet rent varies by property — some charge $15/mo, others $50/mo. Using the
-- property's own average ensures the estimate reflects actual pricing, not a
-- blended average that would overcount cheap properties and undercount
-- expensive ones.
--
-- WHY we separate recurring vs one-time:
-- Some "pet" charge codes are monthly recurring rent, others are one-time
-- deposits. A $350 pet deposit should NOT be multiplied by 12 months. For
-- Yardi, we infer this from the date span: charges spanning > 60 days are
-- recurring (monthly rent), ≤ 60 days are one-time (deposit/fee).
-- ============================================================================

WITH pet_codes AS (
    SELECT column1 AS charge_code
    FROM VALUES ('PetRent'), ('Pet Rent'), ('PET RENT'), ('Pet Fee')
),

-- Average pet rent per property (from paying tenants in the API data)
avg_pet_rent AS (
    SELECT
        property_name,
        charge_code,
        AVG(TRY_TO_DOUBLE(charge_amount)) AS avg_amount,
        -- Classify as recurring or one-time based on date span
        -- > 60 days between from and to = recurring monthly charge
        -- <= 60 days = one-time fee/deposit
        CASE
            WHEN MEDIAN(DATEDIFF('day',
                TRY_TO_DATE(charge_from_date, 'YYYY-MM-DD'),
                TRY_TO_DATE(charge_to_date, 'YYYY-MM-DD')
            )) > 60 THEN 'recurring'
            ELSE 'onetime'
        END AS charge_class
    FROM sandbox.pet_rent_validation.api_charges
    WHERE charge_code IN (SELECT charge_code FROM pet_codes)
      AND TRY_TO_DOUBLE(charge_amount) > 0
      AND TRY_TO_DATE(charge_from_date, 'YYYY-MM-DD') IS NOT NULL
    GROUP BY 1, 2
),

-- Average RECURRING pet rent per property (this is the monthly number)
avg_recurring_per_property AS (
    SELECT
        property_name,
        AVG(avg_amount) AS avg_monthly_pet_rent
    FROM avg_pet_rent
    WHERE charge_class = 'recurring'
    GROUP BY 1
),

-- Missing tenant count per property (from Step 4b — you'll need to
-- materialize profiles_final or repeat the CTE chain above)
-- For simplicity, this assumes you've saved the missing count:
missing_per_property AS (
    -- *** Replace this with the profiles_final query from Step 4b ***
    -- or save the results to a table first:
    -- CREATE TABLE sandbox.pet_rent_validation.missing_tenants AS (...)
    SELECT
        property_name,
        COUNT(DISTINCT user_email) AS missing_count
    FROM sandbox.pet_rent_validation.missing_tenants  -- from Step 4b
    WHERE pet_rent_paid = 0
    GROUP BY 1
)

SELECT
    m.property_name,
    m.missing_count,
    a.avg_monthly_pet_rent,
    m.missing_count * a.avg_monthly_pet_rent AS estimated_monthly_missing
FROM missing_per_property m
LEFT JOIN avg_recurring_per_property a
    ON m.property_name = a.property_name
ORDER BY estimated_monthly_missing DESC NULLS LAST;

-- Portfolio total:
-- SELECT SUM(missing_count * avg_monthly_pet_rent) AS total_monthly_missing
-- FROM missing_per_property m
-- LEFT JOIN avg_recurring_per_property a ON m.property_name = a.property_name;
```

> **Check:** The `total_monthly_missing` should match the "$76,482/month" your boss cited.

---

## Step 5: Tranche 3 — Projected revenue at 100% adoption

This is calculated differently — it uses adoption data from PetScreening's QBR table.

```sql
-- ============================================================================
-- TRANCHE 3: PROJECTED AT 100% ADOPTION
-- ============================================================================
--
-- HOW IT WORKS:
-- For each property, we take the current monthly pet revenue and divide by
-- the current unit adoption rate. This extrapolates what the revenue would be
-- if every unit was enrolled.
--
-- Example: Property X earns $5,000/mo at 60% adoption → at 100% it would be
-- $5,000 / 0.60 = $8,333/mo → additional $3,333/mo.
--
-- WHY this is labeled as a "ceiling":
-- This assumes the remaining 40% of units would generate revenue at the same
-- rate as the enrolled 60%. In reality, some of those units may not have pets
-- at all, so actual revenue would be lower.
-- ============================================================================

WITH pet_codes AS (
    SELECT column1 AS charge_code
    FROM VALUES ('PetRent'), ('Pet Rent'), ('PET RENT'), ('Pet Fee')
),

-- Current month revenue per property
current_revenue AS (
    SELECT
        TRIM(a.property_id) AS property_id,
        a.property_name,
        SUM(TRY_TO_DOUBLE(a.charge_amount)) AS current_monthly_rev
    FROM sandbox.pet_rent_validation.api_charges a
    WHERE a.charge_code IN (SELECT charge_code FROM pet_codes)
      AND TRY_TO_DOUBLE(a.charge_amount) > 0
      AND TRY_TO_DATE(a.charge_from_date, 'YYYY-MM-DD') IS NOT NULL
      -- Only charges active in the current month
      AND DATE_TRUNC('MONTH', TRY_TO_DATE(a.charge_from_date, 'YYYY-MM-DD'))
          <= DATE_TRUNC('MONTH', CURRENT_DATE())
      AND DATE_TRUNC('MONTH', COALESCE(
          TRY_TO_DATE(a.charge_to_date, 'YYYY-MM-DD'),
          CASE WHEN LOWER(TRIM(a.tenant_status)) = 'current' THEN CURRENT_DATE()
               ELSE COALESCE(
                   TRY_TO_DATE(a.move_out, 'YYYY-MM-DD'),
                   TRY_TO_DATE(a.lease_to, 'YYYY-MM-DD'),
                   CURRENT_DATE()
               )
          END
      )) >= DATE_TRUNC('MONTH', CURRENT_DATE())
    GROUP BY 1, 2
),

-- Latest adoption rate per property from the QBR table
adoption AS (
    SELECT
        property_id,
        ACTIVE_UNITS,
        TOTAL_UNITS,
        CASE WHEN TOTAL_UNITS > 0
            THEN ACTIVE_UNITS::FLOAT / TOTAL_UNITS
            ELSE NULL
        END AS unit_adoption_rate
    FROM PROD.REPORTING.R_QUARTERLY_BUSINESS_REVIEW_REPORTING
    WHERE AFTER_PROPERTY_LAUNCH_FLAG = TRUE
      AND TOTAL_UNITS > 0
    QUALIFY ROW_NUMBER() OVER (PARTITION BY property_id ORDER BY period_month DESC) = 1
)

SELECT
    cr.property_name,
    cr.current_monthly_rev,
    a.unit_adoption_rate,
    ROUND(a.unit_adoption_rate * 100, 1) AS adoption_pct,
    cr.current_monthly_rev / a.unit_adoption_rate AS projected_at_100pct,
    (cr.current_monthly_rev / a.unit_adoption_rate) - cr.current_monthly_rev AS additional_at_100pct
FROM current_revenue cr
INNER JOIN adoption a ON cr.property_id::INTEGER = a.property_id
WHERE a.unit_adoption_rate > 0
ORDER BY additional_at_100pct DESC;

-- Portfolio totals:
-- SELECT
--     SUM(current_monthly_rev) AS total_current,
--     SUM(current_monthly_rev / unit_adoption_rate) AS total_projected,
--     SUM((current_monthly_rev / unit_adoption_rate) - current_monthly_rev) AS total_additional
-- FROM current_revenue cr
-- INNER JOIN adoption a ON cr.property_id::INTEGER = a.property_id
-- WHERE a.unit_adoption_rate > 0;
```

> **Check:** The `total_additional` should match the "$157,847/month" your boss cited.
> The average adoption should be around 70.6%.

---

## Step 6: Diagnostic queries (finding gaps)

If any numbers don't match, run these to figure out why.

```sql
-- ============================================================================
-- DIAGNOSTIC 1: Match rate analysis
-- How are profiles matching to the API data?
-- ============================================================================
-- (Run the profiles_with_raw_flag CTE from Step 4b and add:)

SELECT
    match_method,
    COUNT(*) AS profile_rows,
    COUNT(DISTINCT user_email) AS unique_emails
FROM profiles_with_raw_flag
GROUP BY 1;

-- Expected for healthy Yardi data:
--   tenant_code: 60-80% of matches
--   email: 10-20% (fallback)
--   no_match: 10-30% (these are the "missing" population)
-- If no_match is > 50%, the tenant_code extraction is broken.
```

```sql
-- ============================================================================
-- DIAGNOSTIC 2: Reverse check
-- Who has a pet charge in the API but NO PetScreening profile?
-- These are tenants paying pet rent who never went through screening.
-- ============================================================================

WITH pet_payers_in_api AS (
    SELECT DISTINCT
        TRIM(property_id) AS property_id,
        property_name,
        LOWER(TRIM(email)) AS email,
        tenant_code
    FROM sandbox.pet_rent_validation.api_charges
    WHERE charge_code IN ('PetRent', 'Pet Rent', 'PET RENT', 'Pet Fee')
      AND email IS NOT NULL AND TRIM(email) <> ''
),

ps_emails AS (
    SELECT DISTINCT
        CAST(du.property_id AS VARCHAR) AS property_id,
        LOWER(TRIM(ue.user_email)) AS email
    FROM PROD.common.d_units du
    JOIN PROD.petscreening.petscreening__user_enriched ue ON ue.unit_id = du.unit_id
    WHERE du.unit_source = 'yardi'
      AND du.property_id IN (
          SELECT DISTINCT TRIM(property_id)::INTEGER
          FROM sandbox.pet_rent_validation.api_charges
      )
      AND ue.user_pet_type = 'household'
      AND ue.user_pet_status = 'active'
)

SELECT
    api.property_name,
    COUNT(*) AS paying_no_ps_profile
FROM pet_payers_in_api api
LEFT JOIN ps_emails ps
    ON api.property_id = ps.property_id AND api.email = ps.email
WHERE ps.email IS NULL
GROUP BY 1
ORDER BY 2 DESC;

-- If this number is large, it means many tenants are paying pet rent
-- without a PetScreening profile (pre-launch grandfathered tenants or
-- tenants the PMC added charges for manually).
```

```sql
-- ============================================================================
-- DIAGNOSTIC 3: Spot-check specific tenants
-- Pick a "missing" tenant and trace them through the pipeline
-- ============================================================================

-- Replace with an actual email from your missing list
SET check_email = 'john.doe@gmail.com';

-- What does the API show for this person?
SELECT *
FROM sandbox.pet_rent_validation.api_charges
WHERE LOWER(TRIM(email)) = $check_email;

-- What does PetScreening show?
SELECT *
FROM PROD.petscreening.petscreening__user_enriched
WHERE LOWER(TRIM(user_email)) = $check_email;

-- What tenant_code does f_leases have?
SELECT
    l.lease_source_external_id,
    l.lease_source_external_id:tenant_code::STRING AS tc_from_json,
    du.property_id,
    ue.user_email
FROM PROD.common.f_leases l
JOIN PROD.common.d_units du ON l.unit_key = du.unit_key
JOIN PROD.petscreening.petscreening__user_enriched ue
    ON ue.user_key = l.user_key AND ue.unit_id = du.unit_id
WHERE LOWER(TRIM(ue.user_email)) = $check_email;
```

---

## Quick Reference: What maps to what

| Your boss's number | Where it comes from | Query to validate |
|---|---|---|
| "$72,933/month" | Tranche 1 — monthly revenue change (post avg - pre avg, summed) | Step 3: `SUM(monthly_change)` from `launch_analysis` where `pre_months_used > 0` |
| "$2,072,775" | Tranche 1 — total cumulative revenue change | Step 3: `SUM(total_change)` from `launch_analysis` where `pre_months_used > 0` |
| "22.6% increase" | Tranche 1 — monthly change / pre-launch baseline | `SUM(monthly_change) / SUM(pre_avg_monthly)` from comparable properties |
| "2,569 tenants" | Tranche 2 — missing pet rent count | Step 4b: `COUNT(DISTINCT user_email)` where `pet_rent_paid = 0` |
| "$76,482/month" | Tranche 2 — missing count × avg pet rent per property | Step 4c: `SUM(missing_count * avg_monthly_pet_rent)` |
| "301 properties" | Tranche 2 — properties with at least 1 missing tenant | Step 4b: `COUNT(DISTINCT property_name)` where `pet_rent_paid = 0` |
| "70.6% adoption" | Tranche 3 — avg unit adoption from QBR table | Step 5: `AVG(unit_adoption_rate)` |
| "$157,847/month" | Tranche 3 — additional revenue at 100% adoption | Step 5: `SUM(additional_at_100pct)` |

---

## Tips for you and Nick

1. **Run these on the same CSV export.** The API data is live — export once, share the file, both validate from the same snapshot.

2. **Start with Step 1** to make sure the CSV loaded correctly and you see the same charge codes.

3. **Step 4b is the money query.** If you both get 2,569 missing tenants from Step 4b, you're golden. If not, run the diagnostics in Step 6 to figure out where the gap is.

4. **Save intermediate results.** After running Step 4b, do `CREATE TABLE sandbox.pet_rent_validation.missing_tenants AS (...)` so you can reference it in Step 4c without repeating the entire CTE chain.

5. **The pet_codes CTE is the single source of truth.** Make sure the charge codes in that CTE exactly match what you selected in the app's dropdown.
