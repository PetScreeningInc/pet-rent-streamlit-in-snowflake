"""Entrata: Snowflake property/parent queries + live getLeases REST fetch."""

from services.snowflake_io import get_app_secret as _get_app_secret
import json
import requests
import snowflake.connector
import streamlit as st
from services.snowflake_io import get_snowflake_connection

ENTRATA_API_KEY = _get_app_secret("ENTRATA_API_KEY", "")
ENTRATA_BASE_URL = "https://apis.entrata.com/ext/orgs"
# ─── Entrata Snowflake queries ────────────────────────────────────────
@st.cache_data(ttl=300)
def load_entrata_parent_companies():
    """Count ALL properties per parent company that have Entrata integrations.

    total_props = every property under the parent company in d_properties
    api_props   = only those with active Entrata API integrations
    """
    conn = get_snowflake_connection()
    cur = conn.cursor(snowflake.connector.DictCursor)
    cur.execute("""
        SELECT
            p.parent_company_name,
            MAX(p.parent_company_ancestry_id)    AS ancestry_id,
            MAX(p.parent_company_ancestry_name)   AS ancestry_name,
            COUNT(DISTINCT p.property_id)         AS total_props,
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
    """)
    return cur.fetchall()


@st.cache_data(ttl=300)
def load_entrata_all_properties():
    conn = get_snowflake_connection()
    cur = conn.cursor(snowflake.connector.DictCursor)
    cur.execute("""
        SELECT DISTINCT
            p.property_id,
            p.property_name,
            p.parent_company_name,
            p.parent_company_ancestry_id
        FROM PROD.COMMON.D_PROPERTIES p
        WHERE p.property_source_name = 'entrata'
        ORDER BY p.property_name
    """)
    return cur.fetchall()


def load_entrata_properties_for_selection(parent_company_name=None, property_id=None, ancestry_id=None):
    conn = get_snowflake_connection()
    cur = conn.cursor(snowflake.connector.DictCursor)

    where_clause = """
        WHERE i.system = 'entrata'
          AND p.property_source_name = 'entrata'
          AND i.state = 'enabled'
          AND p.integration_status = 'enabled'
          AND p.property_status = 'active'
          AND PARSE_JSON(p.property_source_id):"property_id"::STRING IS NOT NULL
          AND PARSE_JSON(i.settings):"corp_id"::STRING IS NOT NULL
    """
    if parent_company_name:
        where_clause += f" AND p.parent_company_name = '{parent_company_name}'"
    if property_id:
        where_clause += f" AND p.property_id = {property_id}"
    if ancestry_id:
        where_clause += f" AND p.parent_company_ancestry_id = '{ancestry_id}'"

    cur.execute(f"""
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
        {where_clause}
        ORDER BY p.property_name
    """)
    return cur.fetchall()


