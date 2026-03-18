# Entrata Research & Reference

Consolidated research, source code, and findings for the Entrata integration in the Pet Rent app. Covers the Snowflake stored procedure, staging model, deep-dive validation on Hillpointe, and the presentation data plan.

## Table of Contents

- [Stored Procedure: LOAD_ENTRATA_LEASES](#stored-procedure-load_entrata_leases)
- [Staging Model: getLeases CTE](#staging-model-getleases-cte)
- [Deep Dive: Hillpointe (Entrata)](#deep-dive-hillpointe-entrata)
- [Presentation Data Plan](#presentation-data-plan)

---

## Stored Procedure: LOAD_ENTRATA_LEASES

The Snowflake stored procedure that syncs lease data from the Entrata API. Handles pagination, retry logic, property-level tracking, and batch insertion.

```python
CREATE OR REPLACE PROCEDURE RAW.PMC_EXTERNAL_INTEGRATIONS.LOAD_ENTRATA_LEASES()
RETURNS TABLE ("STATUS" VARCHAR)
LANGUAGE PYTHON
RUNTIME_VERSION = '3.9'
PACKAGES = ('requests','snowflake-snowpark-python')
HANDLER = 'run_entrata_sync'
EXTERNAL_ACCESS_INTEGRATIONS = (ENTRATA_EXTERNAL_ACCESS_INTEGRATION)
SECRETS = ('entrata_api_key'=ADMIN.PUBLIC.ENTRATA_API_KEY)
EXECUTE AS OWNER
AS '
import requests
import json
from datetime import datetime
from snowflake.snowpark import Session
import _snowflake

# Constants
BASE_API_URL = "https://apis.entrata.com/ext/orgs"

# Lease status filter configuration
# Set to None to retrieve ALL leases (will explicitly request all status types: 1,2,3,4,5,6)
# Set to specific status code(s) to filter (e.g., "4" for Current, "4,6" for Current and Past)
# Entrata lease status types:
#   1 = Applicant, 2 = Cancelled, 3 = Future, 4 = Current, 5 = Notice, 6 = Past
LEASE_STATUS_TYPE_IDS = None  # None = all leases (1,2,3,4,5,6), or specify like "4" or "4,6"

# Target tables, one for lease data, one to record property response status
TARGET_TABLE = "RAW.PMC_EXTERNAL_INTEGRATIONS.ENTRATA_GETLEASES"
TRACKING_TABLE = "RAW.PMC_EXTERNAL_INTEGRATIONS.ENTRATA_GETLEASES_PROPERTY_RESPONSES"

# Get Entrata API key from Snowflake secrets
# Note: We''ll validate this when the procedure runs, not during creation
def get_entrata_api_key():
    """Get and validate the Entrata API key from secrets"""
    api_key = _snowflake.get_generic_secret_string(''entrata_api_key'')
    if not api_key:
        raise ValueError("Entrata API key not found in secrets. Please configure ADMIN.PUBLIC.ENTRATA_API_KEY")
    return api_key

def post_with_retries(url, json_data, headers, max_retries=3, backoff_factor=5):
    """Function to allow API retries with exponential backoff"""
    import time
    
    for attempt in range(1, max_retries + 1):
        try:
            response = requests.post(url, json=json_data, headers=headers, timeout=60)
            response.raise_for_status()
            return response
        except requests.exceptions.HTTPError as e:
            # Log 401 errors with more detail for debugging
            if response.status_code == 401:
                error_detail = f"401 Unauthorized - Check API key authentication. Response: {response.text[:500]}"
                if attempt == max_retries:
                    raise requests.exceptions.HTTPError(error_detail, response=response)
            if attempt == max_retries:
                raise
            wait_time = backoff_factor * attempt
            time.sleep(wait_time)
        except Exception as e:
            if attempt == max_retries:
                raise
            wait_time = backoff_factor * attempt
            time.sleep(wait_time)

def extract_leases_json(response_json):
    """Parse JSON response and extract lease records
    Returns tuple: (lease_records, error_message, has_more_pages, meta_info)
    """
    try:
        # Check for error in response
        if ''response'' not in response_json:
            return [], "No ''response'' key in API response", False, None
        
        response_obj = response_json[''response'']
        
        # Check for error code
        if response_obj.get(''code'') != 200:
            error_msg = response_obj.get(''message'', f"API returned code {response_obj.get(''code'')}")
            return [], error_msg, False, None
        
        # Extract leases
        result = response_obj.get(''result'', {})
        leases_data = result.get(''leases'', {})
        
        # Handle both single lease object and array of leases
        leases = []
        if isinstance(leases_data.get(''lease''), list):
            leases = leases_data[''lease'']
        elif isinstance(leases_data.get(''lease''), dict):
            leases = [leases_data[''lease'']]
        
        lease_records = []
        for lease in leases:
            # Store the original lease object from API (unedited) along with its ID for the column
            # The RAW_JSON will contain the original lease object exactly as returned by the API
            lease_record = {
                "source_lease_id": lease.get("id"),  # Extract ID for SOURCE_LEASE_ID column
                "original_lease": lease  # Keep original lease object unedited for RAW_JSON
            }
            lease_records.append(lease_record)
        
        # Check for pagination using meta information
        # Format: {"meta": {"currentPage": 1, "lastPage": 102, "perPage": 5, "total": 507}}
        meta = result.get(''meta'', {})
        current_page = meta.get(''currentPage'', 1)
        last_page = meta.get(''lastPage'', 1)
        has_more = current_page < last_page
        
        return lease_records, None, has_more, meta
        
    except Exception as e:
        return [], f"JSON parsing error: {str(e)}", False, None

def process_property(property_row, api_key, session):
    """Function to request API for a single property and return lease records
    Returns tuple: (lease_records, error_message)
    Handles pagination automatically
    """
    property_id = property_row.get(''PROPERTY_ID'')
    property_source_id = property_row.get(''PROPERTY_SOURCE_ID'')
    corp_id = property_row.get(''CORP_ID'')
    
    if not corp_id:
        error_msg = "Missing corp_id in property settings"
        return [], error_msg
    if not property_source_id:
        error_msg = "Missing property_source_id"
        return [], error_msg
    
    # Use API key from secret
    if not api_key:
        error_msg = "Missing API key in secrets"
        return [], error_msg
    
    property_api_key = api_key
    
    # Build request headers
    headers = {
        "Content-Type": "application/json"
    }
    
    # Entrata API key authentication
    # Based on Postman configuration: X-Api-Key header with API key as value
    headers["X-Api-Key"] = property_api_key
    
    all_lease_records = []
    page_no = 1
    per_page = 100  # Number of records per page (adjust as needed)
    max_pages = 1000  # Safety limit to prevent infinite loops
    
    try:
        while page_no <= max_pages:
            # Build API endpoint URL with pagination query parameters
            # Format: https://apis.entrata.com/ext/orgs/{corp_id}/v1/leases?page_no=1&per_page=100
            api_url = f"{BASE_API_URL}/{corp_id}/v1/leases?page_no={page_no}&per_page={per_page}"
            
            # Build request body based on example input
            # Build params object
            params = {
                "propertyId": property_source_id,
                "includeArTransactions": 0,
                "includeLeaseHistory": 1
            }
            
            # Handle leaseStatusTypeIds parameter
            # If None, explicitly request all status types (1,2,3,4,5,6) to ensure we get all leases
            # If specified, use the provided filter
            if LEASE_STATUS_TYPE_IDS:
                params["leaseStatusTypeIds"] = LEASE_STATUS_TYPE_IDS
            else:
                # Explicitly request all lease status types to get all leases
                # Status types: 1=Applicant, 2=Cancelled, 3=Future, 4=Current, 5=Notice, 6=Past
                params["leaseStatusTypeIds"] = "1,2,3,4,5,6"
            
            # Build request body - auth object matches Postman example format
            # Note: The example shows auth.type but no auth.key, so key is likely only in header
            request_body = {
                "auth": {
                    "type": "apikey"
                    # API key is passed in Authorization header, not in auth.key field
                },
                "requestId": page_no,  # Use page number as requestId
                "method": {
                    "name": "getLeases",
                    "version": "r1",
                    "params": params
                }
            }
            
            response = post_with_retries(api_url, request_body, headers)
            response_json = response.json()
            
            # Extract lease records from JSON response
            lease_records, error_message, has_more, meta_info = extract_leases_json(response_json)
            
            if error_message:
                # If first page fails, return error
                if page_no == 1:
                    return [], error_message
                # If later page fails, continue with what we have
                break
            
            all_lease_records.extend(lease_records)
            
            # Check if there are more pages - prioritize meta info for accurate pagination
            has_more_pages = False
            if meta_info:
                current_page = meta_info.get(''currentPage'', page_no)
                last_page = meta_info.get(''lastPage'', 1)
                has_more_pages = current_page < last_page
            else:
                # Fallback to has_more flag if meta info not available
                has_more_pages = has_more
                # If we got exactly per_page records, there might be more (safety check)
                if len(lease_records) == per_page and not has_more:
                    # Continue to next page to be safe
                    has_more_pages = True
            
            # Check if there are more pages
            if not has_more_pages:
                break
            
            page_no += 1
        
        # Add property_id to each record for insertion (but keep original_lease unedited)
        for record in all_lease_records:
            record["property_id"] = property_row.get(''PROPERTY_ID'')
        
        return all_lease_records, None
        
    except Exception as e:
        error_msg = str(e)
        return [], error_msg

def load_properties_query():
    """Return the SQL query to load properties with Entrata integration"""
    return """
    select
        i.integration_id,
        parse_json(i.settings):"corp_id"::string as corp_id,
        parse_json(i.settings):"user_name"::string as user_name,
        parse_json(i.settings):"password"::string as password,
        parse_json(p.property_source_id):"property_id"::string as property_source_id,
        p.property_id
    from prod.common.d_properties p
    left join prod.staging.stg_petscreening__integrations i
        on p.integration_id = i.integration_id
    where i.system = ''entrata''
        and p.property_source_name = ''entrata''
        and p.property_source_id:"property_id"::string is not null
        -- Optional filters to target properties that should work, remove for wider pool of properties
        and i.state = ''enabled''
        and p.integration_status = ''enabled''
        and p.property_status = ''active''
        --and property_id = 37440
and parse_json(p.property_source_id):"property_id"::string in (''100002775'',
''100059792'') --use to filter only specific property_ids, remove
    order by p.property_id asc
    """

def insert_lease_records(session, lease_records, ingestion_timestamp):
    """Insert lease records into the target table (append only)"""
    if not lease_records:
        return
    
    # Prepare data for Snowflake insertion
    records_for_snowflake = []
    for record in lease_records:
        source_lease_id = record.get(''source_lease_id'')
        original_lease = record.get(''original_lease'')
        property_id = record.get(''property_id'')
        
        if source_lease_id and property_id and original_lease:
            # Store the original, unedited lease object from the API in RAW_JSON
            records_for_snowflake.append({
                ''PROPERTY_ID'': property_id,
                ''SOURCE_LEASE_ID'': source_lease_id,
                ''RAW_JSON'': original_lease,  # Original lease object from API, unedited
                ''INGESTED_AT'': ingestion_timestamp
            })
    
    if records_for_snowflake:
        # Create DataFrame and insert (append mode)
        df = session.create_dataframe(records_for_snowflake)
        df.write.mode("append").save_as_table(TARGET_TABLE)

def insert_property_response_tracking(session, property_row, attempt_timestamp, success_flag, records_count=0, error_message=None):
    """Insert property response tracking record"""
    tracking_record = {
        ''ATTEMPT_TIMESTAMP'': attempt_timestamp,
        ''PROPERTY_ID'': property_row.get(''PROPERTY_ID''),
        ''PROPERTY_SOURCE_ID'': property_row.get(''PROPERTY_SOURCE_ID''),
        ''INTEGRATION_ID'': property_row.get(''INTEGRATION_ID''),
        ''CORP_ID'': property_row.get(''CORP_ID''),
        ''SUCCESS_FLAG'': success_flag,
        ''RECORDS_COUNT'': records_count,
        ''ERROR_MESSAGE'': error_message,
        ''INGESTED_AT'': datetime.now()
    }
    
    # Create DataFrame and insert tracking record
    df = session.create_dataframe([tracking_record])
    df.write.mode("append").save_as_table(TRACKING_TABLE)

def run_entrata_sync(session: Session):
    """Main function to run the Entrata leases sync"""
    
    try:
        # Get and validate API key at execution time (not during procedure creation)
        entrata_api_key = get_entrata_api_key()
        
        ingestion_timestamp = datetime.now()  # Use timezone-naive timestamp to match TIMESTAMP_NTZ column
        
        # Load properties
        properties_df = session.sql(load_properties_query()).collect()
        
        # Process properties in batches to avoid memory issues
        BATCH_SIZE = 50  # Process 50 properties at a time
        total_records_processed = 0
        
        # Process properties in batches
        for i in range(0, len(properties_df), BATCH_SIZE):
            batch_properties = properties_df[i:i + BATCH_SIZE]
            batch_lease_records = []
            batch_num = (i // BATCH_SIZE) + 1
            total_batches = (len(properties_df) + BATCH_SIZE - 1) // BATCH_SIZE
            
            # Process each property in this batch
            for property_row in batch_properties:
                property_dict = property_row.asDict()
                attempt_timestamp = datetime.now()
                
                try:
                    # Get lease records for this property
                    lease_records, error_message = process_property(property_dict, entrata_api_key, session)
                    
                    if error_message:
                        # API call failed
                        insert_property_response_tracking(
                            session, 
                            property_dict, 
                            attempt_timestamp, 
                            success_flag=False, 
                            records_count=0,
                            error_message=error_message
                        )
                    else:
                        # Successful API call with valid data
                        batch_lease_records.extend(lease_records)
                        insert_property_response_tracking(
                            session, 
                            property_dict, 
                            attempt_timestamp, 
                            success_flag=True, 
                            records_count=len(lease_records)
                        )
                    
                except Exception as e:
                    # Record failed API call (network/connection errors)
                    error_msg = str(e)
                    insert_property_response_tracking(
                        session, 
                        property_dict, 
                        attempt_timestamp, 
                        success_flag=False, 
                        records_count=0,
                        error_message=error_msg
                    )
                    continue
            
            # Insert this batch to the target table
            if batch_lease_records:
                insert_lease_records(session, batch_lease_records, ingestion_timestamp)
                total_records_processed += len(batch_lease_records)
        
        record_count = total_records_processed
        return session.sql(f"SELECT ''Entrata leases sync completed successfully - {record_count} lease records processed'' AS status")
        
    except Exception as e:
        error_msg = str(e)
        return session.sql(f"SELECT ''Error in Entrata leases sync: {error_msg}'' AS status")

';
```

---

## Staging Model: getLeases CTE

The SQL model that unpacks the raw Entrata API JSON into a flat table of residents with lease details, charge arrays, and activity dates.

```sql
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
```

---

## Deep Dive: Hillpointe (Entrata)

**Generated:** 2026-03-03 12:24

### Step 0: Properties & Launch Dates

**43 properties** found for Hillpointe - Enterprise

- With launch date: **43**
- Without launch date: **0**

| Property | Launch Date | Corp ID | Entrata PID |
|----------|------------|---------|-------------|
| Hillpointe - Pointe Grand Augusta | 2025-04-17 | hillpointe | 100070619 |
| Hillpointe - Pointe Grand Augusta Reservation Way | 2025-05-20 | hillpointe | 100070638 |
| Hillpointe - Pointe Grand Beaufort | 2025-09-18 | hillpointe | 100070605 |
| Hillpointe - Pointe Grand Bergen Woods | 2025-05-20 | hillpointe | 100070615 |
| Hillpointe - Pointe Grand Brick City | 2025-05-21 | hillpointe | 100070639 |
| Hillpointe - Pointe Grand Brunswick | 2025-04-17 | hillpointe | 100070620 |
| Hillpointe - Pointe Grand Brunswick Island View | 2025-01-08 | hillpointe | 100070592 |
| Hillpointe - Pointe Grand Byron | 2025-05-20 | hillpointe | 100070621 |
| Hillpointe - Pointe Grand Cartersville | 2026-01-12 | hillpointe | 100139785 |
| Hillpointe - Pointe Grand Champions Village | 2025-09-18 | hillpointe | 100070602 |
| Hillpointe - Pointe Grand Columbia | 2025-04-24 | hillpointe | 100070616 |
| Hillpointe - Pointe Grand Covington | 2025-10-20 | hillpointe | 100070613 |
| Hillpointe - Pointe Grand Davenport | 2024-04-04 | hillpointe | 100070636 |
| Hillpointe - Pointe Grand Dawsonville | 2025-05-20 | hillpointe | 100070622 |
| Hillpointe - Pointe Grand Daytona | 2025-04-17 | hillpointe | 100070618 |
| Hillpointe - Pointe Grand DeLand | 2025-04-08 | hillpointe | 100070595 |
| Hillpointe - Pointe Grand Fort Pierce | 2026-01-12 | hillpointe | 100070601 |
| Hillpointe - Pointe Grand Jacksonville West | 2025-03-04 | hillpointe | 100070626 |
| Hillpointe - Pointe Grand Lakeland | 2025-10-20 | hillpointe | 100070600 |
| Hillpointe - Pointe Grand Macon | 2025-04-17 | hillpointe | 100070627 |
| Hillpointe - Pointe Grand New Berlin | 2025-04-23 | hillpointe | 100070596 |
| Hillpointe - Pointe Grand North Athens | 2025-10-22 | hillpointe | 100070607 |
| Hillpointe - Pointe Grand Ocala | 2023-07-01 | hillpointe | 100070625 |
| Hillpointe - Pointe Grand Palm Coast | 2025-04-23 | hillpointe | 100070624 |
| Hillpointe - Pointe Grand Panama City | 2025-09-18 | hillpointe | 100070597 |
| Hillpointe - Pointe Grand Pendergrass | 2023-12-06 | hillpointe | 100070630 |
| Hillpointe - Pointe Grand Pensacola | 2025-10-20 | hillpointe | 100070612 |
| Hillpointe - Pointe Grand Plant City | 2024-06-06 | hillpointe | 100070631 |
| Hillpointe - Pointe Grand Port Wentworth | 2025-09-18 | hillpointe | 100070598 |
| Hillpointe - Pointe Grand Southlake | 2023-07-01 | hillpointe | 100070632 |
| Hillpointe - Pointe Grand Spartanburg | 2025-05-20 | hillpointe | 100070623 |
| Hillpointe - Pointe Grand Spring Hill | 2024-07-18 | hillpointe | 100070633 |
| Hillpointe - Pointe Grand Statesboro | 2025-10-20 | hillpointe | 100070604 |
| Hillpointe - Pointe Grand Thomaston | 2025-03-21 | hillpointe | 100070593 |
| Hillpointe - Pointe Grand Timber Ridge | 2025-05-20 | hillpointe | 100070637 |
| Hillpointe - Pointe Grand Town Center | 2026-01-12 | hillpointe | 100070603 |
| Hillpointe - Pointe Grand Warner Robins | 2025-04-17 | hillpointe | 100070634 |
| Hillpointe - Pointe Grand Willowbrook | 2025-12-10 | hillpointe | 100070608 |
| Hillpointe - Pointe Grand Yulee | 2025-10-20 | hillpointe | 100070599 |
| Hillpointe - Pointe Grand at Heath Brook | 2025-05-20 | hillpointe | 100070617 |
| Hillpointe - Pointe Grand at Oakleaf | 2025-03-04 | hillpointe | 100070629 |
| Hillpointe - Pointe Grand on Main | 2023-09-29 | hillpointe | 100070628 |
| Hillpointe - Pointe Villas | 2025-04-17 | hillpointe | 100070635 |

> **Note:** The Entrata `getLeases` API caps at 100 leases per page. The pagination in both this script and `app.py` breaks after page 1 if the API doesn't return `meta.currentPage`/`meta.lastPage`. This means we may only see the first 100 leases per property.

### Charge Code Discovery

Pet-related charge codes found:

| Code | Count |
|------|-------|
| Pet Rent | 2,359 |
| Pet Fee | 283 |

**Using:** `['Pet Rent', 'Pet Fee']`

Top 20 charge codes overall:

| Code | Count |
|------|-------|
| Tech Package | 8,818 |
| Washer/Dryer Rental Charges | 8,818 |
| Valet Trash Fee | 8,765 |
| Rent | 8,691 |
| One-Time Concession | 3,750 |
| Amenity Rent | 3,645 |
| Pet Rent | 2,359 |
| Amenity Fob | 2,129 |
| Risk Waiver Fee | 1,671 |
| Garage Rent | 1,429 |
| Hassle-Free Living | 1,163 |
| Month-to-Month Rent | 970 |
| Month To Month Fee | 892 |
| Administrative Fee | 536 |
| Garage/Storage Unit Concession | 421 |
| Pet Fee | 283 |
| Internet Concession | 198 |
| Washer/Dryer Concession | 193 |
| Renewal Rent | 129 |
| Trash Fine | 91 |

### Investigation 1: Pre-PetScreening Data Availability

**Question:** Do we get pet charge data from before PetScreening went live, or does the API only return data from around launch?

#### Interval status distribution (all pet charges)

| Status | Count | Is Valid |
|--------|-------|----------|
|  | 2,642 | False |

> **Note:** No charges matched 'current/past/notice' interval filter. Using ALL pet charges for analysis.

**2 properties with ZERO pet charges:**
- Hillpointe - Pointe Grand Cartersville
- Hillpointe - Pointe Grand Town Center

#### Per-Property Breakdown

| Property | Launch | Earliest Charge | Pre Months | Post Months | Pre Charges | Post Charges | Months Before Launch |
|----------|--------|-----------------|-----------|------------|-------------|-------------|---------------------|
| Hillpointe - Pointe Grand North Athens | 2025-10-22 | 2026-01-02 | 0 | 1 | 0 | 2 | -3 |
| Hillpointe - Pointe Grand Port Wentworth | 2025-09-18 | 2025-10-22 | 0 | 4 | 0 | 11 | -2 |
| Hillpointe - Pointe Grand Plant City | 2024-06-06 | 2024-08-16 | 0 | 20 | 0 | 63 | -3 |
| Hillpointe - Pointe Grand Pensacola | 2025-10-20 | 2026-02-02 | 0 | 2 | 0 | 4 | -4 |
| Hillpointe - Pointe Grand Pendergrass | 2023-12-06 | 2024-02-10 | 0 | 24 | 0 | 81 | -3 |
| Hillpointe - Pointe Grand Statesboro | 2025-10-20 | 2025-11-01 | 0 | 4 | 0 | 8 | -1 |
| Hillpointe - Pointe Grand Ocala | 2023-07-01 | 2024-01-30 | 0 | 25 | 0 | 52 | -8 |
| Hillpointe - Pointe Grand on Main | 2023-09-29 | 2024-05-07 | 0 | 11 | 0 | 24 | -8 |
| Hillpointe - Pointe Grand New Berlin | 2025-04-23 | 2025-09-26 | 0 | 1 | 0 | 2 | -6 |
| Hillpointe - Pointe Grand Thomaston | 2025-03-21 | 2025-05-01 | 0 | 1 | 0 | 12 | -2 |
| Hillpointe - Pointe Grand Lakeland | 2025-10-20 | 2026-04-24 | 0 | 3 | 0 | 10 | -7 |
| Hillpointe - Pointe Grand Fort Pierce | 2026-01-12 | 2026-02-27 | 0 | 2 | 0 | 5 | -2 |
| Hillpointe - Pointe Grand Southlake | 2023-07-01 | 2024-02-12 | 0 | 14 | 0 | 24 | -8 |
| Hillpointe - Pointe Grand DeLand | 2025-04-08 | 2025-06-01 | 0 | 2 | 0 | 10 | -2 |
| Hillpointe - Pointe Grand Willowbrook | 2025-12-10 | 2026-02-26 | 0 | 2 | 0 | 2 | -3 |
| Hillpointe - Pointe Grand Davenport | 2024-04-04 | 2024-11-05 | 0 | 8 | 0 | 23 | -8 |
| Hillpointe - Pointe Grand Covington | 2025-10-20 | 2026-01-12 | 0 | 2 | 0 | 4 | -3 |
| Hillpointe - Pointe Grand Yulee | 2025-10-20 | 2025-12-31 | 0 | 5 | 0 | 13 | -3 |
| Hillpointe - Pointe Grand Champions Village | 2025-09-18 | 2026-04-24 | 0 | 2 | 0 | 4 | -8 |
| Hillpointe - Pointe Grand Brunswick Island View | 2025-01-08 | 2025-05-27 | 0 | 4 | 0 | 12 | -5 |
| Hillpointe - Pointe Grand Bergen Woods | 2025-05-20 | 2025-09-30 | 0 | 5 | 0 | 17 | -5 |
| Hillpointe - Pointe Grand Beaufort | 2025-09-18 | 2026-01-02 | 0 | 3 | 0 | 8 | -4 |
| Hillpointe - Pointe Grand Spring Hill | 2024-07-18 | 2024-10-01 | 0 | 14 | 0 | 53 | -3 |
| Hillpointe - Pointe Grand Panama City | 2025-09-18 | 2025-08-25 | 2 | 2 | 8 | 6 | 0 |
| Hillpointe - Pointe Grand Brick City | 2025-05-21 | 2025-02-28 | 3 | 5 | 5 | 26 | 2 |
| Hillpointe - Pointe Grand Augusta Reservation Way | 2025-05-20 | 2025-02-14 | 4 | 6 | 13 | 16 | 3 |
| Hillpointe - Pointe Grand Timber Ridge | 2025-05-20 | 2024-12-16 | 4 | 4 | 10 | 7 | 5 |
| Hillpointe - Pointe Grand at Oakleaf | 2025-03-04 | 2024-10-14 | 6 | 8 | 21 | 13 | 4 |
| Hillpointe - Pointe Grand Jacksonville West | 2025-03-04 | 2024-02-15 | 7 | 8 | 12 | 20 | 12 |
| Hillpointe - Pointe Grand at Heath Brook | 2025-05-20 | 2024-05-10 | 11 | 7 | 29 | 13 | 12 |
| Hillpointe - Pointe Grand Augusta | 2025-04-17 | 2024-04-30 | 11 | 11 | 22 | 26 | 11 |
| Hillpointe - Pointe Grand Columbia | 2025-04-24 | 2024-03-30 | 11 | 10 | 26 | 27 | 13 |
| Hillpointe - Pointe Grand Byron | 2025-05-20 | 2024-04-15 | 11 | 8 | 22 | 19 | 13 |
| Hillpointe - Pointe Grand Brunswick | 2025-04-17 | 2024-02-23 | 11 | 11 | 22 | 28 | 13 |
| Hillpointe - Pointe Grand Palm Coast | 2025-04-23 | 2024-02-01 | 12 | 9 | 30 | 23 | 14 |
| Hillpointe - Pointe Grand Daytona | 2025-04-17 | 2024-01-08 | 12 | 9 | 18 | 20 | 15 |
| Hillpointe - Pointe Grand Warner Robins | 2025-04-17 | 2023-12-14 | 12 | 12 | 25 | 24 | 16 |
| Hillpointe - Pointe Grand Dawsonville | 2025-05-20 | 2024-03-28 | 12 | 9 | 32 | 22 | 13 |
| Hillpointe - Pointe Grand Spartanburg | 2025-05-20 | 2024-04-01 | 12 | 11 | 27 | 21 | 13 |
| Hillpointe - Pointe Grand Macon | 2025-04-17 | 2024-01-02 | 13 | 8 | 26 | 15 | 15 |
| Hillpointe - Pointe Villas | 2025-04-17 | 2024-02-02 | 13 | 11 | 33 | 40 | 14 |

#### Verdict

- Properties with **< 3 pre-launch months** (unreliable baseline): **24**
- Properties with **3+ pre-launch months** (usable baseline): **17**
- Properties with **zero pet charges**: **2**

> **Finding:** The majority of properties lack sufficient pre-launch charge data. This strongly suggests the Entrata `getLeases` API only returns scheduled charges from around the time the PetScreening integration was configured -- NOT the full historical charge ledger. The 'Revenue Change Since PetScreening' metric is **unreliable** for this PMC.
>
> Properties with some pre-launch data may have been piloting or had their integration enabled before the official launch date.

### Investigation 2: Missing Pet Rent Deduplication

**Question:** If two residents share a lease and one is paying, do we correctly exclude the other from 'missing'? Are we overcounting?

- Total leases with pet charges: **701**
- Shared leases (2+ customers) with pet charges: **572** (81.6%)

#### Paying set expansion

- Direct paying tenant_codes: **1511**
- After unit expansion: **2003** (+492 co-tenants added)
- Direct paying emails: **1357**
- After unit expansion: **1752** (+395 added)

#### Profile matching results

- Total active household profiles: **1460** unique emails
- Matched as **paying**: **191**
- Flagged as **missing** (not paying pet rent): **1269**

#### Match method breakdown (paying profiles)

- Direct tenant_code: 0
- Direct email: 185
- Unit expansion (TC): 0
- Unit expansion (email): 191

#### Missing tenants by property

| Property | Missing Tenants |
|----------|----------------|
| Hillpointe - Pointe Grand Dawsonville | 88 |
| Hillpointe - Pointe Grand Palm Coast | 85 |
| Hillpointe - Pointe Grand Pendergrass | 68 |
| Hillpointe - Pointe Grand Spring Hill | 65 |
| Hillpointe - Pointe Grand Augusta | 61 |
| Hillpointe - Pointe Grand Columbia | 55 |
| Hillpointe - Pointe Grand Plant City | 53 |
| Hillpointe - Pointe Grand at Oakleaf | 51 |
| Hillpointe - Pointe Grand Macon | 48 |
| Hillpointe - Pointe Grand Spartanburg | 47 |
| Hillpointe - Pointe Grand Ocala | 47 |
| Hillpointe - Pointe Grand Byron | 47 |
| Hillpointe - Pointe Grand Davenport | 46 |
| Hillpointe - Pointe Grand Brunswick | 44 |
| Hillpointe - Pointe Grand on Main | 43 |
| Hillpointe - Pointe Grand Warner Robins | 43 |
| Hillpointe - Pointe Grand Brunswick Island View | 33 |
| Hillpointe - Pointe Grand Daytona | 31 |
| Hillpointe - Pointe Grand Jacksonville West | 31 |
| Hillpointe - Pointe Villas | 30 |
| Hillpointe - Pointe Grand DeLand | 29 |
| Hillpointe - Pointe Grand Brick City | 29 |
| Hillpointe - Pointe Grand Thomaston | 29 |
| Hillpointe - Pointe Grand Panama City | 27 |
| Hillpointe - Pointe Grand Timber Ridge | 22 |
| Hillpointe - Pointe Grand at Heath Brook | 20 |
| Hillpointe - Pointe Grand Southlake | 18 |
| Hillpointe - Pointe Grand Augusta Reservation Way | 16 |
| Hillpointe - Pointe Grand Bergen Woods | 13 |
| Hillpointe - Pointe Grand New Berlin | 13 |
| Hillpointe - Pointe Grand Port Wentworth | 11 |
| Hillpointe - Pointe Grand Statesboro | 10 |
| Hillpointe - Pointe Grand Beaufort | 9 |
| Hillpointe - Pointe Grand Fort Pierce | 5 |
| Hillpointe - Pointe Grand Yulee | 2 |

#### Spot check: Sample 'missing' profiles

Checking whether these tenants actually appear in the API data and what charges they have:

- **Eion Madden** (eionmadden@gmail.com)
  - Property: Hillpointe - Pointe Grand Macon, Unit: None, TC: None
  - Found in API data: **NO** -- not in API at all
  - Has pet charge: No (correctly missing)
- **Michelle Sullivan** (futbol37@hotmail.com)
  - Property: Hillpointe - Pointe Grand Spartanburg, Unit: None, TC: None
  - Found in API data: **NO** -- not in API at all
  - Has pet charge: No (correctly missing)
- **Arianna Ciszczon** (ariannaciszczon@gmail.com)
  - Property: Hillpointe - Pointe Grand on Main, Unit: None, TC: None
  - Found in API data: **NO** -- not in API at all
  - Has pet charge: No (correctly missing)
- **Joshua Guerrero** (joshguerrero20132014@gmail.com)
  - Property: Hillpointe - Pointe Grand Brunswick, Unit: None, TC: None
  - Found in API data: **NO** -- not in API at all
  - Has pet charge: No (correctly missing)
- **Alan Valentin** (aceltic17@gmail.com)
  - Property: Hillpointe - Pointe Grand Plant City, Unit: None, TC: None
  - Found in API data: **NO** -- not in API at all
  - Has pet charge: No (correctly missing)
- **Marissa Monn** (marissa.monn11@gmail.com)
  - Property: Hillpointe - Pointe Grand Palm Coast, Unit: None, TC: None
  - Found in API data: **NO** -- not in API at all
  - Has pet charge: No (correctly missing)
- **Katharine Cutia** (katharinecutia@gmail.com)
  - Property: Hillpointe - Pointe Grand Brunswick Island View, Unit: None, TC: None
  - Found in API data: **NO** -- not in API at all
  - Has pet charge: No (correctly missing)
- **Toree Davis** (toreed19@gmail.com)
  - Property: Hillpointe - Pointe Grand Augusta Reservation Way, Unit: None, TC: None
  - Found in API data: **NO** -- not in API at all
  - Has pet charge: No (correctly missing)
- **Autumn Martin** (fallwolfmartin@gmail.com)
  - Property: Hillpointe - Pointe Grand Spring Hill, Unit: None, TC: None
  - Found in API data: **NO** -- not in API at all
  - Has pet charge: No (correctly missing)
- **Renee Pologe** (reneepologe@gmail.com)
  - Property: Hillpointe - Pointe Grand Augusta, Unit: None, TC: None
  - Found in API data: **NO** -- not in API at all
  - Has pet charge: No (correctly missing)
- **Lisa Robitaille** (robitaillelisam@gmail.com)
  - Property: Hillpointe - Pointe Grand Davenport, Unit: None, TC: None
  - Found in API data: **NO** -- not in API at all
  - Has pet charge: No (correctly missing)
- **Ashley Anderson** (wilsonelizabeth608@gmail.com)
  - Property: Hillpointe - Pointe Grand Dawsonville, Unit: None, TC: None
  - Found in API data: **NO** -- not in API at all
  - Has pet charge: No (correctly missing)
- **angel boulware** (angelshoes8@icloud.com)
  - Property: Hillpointe - Pointe Grand Southlake, Unit: None, TC: None
  - Found in API data: **NO** -- not in API at all
  - Has pet charge: No (correctly missing)
- **Dorthalia Newberry** (drthl_smith@yahoo.com)
  - Property: Hillpointe - Pointe Grand Brunswick Island View, Unit: None, TC: None
  - Found in API data: **NO** -- not in API at all
  - Has pet charge: No (correctly missing)
- **Tryston Mckenna** (tryston.mckenna@gmail.com)
  - Property: Hillpointe - Pointe Grand Jacksonville West, Unit: None, TC: None
  - Found in API data: **NO** -- not in API at all
  - Has pet charge: No (correctly missing)

#### Verdict

Of the **1269** 'missing' tenants:
- **1226** are not in the Entrata API data at all (may be past tenants or TC mismatch)
- **0** actually DO have pet charges in the API (matching bug -- tenant_code/email mismatch between Snowflake and API)
- **43** are genuinely in the API without pet charges (truly missing)

> **Stale profiles:** 1226 tenants have active PetScreening profiles but don't appear in the current Entrata API data. They may have moved out but their PetScreening profile wasn't deactivated, or the tenant_code in Snowflake doesn't match any customer_id in the API.

### Investigation 3: Scheduled vs Actual Charges

**Question:** `getLeases` returns `scheduledCharges`. How reliable are these as a proxy for actual revenue?

- Valid pet charges (deduped): **1191**
- Future-dated (scheduled, not yet billed): **98**
- Zero-amount (waived/placeholder): **15**
- On invalid intervals (excluded by app): **1191**

#### Frequency distribution

| Frequency | Count |
|-----------|-------|
| Monthly | 1,067 |
| One-Time | 124 |

#### Amount distribution by frequency

- **Monthly:** 1067 charges, $0-$140, mean=$26.33, median=$20.00
- **One-Time:** 124 charges, $0-$1550, mean=$396.77, median=$350.00

#### Zero-amount charges detail

| Property | Code | Frequency | Start | Interval Status |
|----------|------|-----------|-------|----------------|
| Hillpointe - Pointe Grand Augusta | Pet Rent | Monthly | 2025-05-06 |  |
| Hillpointe - Pointe Grand Brick City | Pet Fee | One-Time | 2025-08-01 |  |
| Hillpointe - Pointe Grand Daytona | Pet Rent | Monthly | 2025-09-16 |  |
| Hillpointe - Pointe Grand Daytona | Pet Rent | Monthly | 2025-07-14 |  |
| Hillpointe - Pointe Grand DeLand | Pet Rent | Monthly | 2025-06-06 |  |
| Hillpointe - Pointe Grand Jacksonville West | Pet Rent | Monthly | 2026-01-16 |  |
| Hillpointe - Pointe Grand Jacksonville West | Pet Rent | Monthly | 2026-01-16 |  |
| Hillpointe - Pointe Grand Ocala | Pet Rent | Monthly | 2025-07-21 |  |
| Hillpointe - Pointe Grand Palm Coast | Pet Rent | Monthly | 2026-01-29 |  |
| Hillpointe - Pointe Grand Pendergrass | Pet Rent | Monthly | 2025-11-14 |  |
| Hillpointe - Pointe Grand Spring Hill | Pet Rent | Monthly | 2025-11-29 |  |
| Hillpointe - Pointe Grand Spring Hill | Pet Rent | Monthly | 2026-02-07 |  |
| Hillpointe - Pointe Grand Timber Ridge | Pet Rent | Monthly | 2025-06-13 |  |
| Hillpointe - Pointe Grand Warner Robins | Pet Rent | Monthly | 2025-04-06 |  |
| Hillpointe - Pointe Grand Warner Robins | Pet Rent | Monthly | 2025-06-10 |  |

#### AR Transactions test

Testing `includeArTransactions=1` on **Hillpointe - Pointe Grand Augusta**:

- **AR transactions present:** 130 transactions on first lease
- Fields: `['id', 'transactionTypeId', 'chargeCodeId', 'chargeCodeName', 'leaseIntervalId', 'description', 'transactionDate', 'postDate', 'postMonth', 'balanceDue', 'amount', 'amountPaid']`
- Sample:
```json
{
  "id": 21201440,
  "transactionTypeId": "2",
  "chargeCodeId": 25839,
  "chargeCodeName": "Rent",
  "leaseIntervalId": 68320,
  "description": "Posted from 03/01/2026 to 03/31/2026",
  "transactionDate": "02/25/2026",
  "postDate": "03/01/2026",
  "postMonth": "03/01/2026",
  "balanceDue": 1455,
  "amount": 1455,
  "amountPaid": 0
}
```

### Critical Finding: Pagination Truncation — ✅ FIXED (March 2026)

> **Update (2026-03-18):** This issue has been fixed in `app.py`. Pagination now uses `per_page = 500` with a proper loop (`while page_no <= max_pages`, breaks when `len(leases) < per_page`). The findings below were based on the OLD 100-per-page limit.

**37 out of 43 properties returned exactly 100 leases** (the per-page cap at the time of this investigation). The pagination logic in both this script and `app.py` broke after page 1 because the Entrata API did not return `meta.currentPage`/`meta.lastPage` in the response.

**Impact (historical — now resolved):** We were working with an incomplete dataset. Properties with more than 100 leases were missing data. This affected:
- Revenue calculations (undercounting charges)
- Missing pet rent detection (may have missed tenants who ARE paying on leases 101+)
- Before/after comparisons (skewed by which 100 leases the API returned first)

### Critical Finding: Empty Interval Status — ✅ HANDLED (March 2026)

> **Update (2026-03-18):** `app.py` now has a fallback: when no valid interval IDs are found and the top-level lease status is not in `INVALID_INTERVAL_STATUSES`, the app uses ALL charges for that lease. Empty interval status no longer causes silent data loss.

**100% of scheduled charges** from this PMC have an empty `leaseIntervalId` status. The app's `interval_is_valid` filter expects 'current', 'past', or 'notice'. Since all statuses are empty, this filter would have excluded ALL Entrata charges for this PMC. The investigation fell back to using all charges, and `app.py` now does the same via its fallback logic.

### Critical Finding: Tenant Code Matching is Broken

**0 out of 1,460 profiles matched via tenant_code.** Every Snowflake profile has `tenant_code = None`, meaning the `f_leases.lease_source_external_id:tenant_code` extraction returns NULL for Entrata. All 191 paying matches came through **email matching only**. This is why only 13% of profiles matched as paying -- the primary matching path is completely non-functional for this PMC.

### Critical Finding: AR Transactions ARE Available

The `includeArTransactions=1` parameter **does return actual posted transactions** with fields like `transactionDate`, `postDate`, `amount`, `amountPaid`, and `balanceDue`. This means we can compare scheduled charges against actual posted/collected charges. The app currently only uses `scheduledCharges` but could use `arTransactions` for more accurate revenue data.

### Summary & Recommendations

#### 1. Pre-PS Data Availability

**Status: Unreliable baseline for most properties.**

24/41 properties have insufficient pre-launch data. The Entrata API likely only returns scheduled charges from when the integration was set up, not full historical charge data.

**Recommendations:**
- Don't rely on before/after revenue comparison for Entrata PMCs
- Consider only showing post-launch trends, or marking the before/after comparison as 'insufficient data'
- Investigate whether Entrata has a separate historical charges endpoint

#### 2. Missing Pet Rent Accuracy — ✅ FRESHNESS FILTER IMPLEMENTED (March 2026)

**Status: Stale profile issue mitigated.**

Key issues found:
- **1226 stale profiles**: Tenants with active PetScreening profiles who were no longer in the API (possibly moved out).

> **Update (2026-03-18):** `app.py` now implements `_in_api` freshness checks in 3 places. Profiles not found in the current API data are excluded from the missing pet rent count. This directly addresses the stale profile overcounting identified here.

**Recommendations (remaining):**
- ~~Consider adding a freshness check: exclude profiles where the tenant doesn't appear in current API data~~ ✅ DONE
- Strengthen tenant matching: email is now the primary match method for Entrata (tenant_code remains broken — see #5)
- Add lease_id-level dedup: if ANY customer on a lease has a pet charge, ALL customers on that lease should be excluded

#### 3. Scheduled vs Actual Charges

**Status: Scheduled charges are the only data currently used, but AR transactions are available.**

- 15 zero-amount charges could inflate tenant counts if not filtered
- 98 future-dated charges may be included in current revenue
- `includeArTransactions=1` DOES return actual posted charges with amounts and payment status

**Recommendations:**
- Filter out $0-amount charges from revenue calculations
- Add a note in the UI that Entrata data reflects *scheduled* charges, not actual collections
- Consider capping charge dates to today to exclude future-scheduled charges from current revenue
- **Explore using AR transactions** (`includeArTransactions=1`) for actual revenue data instead of / in addition to scheduled charges

#### 4. Pagination — ✅ FIXED (March 2026)

**Status: Resolved.** `app.py` now uses `per_page = 500` with a proper pagination loop (`while page_no <= max_pages`, breaks when `len(leases) < per_page`).

> **Note:** The Hillpointe findings in this document (37/43 properties truncated) were based on the OLD 100-per-page limit. With 500 per page and proper pagination, these properties are no longer truncated.

**Recommendations (resolved):**
- ~~Fix pagination~~ ✅ DONE — uses 500 per page with full loop
- ~~Request larger page sizes~~ ✅ DONE
- ~~Flag truncated properties~~ No longer needed — pagination handles all pages

#### 5. Tenant Code Matching (CRITICAL)

**Status: Completely broken for Entrata. 0% match rate via tenant_code.**

All 191 paying matches came through email only. The `f_leases.lease_source_external_id:tenant_code` path returns NULL for all Entrata tenants.

> **Note (2026-03-18):** The app uses email as the primary match method for Entrata, which is the correct workaround. The 87% "missing" rate (1269/1460) was also inflated by the 100-lease pagination cap (now fixed — see #4) and stale profiles (now filtered — see #2). Actual missing counts should be significantly lower with these fixes in place.

**Recommendations (remaining):**
- Audit how `lease_source_external_id` is populated for Entrata in the data pipeline
- ~~Make email the primary matching method~~ Already the case in practice
- Consider matching by unit number as an additional signal

#### 6. Interval Status Filter — ✅ HANDLED (March 2026)

**Status: Resolved.** `app.py` now falls back to using all charges when interval statuses are empty, provided the top-level lease status is valid (not in `INVALID_INTERVAL_STATUSES`).

**Recommendations (resolved):**
- ~~For Entrata, skip the interval status filter or treat empty status as valid~~ ✅ DONE — fallback logic implemented
- ~~Use the lease-level status instead~~ ✅ DONE — top-level status is checked when interval statuses are empty

---

## Presentation Data Plan

**Date:** 2026-03-17

### What the boss needs vs what we can deliver

#### 1. "In 2025, we increased pet rent by $X across mutual clients"
**Data source:** `stg_pmc_integrations_entrata__getleases` (scheduled charges)
**Method:** Lift analysis — same 6-and-6 methodology from the app, but scoped to Entrata-only properties with 2025 data
**Query:** See Query A below

#### 2. "At a 5% cap rate, that equates to $X in asset value created"
**Formula:** `annual_pet_rent_increase / 0.05`
**Derived from:** Monthly lift × 12 / 0.05

#### 3. "Enhanced integration generates XX% more pet-related revenue vs basic"
**Data source:** `stg_petscreening__integrations.settings` — need to identify which properties have enhanced (required new flow) vs basic
**Plus:** Same getleases charge data, split by integration type
**Query:** See Query B below

#### 4. "Pet leak" — uncollected pet rent (active HHP not paying)
**Data source:** Same missing-pet-rent logic from the app — `petscreening__user_enriched` with `household + active + compliant` joined against getleases charges
**Query:** See Query C below
