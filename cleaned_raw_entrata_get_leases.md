

/*

    Output of Entrata getLeases API endpoint. Source table is populated by stored procedure
    that retrieves all leases from Entrata API.

    Customers (residents) are unpacked as individual rows, one row per customer per lease per property.
    Each row includes all lease intervals as an array and lease activities as an array.

*/

with source as (
    select property_id, source_lease_id, ingested_at, raw_json
    from raw.pmc_external_integrations.entrata_getleases
    qualify row_number() over (partition by property_id, source_lease_id order by ingested_at desc) = 1
),

-- Extract lease activity dates per lease for different event types
lease_activity_dates as (
  select
    s.property_id,
    s.source_lease_id,
    -- Extract dates for each event type using conditional aggregation, parse date types to YYYY-MM-DD
    max(case when lease_act.value:eventType::string = 'Lease From' 
        then coalesce(
            try_to_date(lease_act.value:date::string, 'YYYY-MM-DD'),
            try_to_date(lease_act.value:date::string, 'MM/DD/YYYY')
        ) end) as lease_from_date,
    max(case when lease_act.value:eventType::string = 'Lease To'
        then coalesce(
            try_to_date(lease_act.value:date::string, 'YYYY-MM-DD'),
            try_to_date(lease_act.value:date::string, 'MM/DD/YYYY')
        ) end) as lease_to_date,
    max(case when lease_act.value:eventType::string = 'Actual Move In'
        then coalesce(
            try_to_date(lease_act.value:date::string, 'YYYY-MM-DD'),
            try_to_date(lease_act.value:date::string, 'MM/DD/YYYY')
        ) end) as actual_move_in_date,
    max(case when lease_act.value:eventType::string = 'Actual Move Out'
        then coalesce(
            try_to_date(lease_act.value:date::string, 'YYYY-MM-DD'),
            try_to_date(lease_act.value:date::string, 'MM/DD/YYYY')
        ) end) as actual_move_out_date,
    max(case when lease_act.value:eventType::string = 'Financial Move Out'
        then coalesce(
            try_to_date(lease_act.value:date::string, 'YYYY-MM-DD'),
            try_to_date(lease_act.value:date::string, 'MM/DD/YYYY')
        ) end) as financial_move_out_date,
    max(case when lease_act.value:eventType::string = 'Application'
        then coalesce(
            try_to_date(lease_act.value:date::string, 'YYYY-MM-DD'),
            try_to_date(lease_act.value:date::string, 'MM/DD/YYYY')
        ) end) as application_date,
    max(case when lease_act.value:eventType::string = 'Application Completed On'
        then coalesce(
            try_to_date(lease_act.value:date::string, 'YYYY-MM-DD'),
            try_to_date(lease_act.value:date::string, 'MM/DD/YYYY')
        ) end) as application_completed_on_date
  from source s,
  lateral flatten(input => s.raw_json:leaseActivities:leasesActivity) lease_act
  group by s.property_id, s.source_lease_id
),

