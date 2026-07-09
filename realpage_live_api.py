"""
realpage_live_api.py
====================

Live RealPage OneSite SOAP fetcher for the pet-rent Streamlit app.

This module mirrors the output contract of
``fetch_realpage_for_properties()`` in ``app.py`` — same tuple shape, same
lowercase keys — but bypasses Snowflake staging tables entirely. It calls
the RealPage OneSite SOAP API directly for each (pmc_id, site_id) using
two operations:

  - ``getscheduledcharges``    -> current scheduled charges snapshot
  - ``getresidentlistinfo``    -> residents (Current / Applicant / Former)

After fetching, the join logic mirrors the SQL CTEs from
``fetch_realpage_for_properties`` (latest_charges -> all_residents ->
direct_resident vs. lease_resident_pick fallback) so downstream code
receives the same row shape.

Trade-offs:
  - 2 SOAP calls per property (getscheduledcharges + getresidentlist).
    ThreadPoolExecutor pool (max 8) keeps wall time reasonable.
  - Returns ONLY the current snapshot — `postmonth` is ignored by the API
    and Former-resident charges are unreachable, so rows are emitted for
    the CURRENT month (plus one future month for future-scheduled pet
    rent). No history means no before/after lift for RealPage until
    RealPage authorizes `getresidentledger` for our license key
    (see docs/realpage-accuracy-audit.md).
"""

from __future__ import annotations

import os
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import requests
from dotenv import load_dotenv

load_dotenv()

# ─── Constants ──────────────────────────────────────────────────────────
SOAP_ENDPOINT = (
    "https://gateway.rpx.realpage.com/RPXGateway/partner/Pet_Screening/Pet_Screening.svc"
)
SOAP_NS = "http://tempuri.org/"
SOAP_ACTION_CHARGES = "http://tempuri.org/IRPXService/getscheduledcharges"
SOAP_ACTION_RESIDENTS = "http://tempuri.org/IRPXService/getresidentlistinfo"
SOAP_ACTION_RESIDENT_LIST = "http://tempuri.org/IRPXService/getresidentlist"

# Latest resident list per property from the most recent fetch, keyed by
# str(property_id). Lets the missing/suspected report builders filter
# PetScreening profiles against the LIVE resident roster instead of the
# (potentially stale) Snowflake staging resident table. Populated by
# fetch_realpage_live; empty when data came from cache.
LAST_RESIDENTS: dict = {}

# Lease status code -> human label (matches what the Snowflake staging
# table normalizes to via the legacy ELT pipeline).
LEASE_STATUS_LABEL = {
    "C": "Current",
    "P": "Applicant",
    "F": "Former",
}

# Status codes used for the resident list pulls.
RESIDENT_STATUSES = ("C", "P", "F")

# Concurrency knobs.
MAX_WORKERS = 8
MAX_PER_PROP_WORKERS = 4  # 1 charges + 3 resident-status fetches per property
HTTP_TIMEOUT_SEC = 60
RETRY_COUNT = 3
RETRY_BASE_SLEEP = 1.5

# Test/model resident filter — same set as app.py's _is_test().
TEST_WORDS = {
    "model", "test", "sample", "unknown", "noname", "tba", "vacant", "*", "",
}

# Default junk-email set. ``fetch_realpage_live`` accepts an optional
# parameter so the caller can pass app.py's JUNK_EMAILS verbatim.
DEFAULT_JUNK_EMAILS = {
    "none@none.com", "noemail@noemail.com", "none@nowhere.com",
    "none@gmail.com", "noemail@gmail.com", "na@na.com", "no@email.com",
    "na@gmail.com", "noemail@greystar.com", "no@no.com", "n/a@gmail.com",
    "none@aol.com", "none@embreydc.com", "noemail@comcapp.com",
}


# ─── Helpers ────────────────────────────────────────────────────────────
def _credentials() -> Tuple[str, str, str]:
    from snowflake_auth import get_app_secret
    user = get_app_secret("ONESITE_USERNAME", "").strip()
    pwd = get_app_secret("ONESITE_PASSWORD", "").strip()
    lic = get_app_secret("ONESITE_LICENSE_KEY", "").strip()
    if not (user and pwd and lic):
        raise RuntimeError(
            "Missing OneSite credentials. Set ONESITE_USERNAME, "
            "ONESITE_PASSWORD and ONESITE_LICENSE_KEY as Snowflake secrets "
            "on the Streamlit app (or in the environment for local dev)."
        )
    return user, pwd, lic


