"""Yardi: Snowflake property/parent queries + live GetRentroll SOAP fetch."""

import xml.etree.ElementTree as ET
from services.snowflake_io import get_app_secret as _get_app_secret
from datetime import datetime
import requests
import snowflake.connector
import streamlit as st
from datetime import timedelta
from datetime import timezone
from services.snowflake_io import get_snowflake_connection

YARDI_LICENSE_TOKEN = _get_app_secret("YARDI_LICENSE_TOKEN", "")

# ─── SOAP / API helpers ─────────────────────────────────────────────
SOAP_ACTION = "http://tempuri.org/YSI.Interfaces.WebServices/ItfResidentData/GetRentroll"
SOAP_HEADERS = {
    "Content-Type": "text/xml; charset=utf-8",
    "SOAPAction": SOAP_ACTION,
}


def build_soap_payload(row: dict, license_token: str,
                       move_date: str, charge_from: str, charge_to: str) -> str:
    """Build SOAP XML for GetRentroll.

    Parameters
    ----------
    move_date : str   – earliest MoveIn/MoveOut date (tenant filter)
    charge_from : str – earliest lease-charge FromDate to return
    charge_to : str   – latest date for lease-charge range (usually today)
    """
    return f"""<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
               xmlns:xsd="http://www.w3.org/2001/XMLSchema"
               xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <GetRentroll xmlns="http://tempuri.org/YSI.Interfaces.WebServices/ItfResidentData">
        <UserName>{row['USER_NAME']}</UserName>
        <Password>{row['PASSWORD']}</Password>
        <ServerName>{row['SERVER_NAME']}</ServerName>
        <Database>{row['DATABASE_NAME']}</Database>
        <Platform>Yardi</Platform>
        <InterfaceEntity>PetScreening</InterfaceEntity>
        <InterfaceLicense>{license_token}</InterfaceLicense>
        <YardiPropertyId>{row['PROPERTY_CODE']}</YardiPropertyId>
        <MoveIn>{move_date}</MoveIn>
        <MoveOut>{move_date}</MoveOut>
        <LeaseChgFrom>{charge_from}</LeaseChgFrom>
        <LeaseChgTo>{charge_to}</LeaseChgTo>
    </GetRentroll>
  </soap:Body>
</soap:Envelope>"""


def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag


def _etree_to_obj(elem):
    children = list(elem)
    if not children:
        return (elem.text or "").strip()
    obj = {}
    for child in children:
        tag = _strip_ns(child.tag)
        val = _etree_to_obj(child)
        if tag in obj:
            if not isinstance(obj[tag], list):
                obj[tag] = [obj[tag]]
            obj[tag].append(val)
        else:
            obj[tag] = val
    return obj


def xml_to_dict(xml_text: str):
    root = ET.fromstring(xml_text)
    return {_strip_ns(root.tag): _etree_to_obj(root)}


def extract_property(parsed_payload):
    try:
        docs = (
            parsed_payload
            .get("Envelope", {})
            .get("Body", {})
            .get("GetRentrollResponse", {})
            .get("GetRentrollResult", {})
            .get("XmlDocument", [])
        )
        if not isinstance(docs, list):
            docs = [docs]
        for doc in docs:
            props = doc.get("Properties")
            if isinstance(props, dict):
                prop = props.get("Property")
                if prop:
                    return prop
        return None
    except Exception:
        return None


def extract_charges_from_property(prop_data, property_row):
    """Extract ALL lease charges (flat rows) from a property's rent roll."""
    rows = []
    if not isinstance(prop_data, dict):
        return rows

    units = prop_data.get("Units", {})
    if not isinstance(units, dict):
        return rows
    unit_list = units.get("Unit", [])
    if not isinstance(unit_list, list):
        unit_list = [unit_list]

    for unit in unit_list:
        if not isinstance(unit, dict):
            continue
        unit_code = unit.get("UnitCode", "")
        unit_type = unit.get("UnitType", "")
        market_rent = unit.get("MarketRent", "")

        tenants_raw = unit.get("Tenants", {})
        if not isinstance(tenants_raw, dict):
            continue
        tenant_list = tenants_raw.get("Tenant", [])
        if not isinstance(tenant_list, list):
            tenant_list = [tenant_list]

        for tenant in tenant_list:
            if not isinstance(tenant, dict):
                continue
            tenant_code = tenant.get("TenantCode", "")
            first_name = tenant.get("FirstName", "")
            last_name = tenant.get("LastName", "")
            tenant_status = tenant.get("TenantStatus", "")
            lease_from = tenant.get("LeaseFrom", "")
            lease_to = tenant.get("LeaseTo", "")
            move_in = tenant.get("MoveIn", "")
            move_out = tenant.get("MoveOut", "")
            email = tenant.get("Email", "")

            charges = tenant.get("LeaseCharges", "")
            if not charges or not isinstance(charges, dict):
                continue
            charge_list = charges.get("LeaseCharge", [])
            if not isinstance(charge_list, list):
                charge_list = [charge_list]

            for charge in charge_list:
                if not isinstance(charge, dict):
                    continue
                rows.append({
                    "parent_company": property_row["PARENT_COMPANY_NAME"],
                    "property_id": property_row["PROPERTY_ID"],
                    "property_name": property_row["PROPERTY_NAME"],
                    "property_code": property_row["PROPERTY_CODE"],
                    "launch_date": property_row.get("PROPERTY_LAUNCH_DATE"),
                    "unit_code": unit_code,
                    "unit_type": unit_type,
                    "market_rent": market_rent,
                    "tenant_code": tenant_code,
                    "first_name": first_name,
                    "last_name": last_name,
                    "tenant_status": tenant_status,
                    "lease_from": lease_from,
                    "lease_to": lease_to,
                    "move_in": move_in,
                    "move_out": move_out,
                    "email": email,
                    "charge_code": charge.get("ChargeCode", ""),
                    "charge_type": charge.get("ChargeType", ""),
                    "charge_amount": charge.get("ChargeAmount", ""),
                    "charge_from_date": charge.get("FromDate", ""),
                    "charge_to_date": charge.get("ToDate", ""),
                })
    return rows
