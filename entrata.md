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