# ─── Entrata API fetch ────────────────────────────────────────────────
def _extract_charges_from_entrata_lease(lease_obj, property_row):
    """Flatten an Entrata lease JSON into rows matching the Yardi all_charges_df shape.

    Key Entrata-specific handling vs Yardi:

    1. **Interval filtering** — Entrata leases can contain multiple intervals
       (original, renewals, cancelled attempts). We only include charges from
       intervals with status Current / Past / Notice.  Cancelled / Applicant /
       Future intervals are dropped so we don't count revenue from leases that
       never went live.

    2. **Charge→interval linkage** — Each scheduledCharge carries a
       leaseIntervalId that ties it to a specific interval.  We use this to
       skip charges attached to cancelled renewal attempts.

    3. **Deduplication key** — Charges belong to the *lease*, not an individual
       customer.  Every customer row for the same lease has identical charges.
       We still emit one row per customer×charge (so all customers appear in
       the paying-set for missing-pet-rent matching), but stamp each row with
       ``_entrata_charge_dedup_key`` so downstream revenue aggregation can
       ``drop_duplicates`` before summing.

    4. **Frequency-based end-date logic** — Entrata provides a ``frequency``
       field on each charge.  One-Time charges get charge_to = charge_from
       (same day) so they don't get spread across months.  Monthly charges
       with no endDate fall back to the interval's lease_to.

    5. **Status field choice** — We filter on ``leaseIntervalStatus`` (did the
       lease go live?) NOT ``leaseCustomerStatus`` (is this person still on
       the lease?).  A guarantor with customerStatus="Cancelled" on a
       "Notice" lease should not cause us to drop the lease's charges.
    """
    rows = []

    # ── Parse customers ────────────────────────────────────────────────
    customers_data = lease_obj.get("customers", {})
    customers = customers_data.get("customer", [])
    if isinstance(customers, dict):
        customers = [customers]
    if not customers:
        return rows

    # ── Parse lease intervals — find valid ones ────────────────────────
    VALID_INTERVAL_STATUSES = {"current", "past", "notice"}
    INVALID_INTERVAL_STATUSES = {"cancelled", "applicant", "denied", "future"}

    intervals_data = lease_obj.get("leaseIntervals", {})
    intervals = intervals_data.get("leaseInterval", [])
    if isinstance(intervals, dict):
        intervals = [intervals]

    valid_interval_ids = set()
    interval_info = {}  # interval_id → {status, startDate, endDate}
    for iv in intervals:
        iv_id = str(iv.get("id", ""))
        iv_status = str(iv.get("status", "")).strip().lower()
        if iv_status not in INVALID_INTERVAL_STATUSES:
            valid_interval_ids.add(iv_id)
            interval_info[iv_id] = {
                "status": iv_status,
                "start": iv.get("startDate", ""),
                "end": iv.get("endDate", ""),
            }

    # Fallback: if no detailed intervals, check top-level lease fields
    top_level_status = str(lease_obj.get("leaseIntervalStatus", "")).strip().lower()
    top_level_interval_id = str(lease_obj.get("leaseIntervalId", ""))
    if not valid_interval_ids and top_level_status not in INVALID_INTERVAL_STATUSES:
        valid_interval_ids.add(top_level_interval_id)
        interval_info[top_level_interval_id] = {
            "status": top_level_status,
            "start": "",
            "end": "",
        }

    if not valid_interval_ids:
        return rows

    # ── Parse lease activities for date fallbacks ──────────────────────
    activities_data = lease_obj.get("leaseActivities", {})
    activities = activities_data.get("leasesActivity", [])
    if isinstance(activities, dict):
        activities = [activities]
    act_dates = {}
    for act in activities:
        et = act.get("eventType", "")
        dt = act.get("date", "")
        if et and dt:
            act_dates[et] = dt

    lease_from_fallback = act_dates.get("Lease From", "")
    lease_to_fallback = act_dates.get("Lease To", "")

    lease_id = str(lease_obj.get("id", ""))
    unit_number = lease_obj.get("unitNumberSpace", "")
    unit_id = lease_obj.get("unitId", "")

    # ── Parse scheduled charges — keep only valid-interval charges ─────
    charges_data = lease_obj.get("scheduledCharges", {})
    charges = charges_data.get("scheduledCharge", [])
    if isinstance(charges, dict):
        charges = [charges]

    valid_charges = []
    for ch in charges:
        ch_interval_id = str(ch.get("leaseIntervalId", ""))

        # If the charge has an interval ID, skip unless that interval is valid
        if ch_interval_id and ch_interval_id not in valid_interval_ids:
            continue

        iv = interval_info.get(ch_interval_id, {})
        iv_lease_from = iv.get("start", "") or lease_from_fallback
        iv_lease_to = iv.get("end", "") or lease_to_fallback

        frequency = ch.get("frequency", "")
        charge_from = ch.get("startDate", "")
        charge_to = ch.get("endDate", "")

        # ── Frequency-based end-date logic ──
        freq_lower = (frequency or "").strip().lower()
        if freq_lower == "one-time":
            # One-time charges: same day, do NOT spread across months
            charge_to = charge_from
        elif not charge_to or str(charge_to).strip() == "":
            # Monthly charges missing endDate → fall back to interval lease_to
            charge_to = iv_lease_to

        valid_charges.append({
            "charge_code": ch.get("chargeCode", ""),
            "charge_type": ch.get("chargeType", ""),
            "charge_amount": ch.get("amount", ""),
            "charge_from_date": charge_from,
            "charge_to_date": charge_to,
            "frequency": frequency,
            "lease_interval_id": ch_interval_id,
            "iv_lease_from": iv_lease_from,
            "iv_lease_to": iv_lease_to,
        })

    # ── Emit rows: every customer × every valid charge ─────────────────
    #
    # WHY emit for all customers (not just primary)?
    # Charges belong to the lease, but downstream _build_paying_sets needs
    # every customer's email/tenant_code in the paying set so roommates
    # aren't flagged as "missing pet rent".  Revenue dedup happens later
    # via _entrata_charge_dedup_key.
    for cust in customers:
        first_name = cust.get("firstName", "")
        last_name = cust.get("lastName", "")
        customer_id = cust.get("id", "")
        customer_type = cust.get("customerType", "")
        customer_status = cust.get("leaseCustomerStatus", "")
        move_in = cust.get("moveInDate", "") or act_dates.get("Actual Move In", "")
        move_out = cust.get("moveOutDate", "") or act_dates.get("Actual Move Out", "")

        # Email: try direct field, then addresses.address
        email = cust.get("email", "")
        if not email:
            addrs = cust.get("addresses", {})
            if isinstance(addrs, dict):
                addr = addrs.get("address", {})
                if isinstance(addr, dict):
                    email = addr.get("email", "") or addr.get("additionalEmail", "")

        base_row = {
            "parent_company": property_row["PARENT_COMPANY_NAME"],
            "property_id": property_row["PROPERTY_ID"],
            "property_name": property_row["PROPERTY_NAME"],
            "property_code": property_row["PROPERTY_CODE"],
            "launch_date": property_row.get("PROPERTY_LAUNCH_DATE"),
            "unit_code": unit_number or unit_id,
            "unit_type": "",
            "market_rent": "",
            "lease_id": lease_id,
            "tenant_code": customer_id,
            "first_name": first_name,
            "last_name": last_name,
            "tenant_status": customer_status,
            "move_in": move_in,
            "move_out": move_out,
            "email": email,
        }

        if not valid_charges:
            # Tenant-only row (for reports, even when no charges)
            rows.append({
                **base_row,
                "lease_from": lease_from_fallback,
                "lease_to": lease_to_fallback,
                "charge_code": "",
                "charge_type": "",
                "charge_amount": "",
                "charge_from_date": "",
                "charge_to_date": "",
                "frequency": "",
                "lease_interval_id": "",
                "_entrata_charge_dedup_key": "",
            })
            continue

        for ch in valid_charges:
            dedup_key = (
                f"{lease_id}|{ch['lease_interval_id']}|"
                f"{ch['charge_code']}|{ch['charge_from_date']}|{ch['charge_amount']}"
            )
            rows.append({
                **base_row,
                "lease_from": ch["iv_lease_from"],
                "lease_to": ch["iv_lease_to"],
                "charge_code": ch["charge_code"],
                "charge_type": ch["charge_type"],
                "charge_amount": ch["charge_amount"],
                "charge_from_date": ch["charge_from_date"],
                "charge_to_date": ch["charge_to_date"],
                "frequency": ch["frequency"],
                "lease_interval_id": ch["lease_interval_id"],
                "_entrata_charge_dedup_key": dedup_key,
            })

    return rows


