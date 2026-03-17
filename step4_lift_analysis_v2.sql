-- Step 4: Lift Analysis — 6-and-6 windowed methodology
-- Matches app.py compute_launch_analysis() + charts tab aggregation logic
-- Last synced: 2026-03-17

-- ═══════════════════════════════════════════════════════════════════
-- KEY DIFFERENCES FROM PREVIOUS VERSION (v1):
--   1. effective_to_date: past tenants with no dates → NULL (active through
--      current_date), not charge_from_date. Matches app's _effective_to_date().
--   2. baseline_reliable: counts ALL months in the window before launch,
--      not just months with revenue. A property with 5 pre-launch months
--      of $0 still counts as "reliable" (>= 3 pre months).
--   3. pre_avg includes $0 months (avg over calendar months, not just
--      months-with-revenue). Matches app's prop_data.get(m, 0).
-- ═══════════════════════════════════════════════════════════════════

with pet_codes as (
    -- charge codes that look pet-related (same auto-select as app)
    select distinct charge_code
    from "RAW"."MISC"."CHARGES_HQ_ASSET_LIVING_2026_03_16"
    where lower(charge_code) ilike any ('%pet%', '%animal%')
),

charge_classification as (
    -- separate one-time vs recurring based on median date span
    -- matches app: median > 60 → recurring, NULL → recurring, else onetime
    select
        charge_code,
        median(
            case
                when charge_to_date is not null and charge_from_date is not null
                then datediff(day, charge_from_date, charge_to_date)
                else null
            end
        ) as median_span_days,
        case
            when median(
                case
                    when charge_to_date is not null and charge_from_date is not null
                    then datediff(day, charge_from_date, charge_to_date)
                    else null
                end
            ) is null then 'recurring'       -- no date spans → default recurring (matches app)
            when median(
                case
                    when charge_to_date is not null and charge_from_date is not null
                    then datediff(day, charge_from_date, charge_to_date)
                    else null
                end
            ) > 60 then 'recurring'
            else 'onetime'
        end as charge_type
    from "RAW"."MISC"."CHARGES_HQ_ASSET_LIVING_2026_03_16"
    where charge_code in (select charge_code from pet_codes)
    group by 1
),

parsed_charges as (
    -- effective end date logic matching app's _effective_to_date()
    -- one-time: charge_from_date (single month)
    -- has charge_to_date: use it
    -- current tenant, no charge_to: NULL (→ active through current_date)
    -- past tenant: coalesce(move_out, lease_to, NULL)
    --   NOTE: app returns NULL when past tenant has no dates at all
    --   (treats as active — "shouldn't happen often")
    select
        a.property_id,
        a.property_name,
        a.charge_amount::double as amount,
        a.charge_from_date as from_date,
        a.launch_date,
        a.tenant_status,
        cc.charge_type,
        case
            when cc.charge_type = 'onetime' then a.charge_from_date
            when a.charge_to_date is not null then a.charge_to_date
            when lower(trim(a.tenant_status)) = 'current' then null
            -- past tenants: coalesce move_out → lease_to → NULL (not charge_from_date!)
            else coalesce(a.move_out, a.lease_to)
        end as effective_to_date
    from "RAW"."MISC"."CHARGES_HQ_ASSET_LIVING_2026_03_16" a
    inner join pet_codes pc on a.charge_code = pc.charge_code
    left join charge_classification cc on a.charge_code = cc.charge_code
    where a.charge_amount > 0
      and a.charge_from_date is not null
),

month_spine as (
    -- full month range covering all possible data
    select dateadd(month, seq, '2020-01-01'::date) as month_start
    from (select seq4() as seq from table(generator(rowcount => 120)))
    where dateadd(month, seq, '2020-01-01'::date) <= date_trunc('month', current_date())
),

