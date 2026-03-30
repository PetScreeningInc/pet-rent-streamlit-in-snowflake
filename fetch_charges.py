"""
Standalone Yardi charge fetcher.
Pulls GetRentroll data for a list of property IDs and saves as CSV.

Usage:
    python fetch_charges.py

Reads Snowflake + Yardi creds from .env in the same directory.
Edit JOBS below to add/change asset owners and property IDs.
"""

import os
import sys
import json
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import requests
import snowflake.connector
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).parent / ".env")

# ═══════════════════════════════════════════════════════════════════════
# CONFIGURE YOUR JOBS HERE
# Each job = one CSV file. Add as many as you need.
# ═══════════════════════════════════════════════════════════════════════
JOBS = [
    {
        "name": "TruAmerica",
        "property_ids": [98802, 26952, 98862, 84053],
    },
    {
        "name": "AEW_Capital",
        "property_ids": [87351, 41247, 41254, 115185, 51206, 134997],
    },
]

# How far back to pull charge data (months)
LOOKBACK_MONTHS = 120  # 10 years — matches the app default

# Output directory
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "auto_exports")

# ═══════════════════════════════════════════════════════════════════════
# Snowflake
# ═══════════════════════════════════════════════════════════════════════

def get_snowflake_connection():
    return snowflake.connector.connect(
        account=os.getenv("SNOWFLAKE_ACCOUNT"),
        user=os.getenv("SNOWFLAKE_USER"),
        password=os.getenv("SNOWFLAKE_PASSWORD"),
        warehouse=os.getenv("SNOWFLAKE_WAREHOUSE"),
        role=os.getenv("SNOWFLAKE_ROLE"),
    )


def load_properties_for_ids(conn, property_ids):
    """Get Yardi API credentials for a list of property IDs."""
    cur = conn.cursor(snowflake.connector.DictCursor)
    id_list = ",".join(str(pid) for pid in property_ids)
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
        WHERE i.system = 'yardi'
          AND p.property_source_name = 'yardi'
          AND p.property_id IN ({id_list})
        ORDER BY p.property_name
    """)
    return cur.fetchall()


# ═══════════════════════════════════════════════════════════════════════
# Yardi SOAP API
# ═══════════════════════════════════════════════════════════════════════

YARDI_LICENSE_TOKEN = os.getenv("YARDI_LICENSE_TOKEN", "")
SOAP_ACTION = "http://tempuri.org/YSI.Interfaces.WebServices/ItfResidentData/GetRentroll"
SOAP_HEADERS = {
    "Content-Type": "text/xml; charset=utf-8",
    "SOAPAction": SOAP_ACTION,
}


def build_soap_payload(row, move_date, charge_from, charge_to):
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
        <InterfaceLicense>{YARDI_LICENSE_TOKEN}</InterfaceLicense>
        <YardiPropertyId>{row['PROPERTY_CODE']}</YardiPropertyId>
        <MoveIn>{move_date}</MoveIn>
        <MoveOut>{move_date}</MoveOut>
        <LeaseChgFrom>{charge_from}</LeaseChgFrom>
        <LeaseChgTo>{charge_to}</LeaseChgTo>
    </GetRentroll>
  </soap:Body>
</soap:Envelope>"""


def _strip_ns(tag):
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