def _extract_ar_charges_from_entrata_lease(lease_obj, property_row):
    """Extract AR (actual posted) transactions from an Entrata lease.

    Returns a list of dicts with actual posted charges including amount paid.
    Only pet-related charge codes are kept (matched case-insensitively against
    common pet charge code names).
    """
    PET_KEYWORDS = {
        'pet rent', 'pet fee', 'pet deposit', 'pet damage',
        'animal rent', 'animal fee', 'animal deposit',
        'pet', 'pet charge', 'monthly pet', 'pet premium',
        'companion animal', 'esa fee', 'esa rent',
    }
    rows = []
    ar_data = lease_obj.get("arTransactions", {})
    if not ar_data:
        return rows
    transactions = ar_data.get("arTransaction", [])
    if isinstance(transactions, dict):
        transactions = [transactions]

    lease_id = str(lease_obj.get("id", ""))
    unit_number = lease_obj.get("unitNumberSpace", "")

    PARTIAL_KEYWORDS = {'pet', 'animal', 'companion'}
    for txn in transactions:
        code_name = str(txn.get("chargeCodeName", "")).strip()
        if not code_name:
            continue
        code_lower = code_name.lower()
        # Match exact keywords OR any partial substring match
        if code_lower not in PET_KEYWORDS and not any(kw in code_lower for kw in PARTIAL_KEYWORDS):
            continue
        rows.append({
            "property_id": property_row["PROPERTY_ID"],
            "property_name": property_row["PROPERTY_NAME"],
            "launch_date": property_row.get("PROPERTY_LAUNCH_DATE"),
            "lease_id": lease_id,
            "unit": unit_number,
            "charge_code_name": code_name,
            "charge_code_id": txn.get("chargeCodeId", ""),
            "amount": txn.get("amount", 0),
            "amount_paid": txn.get("amountPaid", 0),
            "balance_due": txn.get("balanceDue", 0),
            "post_date": txn.get("postDate", ""),
            "post_month": txn.get("postMonth", ""),
            "transaction_date": txn.get("transactionDate", ""),
            "description": txn.get("description", ""),
        })
    return rows