def _xml_escape(value: Any) -> str:
    """Cheap XML escape for SOAP request bodies."""
    s = "" if value is None else str(value)
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
         .replace('"', "&quot;")
         .replace("'", "&apos;")
    )


def _post_with_retry(
    body: str, soap_action: str, *, prop_label: str = ""
) -> Optional[str]:
    """POST a SOAP body with exponential-backoff retry.

    Returns the response text on success, ``None`` on terminal failure.
    Raises ``RuntimeError`` for SOAP faults so the caller can record them.
    """
    headers = {
        "Content-Type": "text/xml; charset=utf-8",
        "SOAPAction": soap_action,
    }
    last_exc: Optional[Exception] = None
    for attempt in range(1, RETRY_COUNT + 1):
        try:
            resp = requests.post(
                SOAP_ENDPOINT,
                data=body.encode("utf-8"),
                headers=headers,
                timeout=HTTP_TIMEOUT_SEC,
            )
            # OneSite often returns 500 for SOAP faults — let the parser
            # decide. Treat 4xx/5xx with no XML body as retryable.
            text = resp.text or ""
            if resp.status_code >= 500 and "Fault" not in text:
                raise RuntimeError(
                    f"HTTP {resp.status_code} from RealPage ({prop_label})"
                )
            return text
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt >= RETRY_COUNT:
                break
            time.sleep(RETRY_BASE_SLEEP * (2 ** (attempt - 1)))
    if last_exc:
        raise last_exc
    return None


def _strip_ns(tag: str) -> str:
    """Return the local-name of an XML tag, dropping any namespace prefix."""
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def _element_to_dict(elem: ET.Element) -> Dict[str, Any]:
    """Recursively convert an XML element into a flat-ish dict.

    Direct child tags become keys. If a child has its own children, the value
    is a nested dict. Repeated child tags collapse into a list.
    """
    out: Dict[str, Any] = {}
    for child in list(elem):
        key = _strip_ns(child.tag)
        if list(child):
            value: Any = _element_to_dict(child)
        else:
            value = (child.text or "").strip()
        if key in out:
            existing = out[key]
            if isinstance(existing, list):
                existing.append(value)
            else:
                out[key] = [existing, value]
        else:
            out[key] = value
    return out


def _find_all_local(root: ET.Element, local_name: str) -> List[ET.Element]:
    """Find all descendants whose local-name equals ``local_name``."""
    matches: List[ET.Element] = []
    for elem in root.iter():
        if _strip_ns(elem.tag) == local_name:
            matches.append(elem)
    return matches


def _check_soap_fault(root: ET.Element) -> Optional[str]:
    """Return a fault string if the response is a SOAP fault, else None."""
    for elem in root.iter():
        local = _strip_ns(elem.tag)
        if local == "Fault":
            # Try to grab a useful message from <faultstring> or <Reason>.
            for child in elem.iter():
                if _strip_ns(child.tag) in ("faultstring", "Text", "Reason"):
                    text = (child.text or "").strip()
                    if text:
                        return text
            return "SOAP Fault (no message)"
    return None


# ─── SOAP request bodies ────────────────────────────────────────────────
def _build_charges_body(
    pmc_id: str, site_id: str, post_month: str,
    username: str, password: str, license_key: str,
) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
        'xmlns:tns="http://tempuri.org/">'
          "<soapenv:Header/>"
          "<soapenv:Body>"
            "<tns:getscheduledcharges>"
              "<tns:auth>"
                f"<tns:pmcid>{_xml_escape(pmc_id)}</tns:pmcid>"
                f"<tns:siteid>{_xml_escape(site_id)}</tns:siteid>"
                f"<tns:username>{_xml_escape(username)}</tns:username>"
                f"<tns:password>{_xml_escape(password)}</tns:password>"
                f"<tns:licensekey>{_xml_escape(license_key)}</tns:licensekey>"
                "<tns:system>onesite</tns:system>"
              "</tns:auth>"
              "<tns:getscheduledcharge>"
                "<tns:residentstatus>C</tns:residentstatus>"
                f"<tns:postmonth>{_xml_escape(post_month)}</tns:postmonth>"
                "<tns:residentheldthestatus>3650</tns:residentheldthestatus>"
                "<tns:residentwillhavestatus>365</tns:residentwillhavestatus>"
              "</tns:getscheduledcharge>"
            "</tns:getscheduledcharges>"
          "</soapenv:Body>"
        "</soapenv:Envelope>"
    )