# ─── Snowflake queries ───────────────────────────────────────────────
@st.cache_data(ttl=300)
def load_parent_companies():
    """Count ALL properties per parent company from d_properties (total + yardi-integrated)."""
    conn = get_snowflake_connection()
    cur = conn.cursor(snowflake.connector.DictCursor)
    cur.execute("""
        SELECT
            p.parent_company_name,
            MAX(p.parent_company_ancestry_id)    AS ancestry_id,
            MAX(p.parent_company_ancestry_name)   AS ancestry_name,
            COUNT(DISTINCT p.property_id)         AS total_props,
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
    """)
    return cur.fetchall()


@st.cache_data(ttl=300)
def load_all_properties():
    conn = get_snowflake_connection()
    cur = conn.cursor(snowflake.connector.DictCursor)
    cur.execute("""
        SELECT DISTINCT
            p.property_id,
            p.property_name,
            p.parent_company_name,
            p.parent_company_ancestry_id
        FROM PROD.COMMON.D_PROPERTIES p
        WHERE p.property_source_name = 'yardi'
        ORDER BY p.property_name
    """)
    return cur.fetchall()


def load_properties_for_selection(parent_company_name=None, property_id=None, ancestry_id=None):
    conn = get_snowflake_connection()
    cur = conn.cursor(snowflake.connector.DictCursor)

    where_clause = """
        WHERE i.system = 'yardi'
          AND p.property_source_name = 'yardi'
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
        {where_clause}
        ORDER BY p.property_name
    """)
    return cur.fetchall()
# ─── Yardi API fetch ─────────────────────────────────────────────────
def fetch_rentroll_for_properties(properties, progress_bar, status_text, lookback_months=24):
    """Call GetRentroll API for each property, return all charge rows.

    IMPORTANT: The API date parameters are ALWAYS set to the widest useful
    range (10 years back) regardless of the display lookback slider.
    This guarantees that the same charge rows are returned no matter what
    display window the user picks, so numbers stay consistent.
    The ``lookback_months`` argument is kept for interface compat but is
    NOT used for the API call dates.
    """
    today = datetime.now(timezone.utc).date()

    # Always fetch the widest useful window from Yardi so the data never
    # changes when the user adjusts the display slider.
    move_date    = (today - timedelta(days=10 * 365)).strftime("%Y-%m-%d")   # 10 years
    charge_from  = "2000-01-01"                                               # very early
    charge_to    = today.strftime("%Y-%m-%d")                                 # today

    all_charges = []
    results_log = []

    for i, prop in enumerate(properties):
        prop_name = prop['PROPERTY_NAME']
        prop_code = prop['PROPERTY_CODE']
        progress = (i + 1) / len(properties)
        progress_bar.progress(progress)
        status_text.text(f"[{i+1}/{len(properties)}] Fetching {prop_name} ({prop_code})...")

        try:
            payload = build_soap_payload(prop, YARDI_LICENSE_TOKEN, move_date, charge_from, charge_to)
            resp = requests.post(
                prop['RESIDENT_DATA_URL'],
                data=payload,
                headers=SOAP_HEADERS,
                timeout=120,
            )

            if resp.status_code != 200:
                results_log.append({"property": prop_name, "code": prop_code, "status": f"Error: HTTP {resp.status_code}", "charges": 0})
                continue

            parsed = xml_to_dict(resp.text)
            extracted = extract_property(parsed)

            if not extracted:
                error_msg = "No data"
                for m in ET.fromstring(resp.text).iter():
                    if "Message" in m.tag and m.text:
                        error_msg = m.text.strip()[:60]
                        break
                results_log.append({"property": prop_name, "code": prop_code, "status": f"Warning: {error_msg}", "charges": 0})
                continue

            charges = extract_charges_from_property(extracted, prop)
            all_charges.extend(charges)
            results_log.append({"property": prop_name, "code": prop_code, "status": "Success", "charges": len(charges)})

        except requests.exceptions.Timeout:
            results_log.append({"property": prop_name, "code": prop_code, "status": "Error: Timeout", "charges": 0})
        except Exception as exc:
            results_log.append({"property": prop_name, "code": prop_code, "status": f"Error: {str(exc)[:50]}", "charges": 0})

    progress_bar.progress(1.0)
    status_text.text("Done!")
    return all_charges, results_log