def _fetch_entrata_leases_for_property(prop, api_key):
    """Call Entrata getLeases for a single property with pagination. Returns (leases_list, error_msg)."""
    corp_id = prop.get("CORP_ID")
    entrata_pid = prop.get("ENTRATA_PROPERTY_ID") or prop.get("PROPERTY_CODE")

    if not corp_id or not entrata_pid:
        return [], "Missing corp_id or entrata_property_id"

    headers = {
        "Content-Type": "application/json",
        "X-Api-Key": api_key,
    }

    all_leases = []
    page_no = 1
    per_page = 500
    max_pages = 200

    while page_no <= max_pages:
        api_url = f"{ENTRATA_BASE_URL}/{corp_id}/v1/leases?page_no={page_no}&per_page={per_page}"

        request_body = {
            "auth": {"type": "apikey"},
            "requestId": page_no,
            "method": {
                "name": "getLeases",
                "version": "r1",
                "params": {
                    "propertyId": entrata_pid,
                    "includeArTransactions": 1,
                    "includeLeaseHistory": 1,
                    "includeScheduledCharges": 1,  # Explicitly request scheduled charges
                    "leaseStatusTypeIds": "1,2,3,4,5,6",  # All statuses for full history
                },
            },
        }

        try:
            resp = requests.post(api_url, json=request_body, headers=headers, timeout=120)
            if resp.status_code == 401:
                return [], "401 Unauthorized — check API key"
            if resp.status_code == 403:
                return [], f"403 Forbidden — corp_id={corp_id}"
            resp.raise_for_status()
        except requests.exceptions.Timeout:
            return all_leases, f"Timeout on page {page_no}" if all_leases else "Timeout"
        except requests.exceptions.HTTPError as e:
            return all_leases, str(e)[:80]
        except Exception as e:
            return all_leases, str(e)[:80]

        data = resp.json()
        response_obj = data.get("response", {})
        if response_obj.get("code") != 200:
            err = response_obj.get("message", f"API code {response_obj.get('code')}")
            return all_leases, err if not all_leases else None

        result = response_obj.get("result", {})
        leases_data = result.get("leases", {})
        leases = leases_data.get("lease", [])
        if isinstance(leases, dict):
            leases = [leases]

        all_leases.extend(leases)

        if len(leases) < per_page:
            break
        meta = result.get("meta", {})
        last_page = meta.get("lastPage", 0)
        if last_page > 0 and meta.get("currentPage", 0) >= last_page:
            break
        page_no += 1

    return all_leases, None


