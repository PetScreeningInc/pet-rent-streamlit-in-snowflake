# Pet Rent Analysis — Data Engineering Reference

> **Purpose:** This document details every data source, SQL query, API call, join, filter, grouping, and transformation used in the Pet Rent Analysis application. It is intended for data engineers who need to replicate or audit the logic.

---

## Table of Contents

0. [The Core Idea: How API Data Joins to Snowflake (dbt-style CTEs)](#0-the-core-idea-how-api-data-joins-to-snowflake)
1. [Data Sources Overview](#1-data-sources-overview)
2. [Snowflake Tables Used](#2-snowflake-tables-used)
3. [API Data Retrieval — Yardi](#3-api-data-retrieval--yardi)
4. [API Data Retrieval — Entrata](#4-api-data-retrieval--entrata)
5. [Snowflake: Property & Integration Queries](#5-snowflake-property--integration-queries)
6. [Charge Code Selection & Auto-Detection](#6-charge-code-selection--auto-detection)
7. [Monthly Revenue Aggregation Logic](#7-monthly-revenue-aggregation-logic)
8. [Missing Pet Rent — Confirmed Profiles](#8-missing-pet-rent--confirmed-profiles)
9. [Suspected Undisclosed Pets](#9-suspected-undisclosed-pets)
10. [Paying Tenant Matching Logic](#10-paying-tenant-matching-logic)
11. [Revenue Estimation Methodology](#11-revenue-estimation-methodology)
12. [Entrata-Specific Handling](#12-entrata-specific-handling)
13. [Compliance & Adoption Data](#13-compliance--adoption-data)
14. [Junk Email Exclusion List](#14-junk-email-exclusion-list)
15. [End-to-End Data Flow](#15-end-to-end-data-flow)

---

## 0. The Core Idea: How API Data Joins to Snowflake

The live API data (Yardi or Entrata) is **never written to Snowflake**. It lives in memory as a Python DataFrame. Think of it as a `{{ source() }}` in dbt that just happens to come from an API instead of a raw table.

The app runs Snowflake queries to pull PetScreening profile data, brings the results back to Python, and joins the two worlds **in Python** — using the same logic you'd write in a dbt model with CTEs.

Below is the entire join logic written as **dbt-style models with CTEs** so your team can replicate it. If you ever materialize the API data into Snowflake (e.g., via the existing `raw.yardi_getrentroll_new`), these models could run as real dbt models with no changes.

---

### Source: `stg_api_charges`

If this were a dbt source, it would be the flattened output from the Yardi/Entrata API. One row per tenant per charge line. This is equivalent to your existing `stg_pmc_integrations_yardi__getrentroll_new`.

| Column | Example |
|--------|---------|
| `property_id` | `94672` |
| `property_name` | `The Villas at Waterchase` |
| `property_code` | `water` |
| `tenant_code` | `t0012345` |
| `email` | `john.doe@gmail.com` |
| `unit_code` | `101` |
| `charge_code` | `PetRent` |
| `charge_amount` | `35.00` |
| `charge_from_date` | `2024-01-01` |
| `charge_to_date` | `2025-12-31` |
| `tenant_status` | `Current` |
| `move_in` | `2023-06-15` |
| `move_out` | (null) |
| `lease_from` | `2023-06-15` |
| `lease_to` | `2025-06-14` |
| `lease_id` | (Entrata only) `98765` |

---

### Model 1: `int_paying_tenants` — Who is paying pet rent?

This model works entirely from `stg_api_charges`. It identifies every tenant considered "paying" through direct charges, unit-level expansion (roommates), and lease-level expansion (co-tenants on the same Entrata lease).

```sql
-- models/intermediate/int_paying_tenants.sql

with api_charges as (

    select * from {{ ref('stg_api_charges') }}

),

-- Tenants who directly have a selected pet charge
direct_payers as (

    select distinct
        trim(cast(property_id as varchar))          as property_id,
        upper(trim(cast(tenant_code as varchar)))   as tenant_code,
        lower(trim(cast(email as varchar)))          as email,
        upper(trim(cast(unit_code as varchar)))     as unit_code,
        trim(cast(lease_id as varchar))              as lease_id  -- Entrata only, NULL for Yardi
    from api_charges
    where charge_code in ({{ var('selected_charge_codes') }})

),

-- Units that have at least one pet charge.
-- If ANY tenant on a unit pays pet rent, ALL tenants on that unit count as
-- paying. This handles roommates — the charge is on one person but covers
-- the whole unit.
paying_units as (

    select distinct
        property_id,
        unit_code
    from direct_payers
    where unit_code is not null
      and unit_code not in ('', 'NAN', 'NONE')

),

-- Expand: find ALL tenants who live on a paying unit
unit_expanded_tenants as (

    select distinct
        trim(cast(a.property_id as varchar))          as property_id,
        upper(trim(cast(a.tenant_code as varchar)))   as tenant_code,
        lower(trim(cast(a.email as varchar)))          as email
    from api_charges a
    inner join paying_units pu
        on trim(cast(a.property_id as varchar)) = pu.property_id
       and upper(trim(cast(a.unit_code as varchar))) = pu.unit_code

),

-- Leases that have at least one pet charge (Entrata only).
-- If ANY customer on the same lease pays, ALL customers on that lease count.
-- Entrata ties charges to the lease, not the individual customer.
paying_leases as (

    select distinct
        property_id,
        lease_id
    from direct_payers
    where lease_id is not null
      and lease_id not in ('', 'nan', 'None')

),

-- Expand: find ALL customers on a paying lease
lease_expanded_tenants as (

    select distinct
        trim(cast(a.property_id as varchar))          as property_id,
        upper(trim(cast(a.tenant_code as varchar)))   as tenant_code,
        lower(trim(cast(a.email as varchar)))          as email
    from api_charges a
    inner join paying_leases pl
        on trim(cast(a.property_id as varchar)) = pl.property_id
       and trim(cast(a.lease_id as varchar)) = pl.lease_id

),

-- Union all paying tenants: direct + unit-expanded + lease-expanded
all_paying_tenants as (

    select property_id, tenant_code, email from direct_payers
    union
    select property_id, tenant_code, email from unit_expanded_tenants
    union
    select property_id, tenant_code, email from lease_expanded_tenants

),

-- Deduplicated lookup by tenant_code
paying_by_tenant_code as (

    select distinct
        property_id,
        tenant_code
    from all_paying_tenants
    where tenant_code is not null
      and tenant_code not in ('', 'NAN', 'NONE')

),

-- Deduplicated lookup by email (fallback when tenant_code doesn't match)
paying_by_email as (

    select distinct
        property_id,
        email
    from all_paying_tenants
    where email is not null
      and email not in ('', 'nan', 'none')

)

-- Downstream models ref either paying_by_tenant_code or paying_by_email
select 'tc' as match_type, property_id, tenant_code as match_key from paying_by_tenant_code
union all
select 'email' as match_type, property_id, email as match_key from paying_by_email
```

---

### Model 2: `int_ps_household_profiles` — PetScreening profiles with tenant codes

This runs in Snowflake. It pulls all PetScreening users who have a compliant, active household pet — and extracts their `tenant_code` from the lease JSON so we have a join key back to the API data.

```sql
-- models/intermediate/int_ps_household_profiles.sql

with profiles as (

    select distinct
        du.property_id,
        p.property_name,

        -- The tenant_code is stored as JSON in f_leases.lease_source_external_id.
        -- Yardi uses :tenant_code, Entrata uses :"customerId" or :"customer_id"
        coalesce(
            l.lease_source_external_id:tenant_code::string,
            l.lease_source_external_id:"customerId"::string,
            l.lease_source_external_id:"customer_id"::string
        ) as tenant_code,

        ue.user_email,
        ue.user_first_name,
        ue.user_last_name,
        ue.compliance_status,
        ue.user_pet_type,
        ue.user_pet_status,
        ue.user_profile_url

    from {{ ref('d_units') }} du

    join {{ ref('d_properties') }} p
        on du.property_id = p.property_id

    join {{ ref('petscreening__user_enriched') }} ue
        on ue.unit_id = du.unit_id

    join {{ ref('f_leases') }} l
        on du.unit_key = l.unit_key
       and l.user_key = ue.user_key

    where du.unit_source = '{{ var("pmc_system") }}'  -- 'yardi' or 'entrata'
      and du.property_id in ({{ var("property_ids") }})

      -- Only compliant profiles with an active household pet
      and ue.compliance_status = 'compliant'
      and ue.user_pet_type = 'household'
      and ue.user_pet_status = 'active'

      -- Exclude junk/placeholder emails
      and ue.user_email is not null
      and trim(ue.user_email) <> ''
      and lower(trim(ue.user_email)) not in (
          'none@none.com', 'noemail@noemail.com', 'none@nowhere.com',
          'none@gmail.com', 'noemail@gmail.com', 'na@na.com',
          'no@email.com', 'na@gmail.com', 'noemail@greystar.com',
          'no@no.com', 'n/a@gmail.com', 'none@aol.com',
          'none@embreydc.com', 'noemail@comcapp.com'
      )

)

select * from profiles
```

---

### Model 3: `int_missing_pet_rent` — The core join (API meets Snowflake)

This is where the two worlds meet. `int_paying_tenants` (from API data) is left-joined to `int_ps_household_profiles` (from Snowflake) to find who has a pet but isn't paying.

```sql
-- models/intermediate/int_missing_pet_rent.sql
--
-- NOTE: In the app, this join happens in Python because int_paying_tenants
-- comes from the live API. If you materialize the API data into Snowflake,
-- this becomes a standard dbt model.

with ps_profiles as (

    select * from {{ ref('int_ps_household_profiles') }}

),

paying_by_tc as (

    select distinct property_id, match_key as tenant_code
    from {{ ref('int_paying_tenants') }}
    where match_type = 'tc'

),

paying_by_email as (

    select distinct property_id, match_key as email
    from {{ ref('int_paying_tenants') }}
    where match_type = 'email'

),

-- Step 1: Try to match each PS profile to the paying set.
--         Primary join key: (property_id, tenant_code)
--         Fallback join key: (property_id, email)
profiles_with_paying_flag as (

    select
        prof.*,
        case
            when ptc.tenant_code is not null then 1
            when pte.email is not null then 1
            else 0
        end as pet_rent_paid_raw

    from ps_profiles prof

    left join paying_by_tc ptc
        on trim(cast(prof.property_id as varchar)) = ptc.property_id
       and upper(trim(cast(prof.tenant_code as varchar))) = ptc.tenant_code

    left join paying_by_email pte
        on trim(cast(prof.property_id as varchar)) = pte.property_id
       and lower(trim(prof.user_email)) = pte.email

),

-- Step 2: Cross-lease propagation.
--         A user can have multiple rows from multiple f_leases entries.
--         If ANY row for the same (property_id, email) matched as paying,
--         ALL rows for that user at that property should be paying too.
--
--         Why? John might have lease A (tenant_code matches → paying) and
--         lease B (different tenant_code → no match). Without propagation,
--         lease B would falsely show as "missing pet rent".
user_level_paying as (

    select
        property_id,
        lower(trim(user_email)) as user_email,
        max(pet_rent_paid_raw) as is_paying
    from profiles_with_paying_flag
    group by 1, 2

),

profiles_with_propagated_flag as (

    select
        prof.*,
        coalesce(ulp.is_paying, prof.pet_rent_paid_raw) as pet_rent_paid
    from profiles_with_paying_flag prof
    left join user_level_paying ulp
        on prof.property_id = ulp.property_id
       and lower(trim(prof.user_email)) = ulp.user_email

),

-- Step 3 (Entrata only): Freshness filter.
--         Remove PS profiles whose email doesn't exist in the current API data.
--         Prevents stale profiles (moved-out residents not yet cleaned up in
--         PetScreening) from appearing as false positives.
--         For Yardi this CTE passes everything through — Yardi's API date
--         parameters already filter out old tenants.
api_emails as (

    select distinct
        trim(cast(property_id as varchar)) as property_id,
        lower(trim(email))                  as email
    from {{ ref('stg_api_charges') }}
    where email is not null
      and trim(email) <> ''
      and lower(trim(email)) not in ('nan', 'none')

),

freshness_filtered as (

    select prof.*
    from profiles_with_propagated_flag prof
    where
        prof.pet_rent_paid = 1
        or exists (
            select 1 from api_emails ae
            where ae.property_id = trim(cast(prof.property_id as varchar))
              and ae.email = lower(trim(prof.user_email))
        )

),

-- FINAL: The missing pet rent population
-- pet_rent_paid = 0 means: has a PetScreening household pet, NOT paying
-- any of the selected charge codes
missing_pet_rent as (

    select
        *,
        'Profile_No_Rent' as overall_status
    from freshness_filtered
    where pet_rent_paid = 0

)

select * from missing_pet_rent
```

---

### Model 4: `rpt_missing_pet_rent` — Enrich with pet details for the downloadable report

The missing tenants from Model 3 are inner-joined to `r_monthly_executive_summary` to get per-pet detail rows (pet name, breed, species, unit address, profile URL).

```sql
-- models/reporting/rpt_missing_pet_rent.sql

with missing_tenants as (

    select distinct
        lower(trim(user_email)) as email,
        cast(property_id as varchar) as property_id
    from {{ ref('int_missing_pet_rent') }}

),

exec_summary as (

    select distinct
        m.pet_profile_url,
        m.pet_id,
        m.pet_name,
        m.breed,
        m.species,
        m.user_first_name,
        m.user_last_name,
        m.user_email,
        m.unit_id,
        m.lease_start_date,
        m.lease_end_date,
        m.resident_type,
        m.unit_address_1,
        m.unit_address_2,
        concat_ws(' ', m.unit_address_1, m.unit_address_2) as full_unit_address,
        m.property_name,
        m.property_id,
        m.pet_level_compliance_status,
        m.user_level_compliance_status,
        m.pet_profile_type,
        m.pet_profile_status,
        m.property_source_name

    from {{ ref('r_monthly_executive_summary') }} m

    where m.property_id in ({{ var("property_ids") }})
      and m.pet_profile_type = 'household'
      and m.pet_profile_status = 'active'
      and m.property_source_name = '{{ var("pmc_system") }}'

),

-- Inner join: keep only exec summary rows for tenants identified as missing
final as (

    select
        e.*,
        1 as has_petscreening_pets,
        0 as pet_rent_paid,
        'Profile_No_Rent' as overall_status

    from exec_summary e
    inner join missing_tenants mt
        on lower(trim(e.user_email)) = mt.email
       and cast(e.property_id as varchar) = mt.property_id

)

select * from final
```

---

### Model 5: `int_ps_suspected_profiles` — Suspected undisclosed pets

Same join pattern as `int_ps_household_profiles` (Model 2), but targets a **different population** — residents who show behavioral signals of having an undisclosed pet. The rest of the flow (paying set join, propagation, freshness filter) is identical to Model 3.

```sql
-- models/intermediate/int_ps_suspected_profiles.sql
-- (replaces int_ps_household_profiles for the suspected undisclosed flow)

with suspected_profiles as (

    select distinct
        du.property_id,
        p.property_name,
        coalesce(
            l.lease_source_external_id:tenant_code::string,
            l.lease_source_external_id:"customerId"::string,
            l.lease_source_external_id:"customer_id"::string
        ) as tenant_code,
        ue.user_first_name,
        ue.user_last_name,
        ue.user_email,
        ue.user_profile_url,
        ue.user_pet_type,
        ue.user_pet_status,
        ue.compliance_status,
        ue.household_profile_started_alltime,
        ue.assistance_profile_started_alltime,

        -- Categorize WHY we think they have an undisclosed pet
        case
            when ue.household_profile_started_alltime > 0
                 and not (ue.user_pet_type = 'household' and ue.user_pet_status = 'active')
            then 'Abandoned household profile'

            when ue.assistance_profile_started_alltime > 0
                 and ue.user_pet_type = 'assistance'
                 and ue.user_pet_status in ('draft', 'non_responsive', 'declined',
                                             'not_recommended', 'returned')
            then 'Unresolved assistance request'

            when ue.assistance_profile_started_alltime > 0
                 and ue.user_pet_type = 'not_pet'
            then 'No-pet after assistance started'

            else 'Other suspected'
        end as suspected_reason

    from {{ ref('d_units') }} du

    join {{ ref('d_properties') }} p
        on du.property_id = p.property_id

    join {{ ref('petscreening__user_enriched') }} ue
        on ue.unit_id = du.unit_id

    join {{ ref('f_leases') }} l
        on du.unit_key = l.unit_key
       and l.user_key = ue.user_key

    -- LEFT JOINs needed to check pet_profile-level exclusions
    left join {{ ref('f_user_pets') }} up
        on up.user_key = ue.user_key

    left join {{ ref('d_pet_profiles') }} pp
        on pp.pet_key = up.pet_key

    where du.unit_source = '{{ var("pmc_system") }}'
      and du.property_id in ({{ var("property_ids") }})

      -- Broader than confirmed missing: includes non-compliant
      and ue.compliance_status in ('compliant', 'non_compliant')

      -- Exclude legitimate approved or expired assistance animals
      -- (at both the pet_profile level and user_enriched level)
      and not (
          coalesce(pp.pet_profile_kind, '') = 'assistance'
          and coalesce(pp.pet_profile_status, '') in ('recommended', 'expired')
      )
      and not (
          ue.user_pet_type = 'assistance'
          and ue.user_pet_status in ('recommended', 'expired')
      )

      -- The behavioral signals that suggest an undisclosed pet:
      and (
            -- Pattern 1: Started a household profile but never completed it
            (
              ue.household_profile_started_alltime > 0
              and not (ue.user_pet_type = 'household' and ue.user_pet_status = 'active')
            )
            or
            -- Pattern 2: Started an assistance animal request, never resolved
            (
              ue.assistance_profile_started_alltime > 0
              and (
                    (ue.user_pet_type = 'assistance'
                     and ue.user_pet_status in ('draft', 'non_responsive', 'declined',
                                                 'not_recommended', 'returned'))
                    or
                    (ue.user_pet_type = 'not_pet')
                  )
            )
          )

      -- Exclude archived profiles
      and coalesce(pp.pet_profile_archive_reason, '') = ''

      -- Exclude junk emails
      and ue.user_email is not null
      and trim(ue.user_email) <> ''
      and lower(trim(ue.user_email)) not in (
          'none@none.com', 'noemail@noemail.com', 'none@nowhere.com',
          'none@gmail.com', 'noemail@gmail.com', 'na@na.com',
          'no@email.com', 'na@gmail.com', 'noemail@greystar.com',
          'no@no.com', 'n/a@gmail.com', 'none@aol.com',
          'none@embreydc.com', 'noemail@comcapp.com'
      )

)

select * from suspected_profiles

-- After this, the same int_missing_pet_rent pattern applies:
-- → left join int_paying_tenants (exclude already-paying)
-- → user_level_paying propagation
-- → freshness_filtered (Entrata only)
-- → WHERE pet_rent_paid = 0 → suspected undisclosed population
```

---

### dbt DAG — How it all connects

```
{{ source('yardi/entrata', 'api_charges') }}    {{ ref('d_units') }}
         │                                        │
         ▼                                        ▼
┌─────────────────────┐              ┌──────────────────────────────────┐
│ int_paying_tenants  │              │ int_ps_household_profiles        │
│                     │              │ (OR int_ps_suspected_profiles)   │
│ CTEs:               │              │                                  │
│ • direct_payers     │              │ d_units                          │
│ • paying_units      │              │   → d_properties                 │
│ • unit_expanded     │              │   → petscreening__user_enriched  │
│ • paying_leases     │              │   → f_leases                     │
│ • lease_expanded    │              │   → (f_user_pets, d_pet_profiles │
│ • union all + dedup │              │      for suspected only)         │
│                     │              │                                  │
│ Output:             │              │ Filters:                         │
│  paying_by_tc       │              │  confirmed: compliant +          │
│  paying_by_email    │              │    household + active            │
└────────┬────────────┘              │  suspected: compliant/non_comp + │
         │                           │    abandoned/unresolved signals  │
         │                           └──────────────┬───────────────────┘
         │                                          │
         │   LEFT JOIN on (property_id, tenant_code)│
         │   LEFT JOIN on (property_id, email)      │
         └──────────────────┬───────────────────────┘
                            │
                            ▼
              ┌──────────────────────────────┐
              │ int_missing_pet_rent         │
              │ (or int_suspected_undisclosed)│
              │                              │
              │ CTEs:                        │
              │ • profiles_with_paying_flag  │
              │ • user_level_paying          │
              │   (cross-lease propagation)  │
              │ • freshness_filtered         │
              │   (Entrata stale profile     │
              │    removal)                  │
              │ • WHERE pet_rent_paid = 0    │
              └──────────────┬───────────────┘
                             │
                             │  INNER JOIN on (email, property_id)
                             ▼
              ┌──────────────────────────────┐
              │ rpt_missing_pet_rent         │
              │                              │
              │ r_monthly_executive_summary  │
              │ → pet_name, breed, species   │
              │ → unit_address, profile_url  │
              │ → overall_status =           │
              │   'Profile_No_Rent'          │
              │   or 'Suspected_Undisclosed' │
              └──────────────────────────────┘
```

---

## 1. Data Sources Overview

The application combines two data sources:

| Source | What it provides |
|--------|-----------------|
| **Yardi GetRentroll SOAP API** (live) | Per-property rent roll: every tenant and their lease charges (charge code, amount, date range) |
| **Entrata getLeases REST API** (live) | Per-property lease data: customers, scheduled charges, AR transactions, lease intervals |
| **Snowflake** (PROD database) | Property metadata, PetScreening profiles, lease linkages, executive summary, compliance/adoption KPIs |

The live API data replaces the dbt staging models (`stg_pmc_integrations_yardi__getrentroll_new` and `ht_pmc_integrations_yardi__getrentroll_new`). The app performs the same joins in Python that dbt would do in SQL.

---

## 2. Snowflake Tables Used

| Table | Schema | Purpose |
|-------|--------|---------|
| `D_PROPERTIES` | `PROD.COMMON` | Property master data — `property_id`, `property_name`, `parent_company_name`, `parent_company_ancestry_id`, `property_source_name`, `property_source_id`, `integration_id`, `integration_status`, `property_status` |
| `STG_PETSCREENING__INTEGRATIONS` | `PROD.STAGING` | API credentials per integration — `integration_id`, `system` (yardi/entrata), `state`, `settings` (JSON containing user_name, password, server_name, database_name, resident_data_url for Yardi; corp_id for Entrata) |
| `STG_PETSCREENING__UNITS` | `PROD.STAGING` | Property code mapping — `SOURCE_EXTERNAL_ID` contains `property_code` in JSON |
| `PETSCREENING__PROPERTY_KEY_FACTS` | `PROD.PETSCREENING` | PetScreening launch dates per property — `property_id`, `property_launch_date` |
| `D_UNITS` | `PROD.COMMON` | Unit dimension — `unit_id`, `unit_key`, `property_id`, `unit_source`, `unit_address_1`, `unit_address_2` |
| `PETSCREENING__USER_ENRICHED` | `PROD.PETSCREENING` | PetScreening user profiles — `user_key`, `unit_id`, `user_email`, `user_first_name`, `user_last_name`, `compliance_status`, `user_pet_type`, `user_pet_status`, `user_profile_url`, `household_profile_started_alltime`, `assistance_profile_started_alltime` |
| `F_LEASES` | `PROD.COMMON` | Lease facts — `unit_key`, `user_key`, `lease_source_external_id` (JSON containing `tenant_code` for Yardi, `customerId`/`customer_id` for Entrata), `lease_start_date`, `lease_end_date` |
| `R_MONTHLY_EXECUTIVE_SUMMARY` | `PROD.REPORTING` | Detailed pet/profile data — `pet_profile_url`, `pet_id`, `pet_name`, `breed`, `species`, `pet_profile_type`, `pet_profile_status`, `pet_level_compliance_status`, `user_level_compliance_status`, `property_source_name` |
| `R_QUARTERLY_BUSINESS_REVIEW_REPORTING` | `PROD.REPORTING` | Adoption KPIs — `PROPERTY_ID`, `PERIOD_MONTH`, `ACTIVE_UNITS`, `TOTAL_UNITS`, `ACTIVE_USERS`, `TOTAL_USERS`, `AFTER_PROPERTY_LAUNCH_FLAG` |
| `F_USER_PETS` | `PROD.COMMON` | User-to-pet linkage — `user_key`, `pet_key` |
| `D_PET_PROFILES` | `PROD.COMMON` | Pet profile details — `pet_key`, `pet_profile_kind`, `pet_profile_status`, `pet_profile_archive_reason` |

---

## 3. API Data Retrieval — Yardi

### SOAP Endpoint

- **Action:** `http://tempuri.org/YSI.Interfaces.WebServices/ItfResidentData/GetRentroll`
- **Content-Type:** `text/xml; charset=utf-8`
- **Timeout:** 120 seconds per property

### SOAP Payload Template

```xml
<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
               xmlns:xsd="http://www.w3.org/2001/XMLSchema"
               xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <GetRentroll xmlns="http://tempuri.org/YSI.Interfaces.WebServices/ItfResidentData">
        <UserName>{USER_NAME from integration settings}</UserName>
        <Password>{PASSWORD from integration settings}</Password>
        <ServerName>{SERVER_NAME from integration settings}</ServerName>
        <Database>{DATABASE_NAME from integration settings}</Database>
        <Platform>Yardi</Platform>
        <InterfaceEntity>PetScreening</InterfaceEntity>
        <InterfaceLicense>{YARDI_LICENSE_TOKEN from .env}</InterfaceLicense>
        <YardiPropertyId>{PROPERTY_CODE}</YardiPropertyId>
        <MoveIn>{10 years ago, YYYY-MM-DD}</MoveIn>
        <MoveOut>{10 years ago, YYYY-MM-DD}</MoveOut>
        <LeaseChgFrom>2000-01-01</LeaseChgFrom>
        <LeaseChgTo>{today, YYYY-MM-DD}</LeaseChgTo>
    </GetRentroll>
  </soap:Body>
</soap:Envelope>
```

**Important:** The API date parameters are ALWAYS set to the widest useful range (10 years back for MoveIn/MoveOut, 2000-01-01 for LeaseChgFrom) regardless of the display lookback slider. This guarantees consistent data regardless of what display window the user picks.

### Yardi XML Response Structure

```
Envelope > Body > GetRentrollResponse > GetRentrollResult > XmlDocument > Properties > Property
  └─ Units > Unit (one per unit)
       ├─ UnitCode, UnitType, MarketRent
       └─ Tenants > Tenant (one per tenant)
            ├─ TenantCode, FirstName, LastName, TenantStatus
            ├─ LeaseFrom, LeaseTo, MoveIn, MoveOut, Email
            └─ LeaseCharges > LeaseCharge (one per charge line)
                 ├─ ChargeCode, ChargeType, ChargeAmount
                 └─ FromDate, ToDate
```

### Flattened Output Columns (per charge row)

Each XML response is flattened into rows with these columns:

| Column | Source |
|--------|--------|
| `parent_company` | From Snowflake property query |
| `property_id` | From Snowflake property query |
| `property_name` | From Snowflake property query |
| `property_code` | From Snowflake property query |
| `launch_date` | From `PETSCREENING__PROPERTY_KEY_FACTS` |
| `unit_code` | XML: `Unit > UnitCode` |
| `unit_type` | XML: `Unit > UnitType` |
| `market_rent` | XML: `Unit > MarketRent` |
| `tenant_code` | XML: `Tenant > TenantCode` |
| `first_name` | XML: `Tenant > FirstName` |
| `last_name` | XML: `Tenant > LastName` |
| `tenant_status` | XML: `Tenant > TenantStatus` |
| `lease_from` | XML: `Tenant > LeaseFrom` |
| `lease_to` | XML: `Tenant > LeaseTo` |
| `move_in` | XML: `Tenant > MoveIn` |
| `move_out` | XML: `Tenant > MoveOut` |
| `email` | XML: `Tenant > Email` |
| `charge_code` | XML: `LeaseCharge > ChargeCode` |
| `charge_type` | XML: `LeaseCharge > ChargeType` |
| `charge_amount` | XML: `LeaseCharge > ChargeAmount` |
| `charge_from_date` | XML: `LeaseCharge > FromDate` |
| `charge_to_date` | XML: `LeaseCharge > ToDate` |

---

## 4. API Data Retrieval — Entrata

### REST Endpoint

```
POST https://apis.entrata.com/ext/orgs/{corp_id}/v1/leases?page_no={N}&per_page=500
```

- **Auth:** `X-Api-Key` header with `ENTRATA_API_KEY` from `.env`
- **Pagination:** 500 leases per page, up to 200 pages max
- **Timeout:** 120 seconds per request

### Request Body

```json
{
  "auth": {"type": "apikey"},
  "requestId": 1,
  "method": {
    "name": "getLeases",
    "version": "r1",
    "params": {
      "propertyId": "{entrata_property_id}",
      "includeArTransactions": 1,
      "includeLeaseHistory": 1,
      "includeScheduledCharges": 1,
      "leaseStatusTypeIds": "1,2,3,4,5,6"
    }
  }
}
```

`leaseStatusTypeIds: "1,2,3,4,5,6"` fetches all statuses for full history.

### Entrata-Specific Processing

Five key differences from Yardi:

1. **Interval filtering:** Only charges from lease intervals with status `Current`, `Past`, or `Notice` are included. Statuses `Cancelled`, `Applicant`, `Denied`, `Future` are dropped.

2. **Charge-to-interval linkage:** Each `scheduledCharge` carries a `leaseIntervalId`. Charges attached to cancelled/invalid intervals are skipped.

3. **Deduplication:** Charges belong to the *lease*, not individual customers. Every customer on the same lease gets identical charges. A `_entrata_charge_dedup_key` is stamped on each row: `{lease_id}|{interval_id}|{charge_code}|{charge_from_date}|{charge_amount}`. Revenue aggregation deduplicates on this key before summing.

4. **Frequency-based end-date logic:**
   - `One-Time` charges: `charge_to = charge_from` (same day, not spread across months)
   - `Monthly` charges with no `endDate`: fall back to the interval's `lease_to`

5. **Customer expansion for paying set:** All customers on a lease are emitted (not just primary), so roommates aren't falsely flagged as "missing pet rent". Revenue dedup happens via the dedup key.

### Flattened Output Columns

Same as Yardi, plus:

| Additional Column | Source |
|-------------------|--------|
| `lease_id` | Entrata lease `id` |
| `frequency` | `scheduledCharge > frequency` |
| `lease_interval_id` | `scheduledCharge > leaseIntervalId` |
| `_entrata_charge_dedup_key` | Computed: `{lease_id}\|{interval_id}\|{charge_code}\|{from_date}\|{amount}` |

---

## 5. Snowflake: Property & Integration Queries

### Query 1: Load Parent Companies (Yardi)

Returns parent companies that have at least one Yardi integration.

```sql
SELECT
    p.parent_company_name,
    MAX(p.parent_company_ancestry_id)    AS ancestry_id,
    MAX(p.parent_company_ancestry_name)  AS ancestry_name,
    COUNT(DISTINCT p.property_id)        AS total_props,
    COUNT(DISTINCT CASE
        WHEN i.integration_id IS NOT NULL AND i.system = 'yardi'
        THEN p.property_id
    END) AS api_props
FROM PROD.COMMON.D_PROPERTIES p
LEFT JOIN PROD.STAGING.STG_PETSCREENING__INTEGRATIONS i
    ON p.integration_id = i.integration_id
   AND i.system = 'yardi'
WHERE p.parent_company_name IS NOT NULL
GROUP BY 1
HAVING api_props > 0
ORDER BY 1
```

### Query 2: Load Parent Companies (Entrata)

Same structure but with additional filters for active integrations:

```sql
SELECT
    p.parent_company_name,
    MAX(p.parent_company_ancestry_id)    AS ancestry_id,
    MAX(p.parent_company_ancestry_name)  AS ancestry_name,
    COUNT(DISTINCT p.property_id)        AS total_props,
    COUNT(DISTINCT CASE
        WHEN i.integration_id IS NOT NULL AND i.system = 'entrata'
             AND i.state = 'enabled'
             AND p.integration_status = 'enabled'
             AND p.property_status = 'active'
        THEN p.property_id
    END) AS api_props
FROM PROD.COMMON.D_PROPERTIES p
LEFT JOIN PROD.STAGING.STG_PETSCREENING__INTEGRATIONS i
    ON p.integration_id = i.integration_id
   AND i.system = 'entrata'
WHERE p.parent_company_name IS NOT NULL
GROUP BY 1
HAVING api_props > 0
ORDER BY 1
```

### Query 3: Load Properties for API Fetch (Yardi)

Retrieves API credentials and property codes for selected properties:

```sql
SELECT DISTINCT
    i.integration_id,
    PARSE_JSON(i.settings):"resident_data_url"::STRING AS resident_data_url,
    PARSE_JSON(i.settings):"user_name"::STRING         AS user_name,
    PARSE_JSON(i.settings):"password"::STRING           AS password,
    PARSE_JSON(i.settings):"server_name"::STRING        AS server_name,
    PARSE_JSON(i.settings):"database_name"::STRING      AS database_name,
    p.property_id,
    p.property_name,
    p.parent_company_name,
    COALESCE(
        PARSE_JSON(u.SOURCE_EXTERNAL_ID):"property_code"::STRING,
        PARSE_JSON(p.PROPERTY_SOURCE_ID):"code"::STRING
    ) AS property_code,
    kf.property_launch_date
FROM PROD.COMMON.D_PROPERTIES p
JOIN PROD.STAGING.STG_PETSCREENING__INTEGRATIONS i
    ON p.integration_id = i.integration_id
LEFT JOIN PROD.STAGING.STG_PETSCREENING__UNITS u
    ON PARSE_JSON(u.SOURCE_EXTERNAL_ID):"property_code"::STRING =
       PARSE_JSON(p.property_source_id):"code"::STRING
LEFT JOIN PROD.PETSCREENING.PETSCREENING__PROPERTY_KEY_FACTS kf
    ON p.property_id = kf.property_id
WHERE i.system = 'yardi'
  AND p.property_source_name = 'yardi'
  AND p.parent_company_name = '{selected_parent_company}'
ORDER BY p.property_name
```

### Query 4: Load Properties for API Fetch (Entrata)

```sql
SELECT DISTINCT
    i.integration_id,
    PARSE_JSON(i.settings):"corp_id"::STRING AS corp_id,
    PARSE_JSON(p.property_source_id):"property_id"::STRING AS entrata_property_id,
    p.property_id          AS property_id,
    p.property_name        AS property_name,
    p.parent_company_name  AS parent_company_name,
    PARSE_JSON(p.property_source_id):"property_id"::STRING AS property_code,
    kf.property_launch_date AS property_launch_date
FROM PROD.COMMON.D_PROPERTIES p
JOIN PROD.STAGING.STG_PETSCREENING__INTEGRATIONS i
    ON p.integration_id = i.integration_id
LEFT JOIN PROD.PETSCREENING.PETSCREENING__PROPERTY_KEY_FACTS kf
    ON p.property_id = kf.property_id
WHERE i.system = 'entrata'
  AND p.property_source_name = 'entrata'
  AND i.state = 'enabled'
  AND p.integration_status = 'enabled'
  AND p.property_status = 'active'
  AND PARSE_JSON(p.property_source_id):"property_id"::STRING IS NOT NULL
  AND PARSE_JSON(i.settings):"corp_id"::STRING IS NOT NULL
  AND p.parent_company_name = '{selected_parent_company}'
ORDER BY p.property_name
```

---

## 6. Charge Code Selection & Auto-Detection

After fetching all charge data from the API, the app:

1. Extracts all unique `charge_code` values across all properties
2. **Auto-selects** codes containing any of these keywords (case-insensitive):
   - `pet`
   - `animal`
   - `petnr`
   - `concpet`
3. The user can manually add or remove charge codes

The selected charge codes drive everything downstream — revenue charts, missing pet rent identification, and suspected undisclosed analysis.

---

## 7. Monthly Revenue Aggregation Logic

### Date Parsing

Dates are parsed trying three formats in order: `%m/%d/%Y`, `%m-%d-%Y`, `%Y-%m-%d`.

### Effective End Date

For each charge line, the effective end date is:

```sql
coalesce(
    charge_to_date,
    case when tenant_status = 'Current'
         then null                            -- current tenants: still active
         else coalesce(move_out, lease_to)    -- past tenants: use move_out or lease_to
    end
) as effective_to_date
```

If `effective_to_date` is NULL (current tenant, no charge end date), the charge extends to the end of the display window.

### Monthly Bucketing

For each charge row where `from_date` is not null and `amount > 0`:

```
charge_start = first day of from_date's month
charge_end   = first day of effective_to_date's month (or end of display window if null)

For each month M in the display window:
    if charge_start <= M <= charge_end:
        monthly_portfolio[M] += amount
        monthly_portfolio_count[M] += 1
        monthly_by_property[property_name][M] += amount
```

**Key behavior:** A single charge line with a date range spanning multiple months contributes its full `charge_amount` to EVERY month in its range. This represents the monthly recurring fee the tenant pays.

### Entrata Deduplication (before aggregation)

For Entrata data, charges are deduplicated on `_entrata_charge_dedup_key` before aggregation. This prevents double-counting when multiple customers on the same lease share the same charge.

### Display Window

The display window is controlled by a lookback slider (6-60 months, default 24):

```
window_start = first of (today - lookback_months * 30 days) month
window_end   = first of current month
months       = pd.date_range(window_start, window_end, freq='MS')
```

---

## 8. Missing Pet Rent — Confirmed Profiles

See [Section 0, Models 2-4](#model-2-int_ps_household_profiles--petscreening-profiles-with-tenant-codes) for the complete dbt-style SQL.

Summary: tenants who have an active household pet profile in PetScreening (`compliant` + `household` + `active`) but are NOT being charged any of the selected pet fee codes. The join happens on `(property_id, tenant_code)` with email fallback, plus unit-level and lease-level expansion.

---

## 9. Suspected Undisclosed Pets

See [Section 0, Model 5](#model-5-int_ps_suspected_profiles--suspected-undisclosed-pets) for the complete dbt-style SQL.

Summary: residents who show behavioral signals of having an undisclosed pet. Three patterns:

| Category | Condition |
|----------|-----------|
| **Abandoned household profile** | `household_profile_started_alltime > 0` AND NOT (pet_type = household AND status = active) |
| **Unresolved assistance request** | `assistance_profile_started_alltime > 0` AND pet_type = assistance AND status IN (draft, non_responsive, declined, not_recommended, returned) |
| **No-pet after assistance started** | `assistance_profile_started_alltime > 0` AND pet_type = not_pet |

Same paying-set join and propagation logic as confirmed missing. Anyone already paying the selected charges is excluded.

---

## 10. Paying Tenant Matching Logic

See [Section 0, Model 1](#model-1-int_paying_tenants--who-is-paying-pet-rent) for the complete dbt-style SQL.

Summary of the matching cascade:

1. **Direct match:** tenant has a selected charge code
2. **Unit expansion:** ANY tenant on the unit has a pet charge → ALL tenants on that unit are paying
3. **Lease expansion (Entrata):** ANY customer on the lease has a pet charge → ALL customers on that lease are paying
4. **Match keys:** primary = `(property_id, upper(tenant_code))`, fallback = `(property_id, lower(email))`
5. **Cross-lease propagation:** if ANY row for `(property_id, email)` is paying, ALL rows for that user are paying

---

## 11. Revenue Estimation Methodology

### Charge Code Classification

Each `(property_name, charge_code)` is classified as **recurring** or **one-time**:

**Yardi:** Inferred from median date span of charges with that code:
- Median span > 60 days → `recurring`
- Median span <= 60 days → `onetime`

**Entrata:** Uses the explicit `frequency` field:
- More `One-Time` charges than `Monthly/Recurring` → `onetime`
- Otherwise → `recurring`

### Average Fee Calculation

Per property, split by recurring vs one-time:

```
For each property:
    For each tenant paying pet charges:
        For each charge line:
            if recurring: add amount to recurring_amounts
            if onetime: add amount to onetime_amounts
    avg_recurring = mean(recurring_amounts)
    avg_onetime = mean(onetime_amounts)
```

If a property has no payers, falls back to portfolio-wide average.

### Monthly Missing Revenue

For each missing tenant:

1. **Determine active period:**
   - Start: `move_in` or `lease_from` (default: property launch date if unknown)
   - End: `move_out` or `lease_to` (if current tenant or no end date: end of display window)

2. **Clamp to relevant window:**
   - Start: `max(active_from, launch_month, display_start)`
   - End: `min(active_to, display_end)`

3. **Apply fees per month:**
   - Recurring: add `avg_recurring` to every active month
   - One-time: add `avg_onetime` to the FIRST active month only

4. **Unique count:** Missing tenants are counted by unique `user_email`, not raw rows (which may have duplicates from multiple lease rows).

---

## 12. Entrata-Specific Handling

### Valid Lease Interval Statuses

| Included | Excluded |
|----------|----------|
| Current | Cancelled |
| Past | Applicant |
| Notice | Denied |
| (any other status) | Future |

### Charge-to-Interval Mapping

Each `scheduledCharge` has a `leaseIntervalId`. Only charges linked to valid intervals are included.

### One-Time vs Monthly Charges

```
if frequency == "One-Time":
    charge_to_date = charge_from_date   # don't spread across months
elif no endDate:
    charge_to_date = interval.lease_to  # monthly charges extend to lease end
```

### Customer Email Extraction

```
email = customer.email
    OR customer.addresses.address.email
    OR customer.addresses.address.additionalEmail
```

### Freshness Filter

For Entrata: profiles in Snowflake whose email is NOT found in the current API data are excluded from missing/suspected reports. This prevents stale PetScreening data from creating false positives. See the `freshness_filtered` CTE in [Model 3](#model-3-int_missing_pet_rent--the-core-join-api-meets-snowflake).

---

## 13. Compliance & Adoption Data

### Query

```sql
SELECT
    PROPERTY_ID,
    PROPERTY_NAME,
    PERIOD_MONTH,
    ACTIVE_UNITS,
    TOTAL_UNITS,
    ACTIVE_USERS,
    TOTAL_USERS
FROM PROD.REPORTING.R_QUARTERLY_BUSINESS_REVIEW_REPORTING
WHERE PROPERTY_ID IN ({property_ids})
  AND AFTER_PROPERTY_LAUNCH_FLAG = TRUE
  AND (TOTAL_UNITS > 0 OR TOTAL_USERS > 0)
ORDER BY PROPERTY_ID, PERIOD_MONTH
```

### Derived Metrics

```
unit_adoption     = ACTIVE_UNITS / TOTAL_UNITS
resident_adoption = ACTIVE_USERS / TOTAL_USERS
```

### Pre/Post Launch Analysis

For properties with a launch date:

```
pre_baseline_months = last 3 months before launch (or fewer if not available)
pre_avg             = mean revenue of pre_baseline_months
post_months         = all months >= launch_month
post_monthly_avg    = total post revenue / count of post months
diff_monthly        = post_monthly_avg - pre_avg
diff_total          = diff_monthly * count of post months
```

- The launch month itself is considered **post-launch** (PetScreening was active for at least part of the month)
- `baseline_reliable = True` only if there are >= 3 pre-launch months available

---

## 14. Junk Email Exclusion List

The following emails are excluded from all profile queries:

```
none@none.com
noemail@noemail.com
none@nowhere.com
none@gmail.com
noemail@gmail.com
na@na.com
no@email.com
na@gmail.com
noemail@greystar.com
no@no.com
n/a@gmail.com
none@aol.com
none@embreydc.com
noemail@comcapp.com
```

---

## 15. End-to-End Data Flow

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ STEP 1: SELECT PROPERTIES                                                    │
│                                                                              │
│ User picks: Parent Company Name / Ancestry ID / Property ID                  │
│ System: Yardi or Entrata                                                     │
│                                                                              │
│ Snowflake → D_PROPERTIES + STG_PETSCREENING__INTEGRATIONS                    │
│          → list of properties with API credentials                           │
│          → PETSCREENING__PROPERTY_KEY_FACTS for launch dates                 │
└──────────────────────────┬───────────────────────────────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ STEP 2: FETCH LIVE RENT ROLL DATA                                            │
│                                                                              │
│ Yardi:   GetRentroll SOAP API → XML → flat charge rows                       │
│ Entrata: getLeases REST API → JSON → flat charge rows                        │
│                                                                              │
│ Result: stg_api_charges (one row per tenant x charge line)                   │
│         columns: property_id, property_name, tenant_code, email,             │
│                  charge_code, charge_amount, charge_from_date,               │
│                  charge_to_date, tenant_status, move_in, move_out, etc.      │
└──────────────────────────┬───────────────────────────────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ STEP 3: SELECT CHARGE CODES                                                  │
│                                                                              │
│ Auto-select codes containing: pet, animal, petnr, concpet                    │
│ User can add/remove codes                                                    │
│                                                                              │
│ selected_codes → drives all downstream analysis                              │
└──────────┬──────────────────────────┬──────────────────────┬─────────────────┘
           │                          │                      │
           ▼                          ▼                      ▼
┌────────────────────┐  ┌──────────────────────┐  ┌─────────────────────────┐
│ CHARTS TAB         │  │ MISSING PET RENT     │  │ SUSPECTED UNDISCLOSED   │
│                    │  │                      │  │                         │
│ Filter charges to  │  │ int_paying_tenants   │  │ int_paying_tenants      │
│ selected codes     │  │   (from API data)    │  │   (from API data)       │
│                    │  │         ↓             │  │         ↓               │
│ Parse dates, calc  │  │ int_ps_household_    │  │ int_ps_suspected_       │
│ effective end date │  │   profiles           │  │   profiles              │
│                    │  │   (from Snowflake)   │  │   (from Snowflake)      │
│ Entrata: dedup on  │  │         ↓             │  │         ↓               │
│ _entrata_charge_   │  │ LEFT JOIN on         │  │ LEFT JOIN on            │
│ dedup_key          │  │ (prop_id, tc/email)  │  │ (prop_id, tc/email)     │
│                    │  │ + propagation        │  │ + propagation           │
│ Bucket into months │  │ + freshness filter   │  │ + freshness filter      │
│                    │  │         ↓             │  │         ↓               │
│ Group by property  │  │ int_missing_pet_rent │  │ int_suspected_          │
│ and by portfolio   │  │ (pet_rent_paid = 0)  │  │   undisclosed           │
│                    │  │         ↓             │  │ (pet_rent_paid = 0)     │
│ Compute pre/post   │  │ rpt_missing_pet_rent │  │         ↓               │
│ launch analysis    │  │ INNER JOIN           │  │ Estimate monthly $      │
│                    │  │ r_monthly_exec_      │  │ using avg fees from     │
│ Overlay: adoption  │  │   summary            │  │ paying tenants          │
│ data from QBR      │  │         ↓             │  │                         │
│                    │  │ Final report with    │  │ Output: per-property    │
│ Output: charts +   │  │ pet name, breed,     │  │ counts + monthly $     │
│ KPIs + monthly data│  │ species, unit addr   │  │ + suspected_reason      │
└────────────────────┘  └──────────────────────┘  └─────────────────────────┘
```

---

## Appendix: Key Differences from the dbt Pipeline

| Aspect | dbt Pipeline | This App |
|--------|-------------|----------|
| Charge data source | `raw.yardi_getrentroll_new` staging table | Live API call per property |
| Charge code filter | Hardcoded `charge_code ILIKE '%pet%'` | User-selected codes (auto-suggested) |
| Join to PS profiles | SQL `ht_` join model | Python join with case-insensitive matching + email fallback + unit/lease expansion |
| Suspected undisclosed | Not in dbt | Full behavioral analysis with 4 suspected categories |
| Revenue estimation | Not estimated | Property-specific average fee x active months per missing tenant |
| Entrata support | Separate pipeline | Unified pipeline with Entrata-specific interval filtering and charge dedup |
