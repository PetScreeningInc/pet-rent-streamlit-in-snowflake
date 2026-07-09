#!/usr/bin/env python3
"""
pm_roster.py — pull current property-management contacts from the PMS APIs.

The problem: properties get bought/sold constantly and internal CRM data on
who manages what goes stale. The PMS is maintained by whoever operates the
property TODAY, so it is the freshest available source.

What each system can give us (verified 2026-07-05, existing credentials —
see docs/property-manager-endpoints.md):

  Yardi    Export_PropertyAttributeData -> named staff by role per property
           (e.g. "Regional Manager: Danielle Allen", "Director of
           Operations: Missy Singer"). Attributes are client-configured in
           Voyager, so coverage varies by client. Names + titles, usually
           no emails.
           GetPropertyConfigurations    -> the client's full current
           property roster (bought/sold detection).
  Entrata  getProperties                -> current office email + website
           per property (self-heals across management changes).
  RealPage nothing on our contract yet (site/property ops not authorized).

Usage:
    python pm_roster.py --ids 1575,1576                  # property ids
    python pm_roster.py --parent "JVM"                   # parent company name
    python pm_roster.py --ancestry 61610                 # ancestry id
    python pm_roster.py --ids 1575 --output ./rosters

Output: pm_roster_<timestamp>.csv with one row per (property, role/field):
    system, property_id, property_name, property_code, field, value, source
"""
from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
import warnings
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

os.environ["STREAMLIT_SERVER_HEADLESS"] = "true"
warnings.filterwarnings("ignore")
logging.getLogger("streamlit").setLevel(logging.ERROR)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests

# Property-attribute names that identify people/roles (case-insensitive
# substring match). Everything else in the attribute set is skipped unless
# --all-attributes is passed.
ROLE_HINTS = (
    "manager", "director", "vp", "svp", "president", "owner", "principal",
    "supervisor", "regional", "asset", "operations", "accountant",
    "accounting", "cfo", "ceo", "coo", "executive", "contact", "email",
    "phone",
)


def _strip_ns(tag):
    return tag.split("}")[-1]


def yardi_soap(row, op, extra, license_token, soap_headers, timeout=90):
    body = f"""<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
               xmlns:xsd="http://www.w3.org/2001/XMLSchema"
               xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <{op} xmlns="http://tempuri.org/YSI.Interfaces.WebServices/ItfResidentData">
        <UserName>{row['USER_NAME']}</UserName>
        <Password>{row['PASSWORD']}</Password>
        <ServerName>{row['SERVER_NAME']}</ServerName>
        <Database>{row['DATABASE_NAME']}</Database>
        <Platform>Yardi</Platform>
        <InterfaceEntity>PetScreening</InterfaceEntity>
        <InterfaceLicense>{license_token}</InterfaceLicense>
        {extra}
    </{op}>
  </soap:Body>
</soap:Envelope>"""
    hdrs = dict(soap_headers)
    hdrs["SOAPAction"] = f"http://tempuri.org/YSI.Interfaces.WebServices/ItfResidentData/{op}"
    return requests.post(row["RESIDENT_DATA_URL"], data=body, headers=hdrs, timeout=timeout)


def yardi_property_attributes(row, license_token, soap_headers, all_attributes=False):
    """(field, value) pairs from Export_PropertyAttributeData for one property."""
    resp = yardi_soap(
        row, "Export_PropertyAttributeData",
        f"<YardiPropertyId>{row['PROPERTY_CODE']}</YardiPropertyId>",
        license_token, soap_headers,
    )
    out = []
    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError:
        return [("_error", f"unparseable response ({resp.status_code})")]
    for attr in root.iter():
        if _strip_ns(attr.tag) != "Attribute":
            continue
        name = value = None
        for child in attr:
            if _strip_ns(child.tag) == "Name":
                name = (child.text or "").strip()
            elif _strip_ns(child.tag) == "Value":
                value = (child.text or "").strip()
        if not name or not value:
            continue
        if all_attributes or any(h in name.lower() for h in ROLE_HINTS):
            out.append((name, value))
    if not out:
        # Surface API-level error messages so a client with no attribute
        # sets is distinguishable from a failed call.
        for msg in root.iter():
            if _strip_ns(msg.tag) == "Message" and (msg.text or "").strip():
                return [("_note", msg.text.strip()[:200])]
    return out