def _build_resident_list_body(
    pmc_id: str, site_id: str,
    username: str, password: str, license_key: str,
) -> str:
    """getresidentlist: ONE call returns ALL residents (Current + Former +
    Applicants) with balance fields — confirmed authorized for our license
    key on 2026-07-05 (see docs/realpage-accuracy-audit.md). Replaces the
    3x getresidentlistinfo calls per property."""
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
        'xmlns:tns="http://tempuri.org/">'
          "<soapenv:Header/>"
          "<soapenv:Body>"
            "<tns:getresidentlist>"
              "<tns:auth>"
                f"<tns:pmcid>{_xml_escape(pmc_id)}</tns:pmcid>"
                f"<tns:siteid>{_xml_escape(site_id)}</tns:siteid>"
                f"<tns:username>{_xml_escape(username)}</tns:username>"
                f"<tns:password>{_xml_escape(password)}</tns:password>"
                f"<tns:licensekey>{_xml_escape(license_key)}</tns:licensekey>"
                "<tns:system>onesite</tns:system>"
              "</tns:auth>"
            "</tns:getresidentlist>"
          "</soapenv:Body>"
        "</soapenv:Envelope>"
    )


def _build_residents_body(
    pmc_id: str, site_id: str, status: str,
    username: str, password: str, license_key: str,
) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
        'xmlns:tem="http://tempuri.org/">'
          "<soapenv:Header/>"
          "<soapenv:Body>"
            "<tem:getresidentlistinfo>"
              "<tem:auth>"
                f"<tem:pmcid>{_xml_escape(pmc_id)}</tem:pmcid>"
                f"<tem:siteid>{_xml_escape(site_id)}</tem:siteid>"
                f"<tem:username>{_xml_escape(username)}</tem:username>"
                f"<tem:password>{_xml_escape(password)}</tem:password>"
                f"<tem:licensekey>{_xml_escape(license_key)}</tem:licensekey>"
                "<tem:system>onesite</tem:system>"
              "</tem:auth>"
              "<tem:getresidentlistinfo>"
                "<tem:ExtensionData/>"
                "<tem:headofhousehold>false</tem:headofhousehold>"
                f"<tem:status>{_xml_escape(status)}</tem:status>"
                "<tem:clickFilterOptions>"
                  "<tem:NameValuePair>"
                    "<tem:Name>IncludeMiscAndNonResidents</tem:Name>"
                    "<tem:Value>False</tem:Value>"
                  "</tem:NameValuePair>"
                "</tem:clickFilterOptions>"
              "</tem:getresidentlistinfo>"
            "</tem:getresidentlistinfo>"
          "</soapenv:Body>"
        "</soapenv:Envelope>"
    )


# ─── Response parsers ───────────────────────────────────────────────────
def _parse_charges_response(xml_text: str, post_month: str) -> List[Dict[str, Any]]:
    """Parse a getscheduledcharges response into a list of charge dicts.

    Returns rows with these normalized keys:
        resh_id, unit_id, lease_id, category_code, transaction_name,
        description, amount, bill_start, bill_end, sub_journal, post_month.
    """
    if not xml_text:
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    fault = _check_soap_fault(root)
    if fault:
        raise RuntimeError(f"SOAP Fault: {fault}")

    rows: List[Dict[str, Any]] = []
    for cd in _find_all_local(root, "ChargeData"):
        d = _element_to_dict(cd)
        # ``d`` keys are the raw XML tag names (CamelCase). Map them to
        # our canonical lowercase schema.
        rows.append({
            "resh_id":           d.get("ReshID", "") or "",
            "unit_id":           d.get("UnitID", "") or "",
            "lease_id":          d.get("LeaID", "") or d.get("LeaseID", "") or "",
            "category_code":     d.get("CatCode", "") or "",
            "transaction_name":  d.get("TranName", "") or "",
            "description":       d.get("TransDesc", "") or "",
            "amount":            d.get("TransAmt", "") or d.get("Amount", "") or "",
            "bill_start":        d.get("BillStart", "") or "",
            "bill_end":          d.get("BillEnd", "") or "",
            "sub_journal":       d.get("SubJournal", "") or d.get("Grouptorent", "") or "",
            "post_month":        d.get("PostMonth", "") or post_month,
        })
    return rows