customers as (
  select
    s.property_id,
    s.source_lease_id,
    s.ingested_at,
    cust.value:id::string as customer_id,
    cust.value:customerType::string as customer_type,
    cust.value:relationshipName::string as relationship,
    cust.value:firstName::string as first_name,
    cust.value:lastName::string as last_name,
    cust.value:nameFull::string as name_full,
    coalesce(
        cust.value:email::string,
        cust.value:addresses:address:email::string,
        cust.value:addresses:address:additionalEmail::string
    ) as email,
    coalesce(
        cust.value:addresses:address:additionalEmail::string,
        cust.value:addresses:address:email::string
    ) as additional_email,
    coalesce(
        cust.value:phone:phoneNumber::string,
        cust.value:addresses:phone:phoneNumber::string
    ) as phone_number,
    coalesce(
        cust.value:phone:phoneType::string,
        cust.value:addresses:phone:phoneType::string
    ) as phone_type,
    cust.value:phone:countryCode::integer as phone_country_code,
    cust.value:leaseCustomerStatus::string as lease_customer_status,
    coalesce(
        try_to_date(cust.value:moveInDate::string, 'YYYY-MM-DD'),
        try_to_date(cust.value:moveInDate::string, 'MM/DD/YYYY')
    ) as move_in_date,
    coalesce(
        try_to_date(cust.value:moveOutDate::string, 'YYYY-MM-DD'),
        try_to_date(cust.value:moveOutDate::string, 'MM/DD/YYYY')
    ) as move_out_date,
    -- Customer contacts as JSON
    cust.value:customerContacts as customer_contacts_json,
    -- Lease-level fields
    s.raw_json:id::string as lease_id,
    s.raw_json:leaseStatusTypeId::string as lease_status_type_id,
    s.raw_json:leaseIntervalStatus::string as lease_interval_status,
    s.raw_json:leaseIntervalId::string as lease_interval_id,
    s.raw_json:unitId::string as unit_id,
    s.raw_json:unitNumberSpace::string as unit_number_space,
    s.raw_json:unitSpaceId::string as unit_space_id,
    s.raw_json:floorPlanId::string as floor_plan_id,
    s.raw_json:floorPlanName::string as floor_plan_name,
    s.raw_json:occupancyType::string as occupancy_type,
    s.raw_json:occupancyTypeId::string as occupancy_type_id,
    s.raw_json:isMonthToMonth::integer as is_month_to_month,
    s.raw_json:isRenewalBlacklist::string as is_renewal_blacklist,
    -- All lease intervals as array
    s.raw_json:leaseIntervals:leaseInterval as lease_intervals_array,
    -- Lease activities as array
    s.raw_json:leaseActivities:leasesActivity as lease_activities_array,
    -- Scheduled charges as array
    s.raw_json:scheduledCharges:scheduledCharge as scheduled_charges_array,
  from source s,
  lateral flatten(input => s.raw_json:customers:customer) cust
),

residents as (
    select 
        c.property_id,
        c.source_lease_id,
        c.ingested_at,
        c.customer_id,
        c.customer_type,
        c.relationship,
        c.first_name,
        c.last_name,
        c.name_full,
        c.email,
        c.additional_email,
        c.phone_number,
        c.phone_type,
        c.phone_country_code,
        c.lease_customer_status,
        c.customer_contacts_json,
        c.lease_id,
        c.lease_status_type_id,
        c.lease_interval_status,
        c.lease_interval_id,
        c.unit_id,
        c.unit_number_space,
        c.unit_space_id,
        c.floor_plan_id,
        c.floor_plan_name,
        c.occupancy_type,
        c.occupancy_type_id,
        c.is_month_to_month,
        c.is_renewal_blacklist,
        c.lease_intervals_array,
        c.lease_activities_array,
        c.scheduled_charges_array,
        lad.lease_from_date,
        lad.lease_to_date,
        coalesce(c.move_in_date, lad.actual_move_in_date) as lease_move_in_date,
        coalesce(c.move_out_date, lad.actual_move_out_date) as lease_move_out_date,
        lad.financial_move_out_date,
        lad.application_date,
        lad.application_completed_on_date
    from customers c
    left join lease_activity_dates lad
        on c.property_id = lad.property_id
        and c.source_lease_id = lad.source_lease_id
)

select 
    
    
md5(cast(coalesce(cast(property_id as TEXT), '_dbt_utils_surrogate_key_null_') || '-' || coalesce(cast(source_lease_id as TEXT), '_dbt_utils_surrogate_key_null_') || '-' || coalesce(cast(customer_id as TEXT), '_dbt_utils_surrogate_key_null_') || '-' || coalesce(cast(customer_type as TEXT), '_dbt_utils_surrogate_key_null_') as TEXT)) as pmc_integration_unique_key,
    *
from residents