def entrata_property_contact(prop, api_key, base_url):
    """(field, value) pairs from getProperties for one Entrata property."""
    corp_id = prop.get("CORP_ID")
    epid = prop.get("ENTRATA_PROPERTY_ID") or prop.get("PROPERTY_CODE")
    if not corp_id or not epid:
        return [("_error", "missing corp_id / entrata property id")]
    body = {"auth": {"type": "apikey"}, "requestId": 1,
            "method": {"name": "getProperties", "version": "r1",
                       "params": {"propertyIds": str(epid)}}}
    r = requests.post(
        f"{base_url}/{corp_id}/v1/properties", json=body,
        headers={"Content-Type": "application/json", "X-Api-Key": api_key},
        timeout=60,
    )
    out = []
    try:
        pdata = r.json()["response"]["result"]["PhysicalProperty"]["Property"][0]
    except Exception:  # noqa: BLE001
        return [("_error", f"unexpected getProperties response ({r.status_code})")]
    addr = pdata.get("Address") or {}
    if addr.get("Email"):
        out.append(("Office Email", addr["Email"]))
    for a in (pdata.get("Addresses") or {}).get("Address", []) if isinstance((pdata.get("Addresses") or {}).get("Address"), list) else []:
        if a.get("Email") and ("Office Email", a["Email"]) not in out:
            out.append((f"Email ({a.get('@attributes', {}).get('AddressType', 'other')})", a["Email"]))
    if pdata.get("webSite"):
        out.append(("Website", pdata["webSite"]))
    if pdata.get("MarketingName"):
        out.append(("Marketing Name", pdata["MarketingName"]))
    return out or [("_note", "no contact fields returned")]