def _parse_residents_response(xml_text: str, status_code: str) -> List[Dict[str, Any]]:
    """Parse a getresidentlistinfo response into a list of resident dicts.

    Returns rows with these normalized keys:
        resident_member_id, resident_household_id, lease_id, unit_id,
        unit_number, first_name, last_name, email, lease_status,
        move_out_date, hoh_bit, signer_bit, active_bit, contact_status.
    """
    if not xml_text:
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    fault = _check_soap_fault(root)
    if fault:
        raise RuntimeError(f"SOAP Fault: {fault}")

    # Try namespaced first, then plain.
    elems: List[ET.Element] = []
    for elem in root.iter():
        if elem.tag == f"{{{SOAP_NS}}}resident" or _strip_ns(elem.tag) == "resident":
            elems.append(elem)

    out: List[Dict[str, Any]] = []
    label = LEASE_STATUS_LABEL.get(status_code, status_code)
    for e in elems:
        d = _element_to_dict(e)
        # XML tags here are lowercase per the OneSite contract.
        out.append({
            "resident_member_id":    d.get("residentmemberid", "") or "",
            "resident_household_id": d.get("residenthouseholdid", "") or "",
            "lease_id":              d.get("leaseid", "") or "",
            "unit_id":               d.get("unitid", "") or "",
            "unit_number":           d.get("unitnumber", "") or "",
            "first_name":            d.get("firstname", "") or "",
            "last_name":             d.get("lastname", "") or "",
            "email":                 d.get("email", "") or "",
            # Prefer the explicit status code we asked for; the payload
            # may not echo it consistently. Falls back to whatever the
            # API returns in ``leasestatus``.
            "lease_status":          d.get("leasestatus", "") or label,
            "move_out_date":         d.get("moveoutdate", "") or "",
            "hoh_bit":               (d.get("hohbit", "") or "").upper(),
            "signer_bit":            (d.get("signerbit", "") or "").upper(),
            "active_bit":            (d.get("activebit", "") or "").upper(),
            "contact_status":        d.get("contactstatus", "") or "",
        })
    # If lease_status came back as a single-letter code, normalize to label.
    for r in out:
        ls = (r["lease_status"] or "").strip()
        if ls in LEASE_STATUS_LABEL:
            r["lease_status"] = LEASE_STATUS_LABEL[ls]
    return out


# ─── Per-property fetch ─────────────────────────────────────────────────
def _fetch_property(
    prop: Dict[str, Any], post_month: str,
    username: str, password: str, license_key: str,
) -> Dict[str, Any]:
    """Fetch charges + residents for one property.

    Returns a dict with keys: pmc_id, site_id, charges, residents, error.
    """
    pmc_id = str(prop.get("PMC_ID") or prop.get("pmc_id") or "").strip()
    site_id = str(prop.get("SITE_ID") or prop.get("site_id") or "").strip()
    prop_label = (
        prop.get("PROPERTY_NAME") or prop.get("property_name") or site_id or pmc_id
    )

    result: Dict[str, Any] = {
        "pmc_id": pmc_id,
        "site_id": site_id,
        "charges": [],
        "residents": [],
        "error": None,
    }

    if not pmc_id or not site_id:
        result["error"] = "Missing pmc_id or site_id"
        return result

    # Build the workload: 1 charges fetch + 1 resident-list fetch
    # (getresidentlist returns Current + Former + Applicants in one call).
    def _fetch_charges() -> Tuple[str, Any]:
        body = _build_charges_body(
            pmc_id, site_id, post_month, username, password, license_key
        )
        text = _post_with_retry(
            body, SOAP_ACTION_CHARGES, prop_label=str(prop_label)
        )
        return ("charges", _parse_charges_response(text or "", post_month))

    def _fetch_residents_all() -> Tuple[str, Any]:
        body = _build_resident_list_body(
            pmc_id, site_id, username, password, license_key
        )
        text = _post_with_retry(
            body, SOAP_ACTION_RESIDENT_LIST, prop_label=str(prop_label)
        )
        # leasestatus comes back as full text ("Current", "Former", ...)
        return ("residents_all", _parse_residents_response(text or "", ""))

    errors: List[str] = []
    with ThreadPoolExecutor(max_workers=2) as ex:
        futs = [ex.submit(_fetch_charges), ex.submit(_fetch_residents_all)]
        for fut in as_completed(futs):
            try:
                tag, rows = fut.result()
                if tag == "charges":
                    result["charges"] = rows
                else:
                    result["residents"].extend(rows)
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc))

    if errors and not result["charges"] and not result["residents"]:
        result["error"] = "; ".join(errors)[:500]
    elif errors:
        # Partial failure — keep what we have but surface the error too.
        result["error"] = "Partial: " + "; ".join(errors)[:400]

    return result