def fetch_entrata_for_properties(properties, progress_bar, status_text, lookback_months=24):
    """Call Entrata getLeases API for each property, return normalized charge rows.

    Returns (all_charges, results_log, all_ar_charges) tuple.  The first two
    match the Yardi equivalent so downstream code works unchanged.
    all_ar_charges contains actual posted AR transactions for comparison.
    """
    all_charges = []
    all_ar_charges = []
    all_raw_lease_arrays = []
    results_log = []

    for i, prop in enumerate(properties):
        prop_name = prop['PROPERTY_NAME']
        prop_code = prop.get('ENTRATA_PROPERTY_ID') or prop.get('PROPERTY_CODE', '')
        progress = (i + 1) / len(properties)
        progress_bar.progress(progress)
        status_text.text(f"[{i+1}/{len(properties)}] Fetching {prop_name} (Entrata {prop_code})...")

        leases, error_msg = _fetch_entrata_leases_for_property(prop, ENTRATA_API_KEY)
        status_text.text(f"[{i+1}/{len(properties)}] {prop_name}: {len(leases)} leases, extracting charges...")

        if error_msg and not leases:
            results_log.append({
                "property": prop_name, "code": prop_code,
                "status": f"Error: {error_msg}", "charges": 0,
            })
            continue

        charges = []
        ar_charges_prop = []
        for lease in leases:
            charges.extend(_extract_charges_from_entrata_lease(lease, prop))
            ar_charges_prop.extend(_extract_ar_charges_from_entrata_lease(lease, prop))

            # Capture raw arrays for export
            lease_id = str(lease.get("id", ""))
            unit = lease.get("unitNumberSpace", "")
            # Get primary customer name
            _custs = lease.get("customers", {}).get("customer", [])
            if isinstance(_custs, dict):
                _custs = [_custs]
            _primary = _custs[0] if _custs else {}
            _tenant_name = f"{_primary.get('firstName', '')} {_primary.get('lastName', '')}".strip()
            _tenant_status = _primary.get("leaseCustomerStatus", "")

            # Raw scheduled charges array
            _sched_raw = lease.get("scheduledCharges", {}).get("scheduledCharge", [])
            if isinstance(_sched_raw, dict):
                _sched_raw = [_sched_raw]
            # Raw AR transactions array
            _ar_raw = lease.get("arTransactions", {}).get("arTransaction", [])
            if isinstance(_ar_raw, dict):
                _ar_raw = [_ar_raw]

            all_raw_lease_arrays.append({
                "property_name": prop_name,
                "property_code": prop_code,
                "lease_id": lease_id,
                "unit": unit,
                "tenant": _tenant_name,
                "tenant_status": _tenant_status,
                "n_scheduled_charges": len(_sched_raw),
                "n_ar_transactions": len(_ar_raw),
                "scheduled_charges_json": json.dumps(_sched_raw, default=str),
                "ar_transactions_json": json.dumps(_ar_raw, default=str),
            })

        if charges:
            all_charges.extend(charges)
            all_ar_charges.extend(ar_charges_prop)
            status = f"Success ({len(leases)} leases)"
            if error_msg:
                status += f" (partial: {error_msg})"
            results_log.append({
                "property": prop_name, "code": prop_code,
                "status": status, "charges": len(charges),
            })
        else:
            all_ar_charges.extend(ar_charges_prop)
            results_log.append({
                "property": prop_name, "code": prop_code,
                "status": "Warning: No charges found", "charges": 0,
            })

    progress_bar.progress(1.0)
    status_text.text("Done!")
    return all_charges, results_log, all_ar_charges, all_raw_lease_arrays
