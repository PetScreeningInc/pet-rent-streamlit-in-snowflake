-- ============================================================================
-- Step 4: Pre/Post PetScreening Launch Analysis (6-and-6 Methodology)
-- Updated: 2026-03-16
-- 
-- Methodology:
--   Pre baseline:  up to 6 months before launch (uses whatever is available)
--   Post current:  up to 6 most recent COMPLETED months (excludes current partial month)
--   Monthly lift:  post_recent_avg − pre_avg
--   Total lift:    sum(all post revenue) − (pre_avg × total post months)  [actual observed]
--
-- Handles:
--   - One-time vs recurring charge classification (Yardi: median span inference)
--   - One-time charges only count in their from_date month (not spread across months)
--   - property_id carried through all CTEs to avoid name collisions
--   - Non-current tenants with all-null dates: charge limited to from_date month only
--   - baseline_meaningful: pre_avg must be >= 2% of post_recent_avg
-- ============================================================================

WITH pet_codes AS (
    -- Auto-detect pet-related charge codes
    SELECT DISTINCT charge_code
    FROM "RAW"."MISC"."CHARGES_HQ_ASSET_LIVING_2026_03_13"
    WHERE LOWER(charge_code) ILIKE ANY ('%pet%', '%animal%')
),

-- Classify each charge code as recurring or one-time using median date span
-- Median span > 60 days = recurring (monthly pet rent)
-- Median span <= 60 days = one-time (pet deposit, DNA kit, etc.)
charge_classification AS (
    SELECT
        charge_code,
        MEDIAN(
            CASE
                WHEN charge_to_date IS NOT NULL AND charge_from_date IS NOT NULL
                THEN DATEDIFF(DAY, charge_from_date, charge_to_date)
                ELSE NULL
            END
        ) AS median_span_days,
        CASE
            WHEN MEDIAN(
                CASE
                    WHEN charge_to_date IS NOT NULL AND charge_from_date IS NOT NULL
                    THEN DATEDIFF(DAY, charge_from_date, charge_to_date)
                    ELSE NULL
                END
            ) > 60 THEN 'recurring'
            WHEN MEDIAN(
                CASE
                    WHEN charge_to_date IS NOT NULL AND charge_from_date IS NOT NULL
                    THEN DATEDIFF(DAY, charge_from_date, charge_to_date)
                    ELSE NULL
                END
            ) IS NULL THEN 'recurring'  -- default to recurring if no span data
            ELSE 'onetime'
        END AS charge_type
    FROM "RAW"."MISC"."CHARGES_HQ_ASSET_LIVING_2026_03_13"
    WHERE charge_code IN (SELECT charge_code FROM pet_codes)
    GROUP BY charge_code
),