# ─── Join + emit (mirrors the SQL CTEs in fetch_realpage_for_properties) ─
def _parse_date(value: str) -> Optional[date]:
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None
    # Try a few common shapes.
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%m/%d/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s[: len(fmt) + 5], fmt).date()
        except ValueError:
            continue
    # Last resort: take the first 10 chars and parse YYYY-MM-DD.
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _is_test_name(first_name: str, last_name: str) -> bool:
    f = (first_name or "").lower().strip()
    l = (last_name or "").lower().strip()
    if f in TEST_WORDS or l in TEST_WORDS:
        return True
    if "model" in f or "model" in l:
        return True
    return False


# Lease-status mapping to the unified tenant_status vocabulary.
TENANT_STATUS_MAP = {
    "Current":                  "Current",
    "Former":                   "Past",
    "Future Lease":             "Future",
    "Applicant":                "Applicant",
    "Applicant - Lease Signed": "Applicant",
    "Former Applicant":         "Past",
    "Pending":                  "Future",
}
DROP_LEASE_STATUSES = {
    "Future Lease", "Applicant", "Applicant - Lease Signed", "Pending",
}


def _pick_lease_resident(residents: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Pick a representative resident from a list using HOH > signer > active
    > contact_status priority (mirrors the SQL ROW_NUMBER ordering).
    """
    def _key(r: Dict[str, Any]) -> Tuple[int, int, int, int, str, str]:
        return (
            0 if r.get("hoh_bit") == "TRUE" else 1,
            0 if r.get("signer_bit") == "TRUE" else 1,
            0 if r.get("active_bit") == "TRUE" else 1,
            0 if r.get("contact_status") == "Lease signer" else 1,
            (r.get("last_name") or ""),
            (r.get("first_name") or ""),
        )

    return sorted(residents, key=_key)[0]


def _join_and_emit(
    prop: Dict[str, Any],
    fetched: Dict[str, Any],
    post_month_date: date,
    today: date,
    junk_emails: set,
    lookback_start: Optional[date] = None,
) -> Tuple[List[Dict[str, Any]], int, int, int]:
    """Replicate the SQL join logic and return:
        (rows, resh_match_count, lease_match_count, no_match_count).
    """
    pmc_id = fetched["pmc_id"]
    site_id = fetched["site_id"]

    # Property-level constants stamped onto every row.
    parent_company = (
        prop.get("PARENT_COMPANY_NAME") or prop.get("parent_company_name") or ""
    )
    property_id = int(prop.get("PROPERTY_ID") or prop.get("property_id") or 0)
    property_name = (
        prop.get("PROPERTY_NAME") or prop.get("property_name") or ""
    )
    property_code = (
        prop.get("PROPERTY_CODE") or prop.get("property_code") or site_id or ""
    )
    launch_raw = (
        prop.get("PROPERTY_LAUNCH_DATE") or prop.get("property_launch_date")
    )
    launch_date: Optional[date] = None
    if launch_raw:
        if hasattr(launch_raw, "strftime"):
            try:
                launch_date = launch_raw if isinstance(launch_raw, date) else launch_raw.date()
            except Exception:  # noqa: BLE001
                launch_date = _parse_date(str(launch_raw))
        else:
            launch_date = _parse_date(str(launch_raw))

    # ── latest_charges: filter to category 'C' (skip P=Payment) ──
    charges = [c for c in fetched["charges"] if (c.get("category_code") or "") == "C"]

    # ── all_residents: dedupe per (lease_id, resident_member_id), keep last ──
    residents = fetched["residents"]
    dedup: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for r in residents:
        key = (r.get("lease_id") or "", r.get("resident_member_id") or "")
        # Last write wins — close enough to "latest ingested_at" without a
        # timestamp on the live payload.
        dedup[key] = r
    residents = list(dedup.values())

    # ── direct_resident map: (lease_id, resident_member_id) -> resident ──
    # RealPage ReshID/resident_member_id values are not globally unique within a
    # site.  Joining only on resident_member_id can misattribute a charge to a
    # resident from an unrelated lease/unit, which then makes the missing-pet-rent
    # report miss the actual paying lease.  Require the lease to line up; if it
    # does not, fall back to the lease-level representative below.
    direct_by_lease_resh: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for r in residents:
        lid = (r.get("lease_id") or "").strip()
        rid = (r.get("resident_member_id") or "").strip()
        if lid and rid:
            direct_by_lease_resh[(lid, rid)] = r

    # ── lease_resident_pick: pick one resident per lease_id ──
    by_lease: Dict[str, List[Dict[str, Any]]] = {}
    for r in residents:
        lid = (r.get("lease_id") or "").strip()
        if lid:
            by_lease.setdefault(lid, []).append(r)
    lease_pick: Dict[str, Dict[str, Any]] = {
        lid: _pick_lease_resident(grp) for lid, grp in by_lease.items()
    }

    # ── Join charges to residents (direct first, fallback to lease) ──
    rows: List[Dict[str, Any]] = []
    resh_matches = 0
    lease_matches = 0
    no_matches = 0

    # Determine the earliest month to emit rows for.  The API always
    # returns the *current* charge snapshot (postmonth is ignored), but
    # each charge carries BillStart/BillEnd which tells us how far back
    # the charge existed.  We expand each charge into one row per month
    # from max(bill_start, lookback_start) to min(bill_end, move_out, today)
    # so the downstream spreading logic sees realistic monthly history.
    if lookback_start is None:
        lookback_start = date(2025, 1, 1)

    for c in charges:
        resh_id = (c.get("resh_id") or "").strip()
        lease_id = (c.get("lease_id") or "").strip()
        direct = direct_by_lease_resh.get((lease_id, resh_id)) if resh_id and lease_id else None
        lease_r = lease_pick.get(lease_id) if lease_id else None

        if direct is not None:
            match_type = "resh_id"
        elif lease_r is not None:
            match_type = "lease_id"
        else:
            match_type = "no_match"

        def _coalesce(field: str, _d=direct, _l=lease_r) -> Any:
            if _d is not None and _d.get(field):
                return _d.get(field)
            if _l is not None and _l.get(field):
                return _l.get(field)
            return ""

        first_name = _coalesce("first_name") or ""
        last_name = _coalesce("last_name") or ""
        email = (_coalesce("email") or "").strip()
        lease_status = _coalesce("lease_status") or ""
        move_out_raw = _coalesce("move_out_date") or ""
        unit_number = _coalesce("unit_number") or c.get("unit_id") or ""

        # ── Filters mirroring the WHERE clause ──
        if not email:
            no_matches += (1 if match_type == "no_match" else 0)
            continue
        if email.lower() in junk_emails:
            continue
        if lease_status in DROP_LEASE_STATUSES:
            continue
        if _is_test_name(first_name, last_name):
            continue

        # Parse date boundaries for this charge.
        bill_start = _parse_date(c.get("bill_start") or "")
        bill_end = _parse_date(c.get("bill_end") or "")
        move_out_date = _parse_date(move_out_raw)

        # Drop charges where bill window is entirely after move-out.
        if (
            move_out_date is not None and bill_start is not None
            and bill_start > move_out_date
        ):
            continue

        # ── Determine the monthly range to emit rows for ──
        # Start: first of max(bill_start, lookback_start)
        if bill_start is not None:
            emit_start = max(bill_start.replace(day=1), lookback_start)
        else:
            emit_start = lookback_start

        # End: normally clip to today for current/past scheduled charges.
        # If the API returns a future scheduled pet-rent charge (e.g. a July
        # PETRENT setup returned in June), emit one future-month row so the
        # missing-rent report treats the tenant/lease as covered while revenue
        # charts still only count it if the user selects that future month.
        if bill_start is not None and bill_start > today:
            future_month = bill_start.replace(day=1)
            if future_month.month == 12:
                next_future_month = future_month.replace(year=future_month.year + 1, month=1)
            else:
                next_future_month = future_month.replace(month=future_month.month + 1)
            end_candidates = [next_future_month - timedelta(days=1)]
        else:
            end_candidates = [today]
        if bill_end is not None:
            end_candidates.append(bill_end)
        if move_out_date is not None:
            end_candidates.append(move_out_date)
        emit_end = min(end_candidates)

        # Skip if the charge ended before our lookback window.
        if emit_end < lookback_start:
            continue

        tenant_status = TENANT_STATUS_MAP.get(lease_status, lease_status or "")

        # Generate one row per month the charge was active.
        month_cursor = emit_start.replace(day=1)
        emitted_any = False
        while month_cursor <= emit_end:
            # charge_from = first of this month
            charge_from = month_cursor
            # charge_to = last day of month, clipped to emit_end
            if month_cursor.month == 12:
                next_month = month_cursor.replace(year=month_cursor.year + 1, month=1)
            else:
                next_month = month_cursor.replace(month=month_cursor.month + 1)
            last_day = next_month - timedelta(days=1)
            charge_to = min(last_day, emit_end)

            rows.append({
                "parent_company":   parent_company,
                "property_id":      property_id,
                "property_name":    property_name,
                "property_code":    property_code,
                "launch_date":      launch_date,
                "unit_code":        str(unit_number),
                "unit_type":        "",
                "market_rent":      "",
                "lease_id":         str(lease_id),
                "tenant_code":      str(lease_id),
                "first_name":       str(first_name),
                "last_name":        str(last_name),
                "tenant_status":    tenant_status,
                "lease_from":       "",
                "lease_to":         "",
                "move_in":          "",
                "move_out":         (
                    move_out_date.strftime("%Y-%m-%d") if move_out_date else ""
                ),
                "email":            email,
                "charge_code":      c.get("transaction_name") or "",
                "charge_type":      c.get("description") or "",
                "charge_amount":    str(c.get("amount") or ""),
                "charge_from_date": charge_from.strftime("%Y-%m-%d"),
                "charge_to_date":   charge_to.strftime("%Y-%m-%d"),
            })
            emitted_any = True
            month_cursor = next_month

        if emitted_any:
            if match_type == "resh_id":
                resh_matches += 1
            elif match_type == "lease_id":
                lease_matches += 1
            else:
                no_matches += 1

    return rows, resh_matches, lease_matches, no_matches


# ─── Public entry point ─────────────────────────────────────────────────
def fetch_realpage_live(
    properties: List[Dict[str, Any]],
    progress_bar: Any,
    status_text: Any,
    lookback_months: int = 24,
    junk_emails: Optional[set] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Live RealPage SOAP fetcher.

    Returns ``(all_charges, results_log)`` in the SAME shape as
    ``fetch_realpage_for_properties`` in app.py. The ``lookback_months``
    arg is accepted for interface parity but does NOT extend history —
    the live API only returns the current scheduled-charge snapshot.
    """
    if not properties:
        return [], []

    junk = junk_emails or DEFAULT_JUNK_EMAILS

    try:
        username, password, license_key = _credentials()
    except RuntimeError as exc:
        # Surface a hard failure as one log row per property so the user
        # sees a clear actionable error instead of a silent empty result.
        try:
            progress_bar.progress(1.0)
            status_text.text(f"RealPage live API: {exc}")
        except Exception:  # noqa: BLE001
            pass
        log = [
            {
                "property": p.get("PROPERTY_NAME") or p.get("property_name", ""),
                "code":     p.get("PROPERTY_CODE") or p.get("property_code", ""),
                "status":   f"Error: {exc}",
                "charges":  0,
            }
            for p in properties
        ]
        return [], log

    today = date.today()
    post_month_date = today.replace(day=1)
    post_month_str = post_month_date.strftime("%Y-%m")

    # CURRENT-STATE ONLY: emit rows for the current month (plus one future
    # month for future-scheduled pet rent). The API cannot see charges that
    # already ended — `postmonth` is ignored and Former residents are
    # unreachable (docs/realpage-accuracy-audit.md) — so expanding
    # BillStart→BillEnd fabricates an understated history that then reads
    # as fake "lift". Until RealPage authorizes `getresidentledger`,
    # RealPage supports current revenue + missing/suspected only.
    lookback_start = today.replace(day=1)

    # Fresh fetch → reset the shared live-resident roster for this run.
    LAST_RESIDENTS.clear()

    total = len(properties)
    try:
        progress_bar.progress(0.02)
        status_text.text(
            f"Live RealPage API: fetching {total} property"
            f"{'ies' if total != 1 else 'y'} (current-state snapshot)..."
        )
    except Exception:  # noqa: BLE001
        pass

    all_charges: List[Dict[str, Any]] = []
    results_log: List[Dict[str, Any]] = []

    # Map property -> index so we can report progress in submission order
    # (avoids confusing "X of Y" jumps when futures complete out-of-order).
    completed = 0
    workers = min(MAX_WORKERS, max(1, total))

    # Submit all fetches in parallel.
    futures = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for prop in properties:
            fut = ex.submit(
                _fetch_property, prop, post_month_str,
                username, password, license_key,
            )
            futures[fut] = prop

        for fut in as_completed(futures):
            prop = futures[fut]
            prop_name = (
                prop.get("PROPERTY_NAME") or prop.get("property_name") or ""
            )
            prop_code = (
                prop.get("PROPERTY_CODE") or prop.get("property_code") or ""
            )
            completed += 1
            try:
                progress_bar.progress(min(0.05 + 0.9 * completed / total, 0.95))
                status_text.text(
                    f"Live RealPage API: {completed}/{total} — {prop_name}"
                )
            except Exception:  # noqa: BLE001
                pass

            try:
                fetched = fut.result()
            except Exception as exc:  # noqa: BLE001
                results_log.append({
                    "property": prop_name,
                    "code":     prop_code,
                    "status":   f"Error: {exc}",
                    "charges":  0,
                })
                continue

            if fetched.get("error") and not fetched.get("charges"):
                results_log.append({
                    "property": prop_name,
                    "code":     prop_code,
                    "status":   f"Error: {fetched['error']}",
                    "charges":  0,
                })
                continue

            rows, resh_m, lease_m, no_m = _join_and_emit(
                prop, fetched, post_month_date, today, junk,
                lookback_start=lookback_start,
            )
            all_charges.extend(rows)

            # Expose the live resident roster (incl. Former) so report
            # builders can filter PS profiles against reality, not staging.
            _pid_key = str(prop.get("PROPERTY_ID") or prop.get("property_id") or "")
            if _pid_key:
                LAST_RESIDENTS[_pid_key] = fetched.get("residents") or []

            n = len(rows)
            if n == 0:
                results_log.append({
                    "property": prop_name,
                    "code":     prop_code,
                    "status":   "Warning: No charges returned by live API",
                    "charges":  0,
                })
            else:
                status_str = (
                    f"Success ({n} charges; "
                    f"{resh_m} resh-match / {lease_m} lease-match"
                )
                if no_m:
                    status_str += f" / {no_m} unmatched"
                status_str += ")"
                if no_m and no_m > n * 0.05:
                    status_str = status_str.replace("Success", "Warning")
                if fetched.get("error"):
                    status_str = (
                        status_str + f"; partial: {fetched['error'][:120]}"
                    )
                results_log.append({
                    "property": prop_name,
                    "code":     prop_code,
                    "status":   status_str,
                    "charges":  n,
                })

    try:
        progress_bar.progress(1.0)
        status_text.text(
            f"Live RealPage API done — {len(all_charges):,} charge rows."
        )
    except Exception:  # noqa: BLE001
        pass

    return all_charges, results_log
