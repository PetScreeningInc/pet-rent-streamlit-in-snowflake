-- ============================================================================
-- PET CHARGE LIFT ANALYSIS — Asset Living (Yardi)
-- ============================================================================
-- compares pet charge revenue before vs after petscreening launch
-- methodology: up to 6 months pre-launch baseline vs 6 most recent
-- completed post-launch months for "current lift", plus cumulative impact
-- across all post months
--
-- matches the app.py compute_launch_analysis() logic
-- ============================================================================


-- grab charge codes that look pet-related
with pet_codes as (
    select distinct charge_code
    from "RAW"."MISC"."CHARGES_HQ_ASSET_LIVING_2026_03_16"
    where lower(charge_code) ilike any ('%pet%', '%animal%')
),

-- ============================================================================
-- IMPORTANT: classifying one-time vs recurring charges
-- ============================================================================
-- we look at how long charges typically span (median days between from/to)
-- if the median span is > 60 days its recurring (monthly pet rent)
-- if <= 60 days its one-time (pet deposit, pet fee at move-in)
-- if null (no date ranges at all) we default to recurring
-- this matters because one-time charges should NOT get spread across months
charge_classification as (
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
            ) > 60 then 'recurring'
            when median(
                case
                    when charge_to_date is not null and charge_from_date is not null
                    then datediff(day, charge_from_date, charge_to_date)
                    else null
                end
            ) is null then 'recurring'
            else 'onetime'
        end as charge_type
    from "RAW"."MISC"."CHARGES_HQ_ASSET_LIVING_2026_03_16"
    where charge_code in (select charge_code from pet_codes)
    group by 1
),

-- ============================================================================
-- IMPORTANT: setting realistic end dates for each charge
-- ============================================================================
-- this is the core logic that determines how long a charge "lives"
-- and therefore which months it gets counted in
--
-- for one-time charges: pin to the start month only (no spreading)
-- for recurring with a charge_to_date: use it
-- for current tenants with no end date: leave open (they're still active)
-- for past tenants with no end date: fall back to move_out or lease_to
-- for past tenants with NO dates at all: leave open (matches app.py behavior)
--
-- note: the app.py _effective_to_date() function does the same thing
parsed_charges as (
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
            when coalesce(a.move_out, a.lease_to) is not null
                then coalesce(a.move_out, a.lease_to)
            else null  -- no dates at all, treat as open-ended (same as app.py)
        end as effective_to_date
    from "RAW"."MISC"."CHARGES_HQ_ASSET_LIVING_2026_03_16" a
    inner join pet_codes pc on a.charge_code = pc.charge_code
    left join charge_classification cc on a.charge_code = cc.charge_code
    where a.charge_amount > 0
      and a.charge_from_date is not null
),

-- AI-GENERATED: month spine to expand charges across their active months
-- generates one row per month from jan 2020 through current month
month_spine as (
    select dateadd(month, seq, '2020-01-01'::date) as month_start
    from (select seq4() as seq from table(generator(rowcount => 120)))
    where dateadd(month, seq, '2020-01-01'::date) <= date_trunc('month', current_date())
),

-- expand each charge into the months its active
-- one-time charges only hit their start month
-- recurring charges hit every month from start to end (or current_date if open)
charge_months as (
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

-- total pet charge revenue by property by month
monthly_by_prop as (
    select
        property_id,
        property_name,
        launch_date,
        month_start,
        sum(amount) as monthly_revenue
    from charge_months
    group by 1, 2, 3, 4
),

-- tag each month as pre or post launch
classified as (
    select
        property_id,
        property_name,
        launch_date,
        month_start,
        monthly_revenue,
        case
            when launch_date is null then 'no_launch'
            when month_start < date_trunc('month', launch_date) then 'pre'
            else 'post'
        end as period
    from monthly_by_prop
),

-- ============================================================================
-- IMPORTANT: pre-launch baseline
-- ============================================================================
-- take up to 6 months immediately before launch
-- this is the "business as usual" rate we compare against
-- need at least 3 months for the baseline to be considered reliable
pre_baseline as (
    select
        property_id,
        property_name,
        avg(monthly_revenue) as pre_avg,
        count(*) as pre_months
    from (
        select *,
            row_number() over (
                partition by property_id
                order by month_start desc
            ) as rn
        from classified
        where period = 'pre'
    )
    where rn <= 6
    group by 1, 2
),

-- all post-launch months for cumulative impact
post_all as (
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

-- ============================================================================
-- IMPORTANT: current lift — 6 most recent COMPLETED months
-- ============================================================================
-- excludes current partial month so we dont undercount
-- this is the "what is petscreening doing right now" number
post_recent as (
    select
        property_id,
        property_name,
        avg(monthly_revenue) as post_recent_avg,
        count(*) as recent_post_months
    from (
        select *,
            row_number() over (
                partition by property_id
                order by month_start desc
            ) as rn
        from classified
        where period = 'post'
          and month_start < date_trunc('month', current_date())  -- exclude current partial month
    )
    where rn <= 6
    group by 1, 2
),

-- bring it all together
final as (
    select
        pa.property_id,
        pa.property_name,
        pa.launch_date,

        coalesce(b.pre_months, 0) as pre_months,
        coalesce(pr.recent_post_months, 0) as recent_post_months,
        pa.post_months as total_post_months,

        round(coalesce(b.pre_avg, 0), 2) as pre_avg_monthly,
        round(coalesce(pr.post_recent_avg, 0), 2) as current_avg_monthly,

        -- monthly lift: how much more per month now vs before
        round(coalesce(pr.post_recent_avg, 0) - coalesce(b.pre_avg, 0), 2) as monthly_lift,

        -- cumulative impact: total observed revenue minus what wouldve happened at the old rate
        round(pa.post_total - (coalesce(b.pre_avg, 0) * pa.post_months), 2) as cumulative_impact,

        -- need at least 3 pre months so baseline isnt too thin
        case
            when coalesce(b.pre_months, 0) >= 3 then 'reliable'
            else 'insufficient'
        end as baseline_quality,

        -- filter out properties where pre baseline is basically zero
        -- (they werent really charging pet rent before PS)
        -- threshold: pre_avg must be >= 2% of current avg to be meaningful
        case
            when coalesce(pr.post_recent_avg, 0) <= 0 then true
            when coalesce(b.pre_avg, 0) >= coalesce(pr.post_recent_avg, 0) * 0.02 then true
            else false
        end as baseline_meaningful

    from post_all pa
    left join pre_baseline b on pa.property_id = b.property_id
    left join post_recent pr on pa.property_id = pr.property_id
    order by monthly_lift desc
)


-- ============================================================================
-- AGGREGATE OUTPUT
-- ============================================================================
-- only properties with reliable + meaningful baselines
-- comparable = properties we can actually trust the before/after on
select
    count(*) as comparable_properties,
    round(sum(monthly_lift), 2) as aggregate_monthly_lift,
    round(sum(cumulative_impact), 2) as aggregate_cumulative_impact
from final
where baseline_quality = 'reliable'
  and baseline_meaningful = true;


-- uncomment for property-level detail:
-- select * from final
-- where baseline_quality = 'reliable'
--   and baseline_meaningful = true
-- order by monthly_lift desc;