parsed_charges AS (
    SELECT
        a.property_id,
        a.property_name,
        a.charge_amount::DOUBLE   AS amount,
        a.charge_from_date        AS from_date,
        a.launch_date,
        a.tenant_status,
        cc.charge_type,
        -- Effective end date logic:
        --   1. One-time charges → same as from_date (only count once)
        --   2. Explicit charge_to_date → use it
        --   3. Current tenant, no end date → NULL (will become CURRENT_DATE)
        --   4. Non-current tenant with move_out or lease_to → use those
        --   5. Non-current tenant with ALL nulls → same as from_date (don't expand)
        CASE
            WHEN cc.charge_type = 'onetime' THEN a.charge_from_date
            WHEN a.charge_to_date IS NOT NULL THEN a.charge_to_date
            WHEN LOWER(TRIM(a.tenant_status)) = 'current' THEN NULL
            WHEN COALESCE(a.move_out, a.lease_to) IS NOT NULL THEN COALESCE(a.move_out, a.lease_to)
            ELSE a.charge_from_date  -- non-current with no dates → limit to from_date only
        END AS effective_to_date
    FROM "RAW"."MISC"."CHARGES_HQ_ASSET_LIVING_2026_03_13" a
    INNER JOIN pet_codes pc ON a.charge_code = pc.charge_code
    LEFT JOIN charge_classification cc ON a.charge_code = cc.charge_code
    WHERE a.charge_amount > 0
      AND a.charge_from_date IS NOT NULL
),

month_spine AS (
    SELECT DATEADD(MONTH, seq, '2020-01-01'::DATE) AS month_start
    FROM (SELECT SEQ4() AS seq FROM TABLE(GENERATOR(ROWCOUNT => 120)))
    WHERE DATEADD(MONTH, seq, '2020-01-01'::DATE) <= DATE_TRUNC('MONTH', CURRENT_DATE())
),

-- Expand charges into monthly buckets
-- One-time charges: only their from_date month (effective_to_date = from_date)
-- Recurring charges: every month from start to end
charge_months AS (
    SELECT
        pc.property_id,
        pc.property_name,
        pc.amount,
        pc.launch_date,
        pc.charge_type,
        ms.month_start
    FROM parsed_charges pc
    CROSS JOIN month_spine ms
    WHERE ms.month_start >= DATE_TRUNC('MONTH', pc.from_date)
      AND ms.month_start <= DATE_TRUNC('MONTH', COALESCE(pc.effective_to_date, CURRENT_DATE()))
),

-- Monthly revenue by property (keyed by property_id + property_name for safety)
monthly_by_prop AS (
    SELECT
        property_id,
        property_name,
        launch_date,
        month_start,
        SUM(amount) AS monthly_revenue
    FROM charge_months
    GROUP BY 1, 2, 3, 4
),

-- Classify each month as pre or post launch
classified AS (
    SELECT
        property_id,
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
        property_id,
        property_name,
        AVG(monthly_revenue) AS pre_avg,
        COUNT(*)             AS pre_months
    FROM (
        SELECT *,
            ROW_NUMBER() OVER (
                PARTITION BY property_id
                ORDER BY month_start DESC
            ) AS rn
        FROM classified
        WHERE period = 'pre'
    )
    WHERE rn <= 6
    GROUP BY 1, 2
),

-- Post-launch: all months (for actual observed cumulative revenue)
post_all AS (
    SELECT
        property_id,
        property_name,
        launch_date,
        SUM(monthly_revenue)  AS post_total,
        COUNT(*)              AS post_months
    FROM classified
    WHERE period = 'post'
    GROUP BY 1, 2, 3
),

-- Post-launch recent: up to 6 most recent COMPLETED months (excludes current month)
post_recent AS (
    SELECT
        property_id,
        property_name,
        AVG(monthly_revenue)  AS post_recent_avg,
        COUNT(*)              AS recent_post_months
    FROM (
        SELECT *,
            ROW_NUMBER() OVER (
                PARTITION BY property_id
                ORDER BY month_start DESC
            ) AS rn
        FROM classified
        WHERE period = 'post'
          AND month_start < DATE_TRUNC('MONTH', CURRENT_DATE())  -- exclude current partial month
    )
    WHERE rn <= 6
    GROUP BY 1, 2
)

-- Final output: per-property lift analysis
SELECT
    pa.property_id,
    pa.property_name,
    pa.launch_date,
    COALESCE(b.pre_months, 0)                                                AS pre_months,
    COALESCE(pr.recent_post_months, 0)                                        AS recent_post_months,
    pa.post_months                                                            AS total_post_months,
    ROUND(COALESCE(b.pre_avg, 0), 2)                                         AS pre_avg_monthly,
    ROUND(COALESCE(pr.post_recent_avg, 0), 2)                                AS current_avg_monthly,
    ROUND(COALESCE(pr.post_recent_avg, 0) - COALESCE(b.pre_avg, 0), 2)      AS monthly_lift,
    ROUND(pa.post_total - (COALESCE(b.pre_avg, 0) * pa.post_months), 2)     AS cumulative_impact,
    -- Baseline quality: need 3+ pre months to be reliable
    CASE
        WHEN COALESCE(b.pre_months, 0) >= 3 THEN 'reliable'
        ELSE 'insufficient'
    END AS baseline_quality,
    -- Baseline meaningful: pre_avg must be >= 2% of post_recent_avg
    -- Catches properties like Miro ($8 pre vs $5,600 post) that weren't really
    -- charging pet rent before PS — their "lift" is just the post-launch revenue
    CASE
        WHEN COALESCE(pr.post_recent_avg, 0) <= 0 THEN TRUE   -- no post data → keep (can't judge)
        WHEN COALESCE(b.pre_avg, 0) >= COALESCE(pr.post_recent_avg, 0) * 0.02 THEN TRUE
        ELSE FALSE
    END AS baseline_meaningful
FROM post_all pa
LEFT JOIN pre_baseline b ON pa.property_id = b.property_id
LEFT JOIN post_recent pr ON pa.property_id = pr.property_id
ORDER BY monthly_lift DESC;


-- ============================================================================
-- AGGREGATE: Sum across reliable + meaningful properties only
-- (matches the app KPIs)
-- ============================================================================
-- To use: wrap the query above as a subquery, or create a temp table/CTE:
--
-- SELECT
--     COUNT(*)                          AS comparable_properties,
--     ROUND(SUM(monthly_lift), 2)       AS agg_monthly_lift,
--     ROUND(SUM(cumulative_impact), 2)  AS agg_cumulative_impact,
--     ROUND(SUM(pre_avg_monthly), 2)    AS agg_pre_baseline
-- FROM ( /* full query above */ ) results
-- WHERE baseline_quality = 'reliable'
--   AND baseline_meaningful = TRUE;


-- ============================================================================
-- DIAGNOSTIC: Show charge code classification
-- Run this to verify which codes are recurring vs one-time
-- ============================================================================
-- SELECT
--     charge_code,
--     charge_type,
--     median_span_days,
--     COUNT(*) AS charge_lines
-- FROM charge_classification cc
-- JOIN "RAW"."MISC"."CHARGES_HQ_ASSET_LIVING_2026_03_13" a USING (charge_code)
-- WHERE a.charge_code IN (SELECT charge_code FROM pet_codes)
-- GROUP BY 1, 2, 3
-- ORDER BY charge_type, charge_code;