def ps_crm_email_matches(parent_names, roster_names):
    """Match Yardi roster names against PetScreening's own CRM emails.

    Yardi attributes give NAMES of current managers but no emails; the PS
    permissions table has manager EMAILS (possibly stale). A name match
    between the two means: this CRM contact is confirmed current by the
    operator's own Yardi system. Verified example: 'Danielle Allen'
    (Yardi Regional Manager) -> dallen@nolanliving.com (PS CRM).

    Returns {normalized_full_name: [emails]}.
    """
    if not parent_names or not roster_names:
        return {}
    from snowflake_auth import get_snowflake_connection
    conn = get_snowflake_connection()
    cur = conn.cursor()
    wanted = {n.lower().strip() for n in roster_names if n and " " in n.strip()}
    matches = {}
    try:
        for parent in sorted({p for p in parent_names if p}):
            safe = parent.replace("'", "''")
            cur.execute(f"""
                SELECT DISTINCT u.user_first_name, u.user_last_name, u.user_email
                FROM PROD.staging.stg_petscreening__property_manager_permissions_only pm
                JOIN PROD.petscreening.petscreening__user_enriched u ON u.user_id = pm.user_id
                JOIN PROD.common.d_properties p ON p.property_id = pm.entity_id
                WHERE p.parent_company_name ILIKE '%{safe}%'
                  AND u.user_email IS NOT NULL AND TRIM(u.user_email) <> ''
            """)
            for fn, ln, em in cur.fetchall():
                full = f"{(fn or '').strip()} {(ln or '').strip()}".lower().strip()
                if full in wanted:
                    matches.setdefault(full, [])
                    if em not in matches[full]:
                        matches[full].append(em)
    finally:
        cur.close()
        conn.close()
    return matches


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ids", help="Comma-separated PetScreening property ids")
    ap.add_argument("--parent", help="Parent company name")
    ap.add_argument("--ancestry", help="Parent company ancestry id")
    ap.add_argument("--all-attributes", action="store_true",
                    help="Yardi: include every property attribute, not just role-like ones")
    ap.add_argument("--output", default=".", help="Output folder")
    args = ap.parse_args()
    if not (args.ids or args.parent or args.ancestry):
        ap.error("provide --ids, --parent, or --ancestry")

    from app import (  # noqa: WPS433 — bare-mode import, same as batch scripts
        load_properties_for_selection,
        load_entrata_properties_for_selection,
        YARDI_LICENSE_TOKEN, SOAP_HEADERS, ENTRATA_API_KEY, ENTRATA_BASE_URL,
    )

    kw = {}
    if args.parent:
        kw["parent_company_name"] = args.parent
    if args.ancestry:
        kw["ancestry_id"] = args.ancestry

    rows = []

    def scope(loader):
        if args.ids:
            out = []
            for pid in args.ids.split(","):
                out.extend(loader(property_id=pid.strip()) or [])
            return out
        return loader(**kw) or []

    yardi_props = scope(load_properties_for_selection)
    print(f"Yardi properties in scope: {len(yardi_props)}")
    for p in yardi_props:
        fields = yardi_property_attributes(p, YARDI_LICENSE_TOKEN, SOAP_HEADERS,
                                           all_attributes=args.all_attributes)
        for field, value in fields:
            rows.append({
                "system": "yardi",
                "property_id": p.get("PROPERTY_ID"),
                "property_name": p.get("PROPERTY_NAME"),
                "property_code": p.get("PROPERTY_CODE"),
                "field": field,
                "value": value,
                "source": "Export_PropertyAttributeData",
            })
        print(f"  {p.get('PROPERTY_NAME')}: {len(fields)} fields")

    entrata_props = scope(load_entrata_properties_for_selection)
    print(f"Entrata properties in scope: {len(entrata_props)}")
    for p in entrata_props:
        fields = entrata_property_contact(p, ENTRATA_API_KEY, ENTRATA_BASE_URL)
        for field, value in fields:
            rows.append({
                "system": "entrata",
                "property_id": p.get("PROPERTY_ID"),
                "property_name": p.get("PROPERTY_NAME"),
                "property_code": p.get("PROPERTY_CODE"),
                "field": field,
                "value": value,
                "source": "getProperties",
            })
        print(f"  {p.get('PROPERTY_NAME')}: {len(fields)} fields")

    # ── Name → email enrichment via PetScreening's own CRM ──
    _role_name_rows = [r for r in rows
                       if r["system"] == "yardi" and not r["field"].startswith("_")
                       and " " in str(r["value"]).strip()]
    _parents = set()
    for p in yardi_props:
        # PS convention: property_name is "<Parent> - <Property>"
        _pn = str(p.get("PROPERTY_NAME") or "")
        if " - " in _pn:
            _parents.add(_pn.split(" - ", 1)[0].strip())
    try:
        _matches = ps_crm_email_matches(_parents, [r["value"] for r in _role_name_rows])
    except Exception as _exc:  # noqa: BLE001
        _matches = {}
        print(f"PS CRM email match skipped: {str(_exc)[:120]}")
    _n_matched = 0
    for r in list(_role_name_rows):
        _emails = _matches.get(str(r["value"]).lower().strip())
        if _emails:
            _n_matched += 1
            rows.append({
                "system": "yardi",
                "property_id": r["property_id"],
                "property_name": r["property_name"],
                "property_code": r["property_code"],
                "field": f"{r['field']} — email (PS CRM, name-verified)",
                "value": "; ".join(_emails),
                "source": "yardi name x PS CRM",
            })
    if _role_name_rows:
        print(f"PS CRM email match: {_n_matched} of {len(_role_name_rows)} "
              f"named managers matched to an email")

    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"pm_roster_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["system", "property_id", "property_name",
                                          "property_code", "field", "value", "source"])
        w.writeheader()
        w.writerows(rows)
    print(f"\nSaved {len(rows)} rows -> {path}")
    print("Note: RealPage exposes no property/staff operations on our current "
          "contract (getunitlist and site-info ops need authorization).")


if __name__ == "__main__":
    main()