charge_months as (
    -- expand each charge into month-level rows
    -- NULL effective_to_date → active through current_date (matches app's window_end fallback)
    select
        pc.property_id,
        pc.property_name,
        pc.amount,
        pc.launch_date,
        pc.charge_type,
        ms.month_start
    from parsed_charges pc
    cross join month_spine ms
    where ms.month_start >= date_trunc('month', pc.from_date)
      and ms.month_start <= date_trunc('month', coalesce(pc.effective_to_date, current_date()))
),

monthly_by_prop as (
    -- total pet charge revenue by property by month
    select
        property_id,
        property_name,
        launch_date,
        month_start,
        sum(amount) as monthly_revenue
    from charge_months
    group by 1, 2, 3, 4
),

-- ═══════════════════════════════════════════════════════════════════
-- IMPORTANT: The app counts ALL calendar months before launch in the
-- window, even $0 months, for both pre_avg and baseline_reliable.
-- We need a full month spine per property to match this behavior.
-- ═══════════════════════════════════════════════════════════════════

-- Get the earliest charge date per property (= start of their window)
property_window as (
    select
        property_id,
        property_name,
        launch_date,
        min(month_start) as earliest_month
    from monthly_by_prop
    group by 1, 2, 3
),

-- Full calendar months per property from earliest charge to now
property_month_spine as (
    select
        pw.property_id,
        pw.property_name,
        pw.launch_date,
        ms.month_start
    from property_window pw
    cross join month_spine ms
    where ms.month_start >= pw.earliest_month
      and ms.month_start <= date_trunc('month', current_date())
),

classified as (
    -- every calendar month for each property, with revenue (0 if no charges)
    select
        pms.property_id,
        pms.property_name,
        pms.launch_date,
        pms.month_start,
        coalesce(mbp.monthly_revenue, 0) as monthly_revenue,
        case
            when pms.launch_date is null then 'no_launch'
            when pms.month_start < date_trunc('month', pms.launch_date) then 'pre'
            else 'post'
        end as period
    from property_month_spine pms
    left join monthly_by_prop mbp
        on pms.property_id = mbp.property_id
        and pms.month_start = mbp.month_start
),

-- Count ALL pre-launch months (including $0) for baseline_reliable check
all_pre_months as (
    select
        property_id,
        count(*) as total_pre_months
    from classified
    where period = 'pre'
    group by 1
),

pre_baseline as (
    -- baseline = up to 6 months right before launch (including $0 months!)
    -- matches app's: pre_months[-6:] with prop_data.get(m, 0)
    select
        property_id,
        property_name,
        avg(monthly_revenue) as pre_avg,
        count(*) as pre_months_used
    from (
        select *,
            row_number() over (partition by property_id order by month_start desc) as rn
        from classified
        where period = 'pre'
    )
    where rn <= 6
    group by 1, 2
),

post_all as (
    -- all post-launch months (including current partial month) for cumulative
    select
        property_id,
        property_name,
        launch_date,
        sum(monthly_revenue) as post_total,
        count(*) as post_months
    from classified
    where period = 'post'
    group by 1, 2, 3
),

post_recent as (
    -- up to 6 most recent COMPLETED post-launch months for current avg
    -- excludes current partial month (matches app: m < current_month)
    select
        property_id,
        property_name,
        avg(monthly_revenue) as post_recent_avg,
        count(*) as recent_post_months
    from (
        select *,
            row_number() over (partition by property_id order by month_start desc) as rn
        from classified
        where period = 'post'
          and month_start < date_trunc('month', current_date())
    )
    where rn <= 6
    group by 1, 2
),

final_analysis as (
    select
        pa.property_id,
        pa.property_name,
        pa.launch_date,

        -- pre baseline info
        coalesce(apm.total_pre_months, 0) as all_pre_months,
        coalesce(b.pre_months_used, 0)    as pre_months_used,
        round(coalesce(b.pre_avg, 0), 2)  as pre_avg_monthly,

        -- post info
        pa.post_months                     as total_post_months,
        coalesce(pr.recent_post_months, 0) as recent_post_months,
        round(coalesce(pr.post_recent_avg, 0), 2) as current_avg_monthly,

        -- lift metrics
        round(coalesce(pr.post_recent_avg, 0) - coalesce(b.pre_avg, 0), 2) as monthly_lift,
        round(pa.post_total - (coalesce(b.pre_avg, 0) * pa.post_months), 2) as cumulative_impact,

        -- baseline_reliable: >= 3 pre-launch months IN THE WINDOW (not just months with revenue)
        -- matches app's: len(pre_months) >= 3
        case
            when coalesce(apm.total_pre_months, 0) >= 3 then 'reliable'
            else 'insufficient'
        end as baseline_quality,

        -- baseline_meaningful: pre_avg >= 2% of post_recent_avg
        -- matches app's: pre_avg >= post_recent_avg * 0.02 OR post_recent_avg <= 0
        case
            when coalesce(pr.post_recent_avg, 0) <= 0 then true
            when coalesce(b.pre_avg, 0) >= coalesce(pr.post_recent_avg, 0) * 0.02 then true
            else false
        end as baseline_meaningful

    from post_all pa
    left join all_pre_months apm on pa.property_id = apm.property_id
    left join pre_baseline b on pa.property_id = b.property_id
    left join post_recent pr on pa.property_id = pr.property_id
)

-- ═══════════════════════════════════════════════════════════════════
-- Output: matches app's comparable filter + aggregate summary
-- comparable = baseline_reliable AND baseline_meaningful
-- ═══════════════════════════════════════════════════════════════════

-- Per-property detail (uncomment to see individual properties):
-- select * from final_analysis order by monthly_lift desc;

-- Aggregate summary (matches app's agg_diff_mo / agg_diff):
select
    count(*) as comparable,
    round(sum(monthly_lift), 2) as agg_monthly_lift,
    round(sum(cumulative_impact), 2) as agg_cumulative_impact
from final_analysis
where baseline_quality = 'reliable'
  and baseline_meaningful = true;