def extract_property(xml_text):
    parsed = {_strip_ns(ET.fromstring(xml_text).tag): _etree_to_obj(ET.fromstring(xml_text))}
    try:
        docs = (
            parsed.get("Envelope", {})
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


def extract_charges_from_property(prop_data, property_row, asset_name):
    """Extract ALL lease charges from a property's rent roll into flat rows."""
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
                    "parent_company": asset_name,
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


def fetch_single_property(prop_row, asset_name, move_date, charge_from, charge_to):
    """Fetch rent roll for one property. Returns (charges_list, status_dict)."""
    pid = prop_row["PROPERTY_ID"]
    pname = prop_row["PROPERTY_NAME"]
    pcode = prop_row["PROPERTY_CODE"]
    url = prop_row.get("RESIDENT_DATA_URL")

    if not url or not pcode:
        return [], {"property_id": pid, "property_name": pname, "status": "Warning: missing URL or property_code"}

    payload = build_soap_payload(prop_row, move_date, charge_from, charge_to)
    try:
        resp = requests.post(url, data=payload, headers=SOAP_HEADERS, timeout=120)
        resp.raise_for_status()
    except requests.Timeout:
        return [], {"property_id": pid, "property_name": pname, "status": "Error: timeout (120s)"}
    except requests.RequestException as e:
        return [], {"property_id": pid, "property_name": pname, "status": f"Error: {e}"}

    prop_data = extract_property(resp.text)
    if not prop_data:
        return [], {"property_id": pid, "property_name": pname, "status": "Warning: no data / invalid access"}

    charges = extract_charges_from_property(prop_data, prop_row, asset_name)
    return charges, {"property_id": pid, "property_name": pname, "status": f"Success: {len(charges)} charges"}


# ═══════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════

def run_job(conn, job):
    name = job["name"]
    property_ids = job["property_ids"]

    print(f"\n{'='*60}")
    print(f"  {name}  —  {len(property_ids)} properties")
    print(f"{'='*60}")

    # 1. Get credentials from Snowflake
    print(f"  Querying Snowflake for property credentials...")
    properties = load_properties_for_ids(conn, property_ids)

    found_ids = {p["PROPERTY_ID"] for p in properties}
    missing_ids = [pid for pid in property_ids if pid not in found_ids]
    if missing_ids:
        print(f"  ⚠️  Missing Yardi integrations for property IDs: {missing_ids}")
    print(f"  Found {len(properties)} properties with Yardi API access")

    if not properties:
        print(f"  ❌ No properties to fetch. Skipping.")
        return

    # 2. Compute date range
    now = datetime.now()
    lookback_date = (now - timedelta(days=LOOKBACK_MONTHS * 30)).strftime("%m/%d/%Y")
    today_str = now.strftime("%m/%d/%Y")

    # 3. Fetch in parallel
    print(f"  Fetching Yardi GetRentroll (lookback: {LOOKBACK_MONTHS} months)...")
    all_charges = []
    fetch_log = []

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {
            executor.submit(
                fetch_single_property, p, name, lookback_date, lookback_date, today_str
            ): p
            for p in properties
        }
        for i, future in enumerate(as_completed(futures), 1):
            charges, status = future.result()
            all_charges.extend(charges)
            fetch_log.append(status)
            marker = "✅" if status["status"].startswith("Success") else ("⚠️" if "Warning" in status["status"] else "❌")
            print(f"    [{i}/{len(properties)}] {marker} {status['property_name']} — {status['status']}")

    # 4. Save CSV
    if all_charges:
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        ts = now.strftime("%Y%m%d_%H%M%S")
        filename = f"charges_{name}_{ts}.csv"
        filepath = os.path.join(OUTPUT_DIR, filename)
        df = pd.DataFrame(all_charges)
        df.to_csv(filepath, index=False)

        success_count = sum(1 for s in fetch_log if s["status"].startswith("Success"))
        print(f"\n  ✅ Saved {len(all_charges):,} charges → {filepath}")
        print(f"     {success_count}/{len(properties)} properties returned data")
        print(f"     Columns: {list(df.columns)}")
    else:
        print(f"\n  ❌ No charges returned for any property in {name}.")

    return fetch_log


def main():
    print("Connecting to Snowflake...")
    conn = get_snowflake_connection()
    print("✅ Connected\n")

    for job in JOBS:
        try:
            run_job(conn, job)
        except Exception as e:
            print(f"\n❌ Job '{job['name']}' failed: {e}")
            import traceback
            traceback.print_exc()

    conn.close()
    print(f"\n{'='*60}")
    print(f"  All done. CSVs saved to: {OUTPUT_DIR}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
