-- ═══════════════════════════════════════════════════════════════════
-- ENTRATA LIFT ANALYSIS — RECURRING vs ALL CHARGES
-- HQ Asset Living | 2026-03-19
--
-- Same 6-and-6 methodology as the Yardi lift analysis.
-- Runs twice: once with ALL pet charges, once with RECURRING ONLY.
-- Compare the two to see if one-time deposits are distorting the lift.
--
-- IMPORTANT: Entrata charges duplicate per customer on the same lease.
-- We dedup on (property_id, lease_id, charge_code, amount, from_date)
-- so shared leases don't inflate revenue.
-- ═══════════════════════════════════════════════════════════════════

with source_charges as (
    select * from "RAW"."MISC"."ENTRATA_CHARGES_HQ_ASSET_LIVING_2026_03_19"
),

-- grab charge codes that look pet-related
pet_codes as (
    select distinct charge_code
    from source_charges
    where lower(charge_code) ilike any ('%pet%', '%animal%')
),

-- ═══════════════════════════════════════════════════════════════════
-- DEDUP: one row per charge per lease (not per customer)
-- without this, revenue doubles on every shared lease
-- ═══════════════════════════════════════════════════════════════════
deduped_charges as (
    select *
    from (
        select
            a.*,
            row_number() over (
                partition by a.property_id, a.lease_id, a.charge_code,
                             a.charge_amount, a.charge_from_date
                order by a.email  -- deterministic pick, doesn't matter which customer
            ) as _rn
        from source_charges a
        inner join pet_codes pc on a.charge_code = pc.charge_code
        where a.charge_amount > 0
          and a.charge_from_date is not null
    )
    where _rn = 1
),

-- ═══════════════════════════════════════════════════════════════════
-- CLASSIFY: use Entrata's explicit frequency field
-- if frequency is null/empty, fall back to recurring (conservative)
-- ═══════════════════════════════════════════════════════════════════
classified_charges as (
    select
        property_id,
        property_name,
        charge_code,
        charge_amount::double as amount,
        charge_from_date as from_date,
        launch_date,
        tenant_status,
        -- Entrata gives us frequency directly
        case
            when lower(trim(frequency)) in ('one-time', 'one time', 'onetime') then 'onetime'
            else 'recurring'  -- monthly, weekly, or null/blank = assume recurring
        end as charge_type,
        -- effective end date
        case
            when lower(trim(frequency)) in ('one-time', 'one time', 'onetime')
                then charge_from_date  -- pin one-time to start month only
            when charge_to_date is not null
                then charge_to_date
            when lower(trim(tenant_status)) = 'current'
                then null  -- open-ended
            when coalesce(move_out, lease_to) is not null
                then coalesce(move_out, lease_to)
            else null  -- open-ended fallback (matches app.py)
        end as effective_to_date
    from deduped_charges
),

-- ═══════════════════════════════════════════════════════════════════
-- MONTH SPINE (AI-GENERATED)
-- ═══════════════════════════════════════════════════════════════════
month_spine as (
    select dateadd(month, seq, '2020-01-01'::date) as month_start
    from (select seq4() as seq from table(generator(rowcount => 120)))
    where dateadd(month, seq, '2020-01-01'::date) <= date_trunc('month', current_date())
),

-- expand each charge into month-level rows
charge_months as (
    select
        pc.property_id,
        pc.property_name,
        pc.amount,
        pc.launch_date,
        pc.charge_type,
        ms.month_start
    from classified_charges pc
    cross join month_spine ms
    where ms.month_start >= date_trunc('month', pc.from_date)
      and ms.month_start <= date_trunc('month', coalesce(pc.effective_to_date, current_date()))
),

-- ═══════════════════════════════════════════════════════════════════
-- MONTHLY REVENUE — three versions:
--   all charges, recurring only, one-time only
-- ═══════════════════════════════════════════════════════════════════
monthly_all as (
    select property_id, property_name, launch_date, month_start,
           sum(amount) as monthly_revenue
    from charge_months
    group by 1, 2, 3, 4
),

monthly_recurring as (
    select property_id, property_name, launch_date, month_start,
           sum(amount) as monthly_revenue
    from charge_months
    where charge_type = 'recurring'
    group by 1, 2, 3, 4
),

-- ═══════════════════════════════════════════════════════════════════
-- LIFT CALC — parameterized as a macro pattern, run for both
-- ═══════════════════════════════════════════════════════════════════

-- ---- ALL CHARGES ----
classified_all as (
    select *, case
        when launch_date is null then 'no_launch'
        when month_start < date_trunc('month', launch_date) then 'pre'
        else 'post'
    end as period
    from monthly_all
),

pre_baseline_all as (
    select property_id, property_name,
           avg(monthly_revenue) as pre_avg,
           count(*) as pre_months
    from (
        select *, row_number() over (
            partition by property_id order by month_start desc
        ) as rn
        from classified_all where period = 'pre'
    ) where rn <= 6
    group by 1, 2
),

post_all_all as (
    select property_id, property_name, launch_date,
           sum(monthly_revenue) as post_total,
           count(*) as post_months
    from classified_all where period = 'post'
    group by 1, 2, 3
),

post_recent_all as (
    select property_id, property_name,
           avg(monthly_revenue) as post_recent_avg,
           count(*) as recent_post_months
    from (
        select *, row_number() over (
            partition by property_id order by month_start desc
        ) as rn
        from classified_all
        where period = 'post'
          and month_start < date_trunc('month', current_date())
    ) where rn <= 6
    group by 1, 2
),

