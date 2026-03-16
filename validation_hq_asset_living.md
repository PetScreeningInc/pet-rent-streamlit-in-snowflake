# Yardi Validation: HQ - Asset Living

**Table:** `RAW.MISC.CHARGES_HQ_ASSET_LIVING_2026_03_13`
**Created:** 2026-03-13

---

## Step 0: Fix the Raw Table

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

---

## Step 1: Find Pet Charge Codes

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

---

## Step 2: Monthly Pet Fee Revenue (Portfolio Level)

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

---

## Step 3: Monthly Revenue by Property

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

---

## Step 4: Pre/Post PetScreening Launch Analysis (6-and-6 Methodology)

**Updated 2026-03-16:** Uses up to 6 months pre, up to 6 most recent completed months post.
Monthly lift = recent post avg − pre avg. Total lift = actual observed (sum post revenue − pre avg × post months).

**Full query with all fixes is in `step4_lift_analysis.sql`** — includes one-time charge handling, property_id joins, non-current tenant null date handling, and baseline_meaningful flag.

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

---

## Step 5: Missing Pet Rent (Confirmed Profiles Not Paying)

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

---

## Step 6: Summary — Missing Count by Property

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

---

## Quick Stats

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