final_all as (
    select
        pa.property_id, pa.property_name, pa.launch_date,
        'all_charges' as charge_scope,
        coalesce(b.pre_months, 0) as pre_months,
        coalesce(pr.recent_post_months, 0) as recent_post_months,
        pa.post_months as total_post_months,
        round(coalesce(b.pre_avg, 0), 2) as pre_avg_monthly,
        round(coalesce(pr.post_recent_avg, 0), 2) as current_avg_monthly,
        round(coalesce(pr.post_recent_avg, 0) - coalesce(b.pre_avg, 0), 2) as monthly_lift,
        round(pa.post_total - (coalesce(b.pre_avg, 0) * pa.post_months), 2) as cumulative_impact,
        case when coalesce(b.pre_months, 0) >= 3 then 'reliable' else 'insufficient' end as baseline_quality,
        case
            when coalesce(pr.post_recent_avg, 0) <= 0 then true
            when coalesce(b.pre_avg, 0) >= coalesce(pr.post_recent_avg, 0) * 0.02 then true
            else false
        end as baseline_meaningful
    from post_all_all pa
    left join pre_baseline_all b on pa.property_id = b.property_id
    left join post_recent_all pr on pa.property_id = pr.property_id
),

-- ---- RECURRING ONLY ----
classified_rec as (
    select *, case
        when launch_date is null then 'no_launch'
        when month_start < date_trunc('month', launch_date) then 'pre'
        else 'post'
    end as period
    from monthly_recurring
),

pre_baseline_rec as (
    select property_id, property_name,
           avg(monthly_revenue) as pre_avg,
           count(*) as pre_months
    from (
        select *, row_number() over (
            partition by property_id order by month_start desc
        ) as rn
        from classified_rec where period = 'pre'
    ) where rn <= 6
    group by 1, 2
),

post_all_rec as (
    select property_id, property_name, launch_date,
           sum(monthly_revenue) as post_total,
           count(*) as post_months
    from classified_rec where period = 'post'
    group by 1, 2, 3
),

post_recent_rec as (
    select property_id, property_name,
           avg(monthly_revenue) as post_recent_avg,
           count(*) as recent_post_months
    from (
        select *, row_number() over (
            partition by property_id order by month_start desc
        ) as rn
        from classified_rec
        where period = 'post'
          and month_start < date_trunc('month', current_date())
    ) where rn <= 6
    group by 1, 2
),

final_rec as (
    select
        pa.property_id, pa.property_name, pa.launch_date,
        'recurring_only' as charge_scope,
        coalesce(b.pre_months, 0) as pre_months,
        coalesce(pr.recent_post_months, 0) as recent_post_months,
        pa.post_months as total_post_months,
        round(coalesce(b.pre_avg, 0), 2) as pre_avg_monthly,
        round(coalesce(pr.post_recent_avg, 0), 2) as current_avg_monthly,
        round(coalesce(pr.post_recent_avg, 0) - coalesce(b.pre_avg, 0), 2) as monthly_lift,
        round(pa.post_total - (coalesce(b.pre_avg, 0) * pa.post_months), 2) as cumulative_impact,
        case when coalesce(b.pre_months, 0) >= 3 then 'reliable' else 'insufficient' end as baseline_quality,
        case
            when coalesce(pr.post_recent_avg, 0) <= 0 then true
            when coalesce(b.pre_avg, 0) >= coalesce(pr.post_recent_avg, 0) * 0.02 then true
            else false
        end as baseline_meaningful
    from post_all_rec pa
    left join pre_baseline_rec b on pa.property_id = b.property_id
    left join post_recent_rec pr on pa.property_id = pr.property_id
),

-- ═══════════════════════════════════════════════════════════════════
-- IMPORTANT: side-by-side comparison
-- ═══════════════════════════════════════════════════════════════════
combined as (
    select * from final_all
    union all
    select * from final_rec
)

-- aggregate summary — comparable properties only
select
    charge_scope,
    count(*) as comparable_properties,
    round(sum(monthly_lift), 2) as agg_monthly_lift,
    round(sum(cumulative_impact), 2) as agg_cumulative_impact,
    round(avg(pre_avg_monthly), 2) as avg_pre_baseline,
    round(avg(current_avg_monthly), 2) as avg_current_monthly
from combined
where baseline_quality = 'reliable'
  and baseline_meaningful = true
group by 1
order by 1;

-- ═══════════════════════════════════════════════════════════════════
-- UNCOMMENT FOR PROPERTY-LEVEL DETAIL:
-- see which properties look different between all vs recurring
-- ═══════════════════════════════════════════════════════════════════

-- select
--     a.property_id,
--     a.property_name,
--     a.pre_avg_monthly  as all_pre,
--     a.current_avg_monthly as all_current,
--     a.monthly_lift      as all_monthly_lift,
--     a.cumulative_impact as all_cumulative,
--     r.pre_avg_monthly   as rec_pre,
--     r.current_avg_monthly as rec_current,
--     r.monthly_lift       as rec_monthly_lift,
--     r.cumulative_impact  as rec_cumulative,
--     a.monthly_lift - r.monthly_lift as deposit_effect
-- from final_all a
-- join final_rec r on a.property_id = r.property_id
-- where a.baseline_quality = 'reliable'
--   and a.baseline_meaningful = true
-- order by deposit_effect desc;
