"""
Parallel charge-fetching for batch_pdf.py.

The Streamlit app fetches charges sequentially (one property at a time) so the
progress bar updates smoothly. For unattended batch runs, that's the bottleneck:
HQ-PAC's 108 properties = 2h22m sequential. With 8 workers, ~20-25 minutes.

These wrappers call the SAME per-property internal functions from app.py, just
in a ThreadPoolExecutor. They produce identical output (same dedupe/cleanup
happens downstream). No data is dropped or filtered differently.

Used only by batch_pdf.py — Streamlit UI is untouched.
"""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Tuple


def _fetch_one_entrata_property(prop: Dict, api_key: str, app_imports: Dict) -> Dict[str, Any]:
    """
    Fetch + extract charges for a single Entrata property.
    Returns a dict matching what fetch_entrata_for_properties produces per-property.
    """
    _fetch_leases   = app_imports["_fetch_entrata_leases_for_property"]
    _extract        = app_imports["_extract_charges_from_entrata_lease"]
    _extract_ar     = app_imports["_extract_ar_charges_from_entrata_lease"]

    prop_name = prop.get("PROPERTY_NAME", "")
    prop_code = prop.get("ENTRATA_PROPERTY_ID") or prop.get("PROPERTY_CODE", "")

    leases, error_msg = _fetch_leases(prop, api_key)

    charges: List[Dict] = []
    ar_charges: List[Dict] = []
    raw_lease_arrays: List[Dict] = []

    if leases:
        for lease in leases:
            charges.extend(_extract(lease, prop))
            ar_charges.extend(_extract_ar(lease, prop))

            # Capture raw arrays (mirrors app.py logic exactly so downstream stays
            # identical — including export tabs)
            lease_id = str(lease.get("id", ""))
            unit = lease.get("unitNumberSpace", "")
            _custs = lease.get("customers", {}).get("customer", [])
            if isinstance(_custs, dict):
                _custs = [_custs]
            _primary = _custs[0] if _custs else {}
            _tenant_name = f"{_primary.get('firstName', '')} {_primary.get('lastName', '')}".strip()
            _tenant_status = _primary.get("leaseCustomerStatus", "")

            _sched_raw = lease.get("scheduledCharges", {}).get("scheduledCharge", [])
            if isinstance(_sched_raw, dict):
                _sched_raw = [_sched_raw]
            _ar_raw = lease.get("arTransactions", {}).get("arTransaction", [])
            if isinstance(_ar_raw, dict):
                _ar_raw = [_ar_raw]

            raw_lease_arrays.append({
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
        status = f"Success ({len(leases)} leases)"
        if error_msg:
            status += f" (partial: {error_msg})"
        log_entry = {"property": prop_name, "code": prop_code, "status": status, "charges": len(charges)}
    elif error_msg and not leases:
        log_entry = {"property": prop_name, "code": prop_code, "status": f"Error: {error_msg}", "charges": 0}
    else:
        log_entry = {"property": prop_name, "code": prop_code, "status": "Warning: No charges found", "charges": 0}

    return {
        "charges": charges,
        "ar_charges": ar_charges,
        "raw_lease_arrays": raw_lease_arrays,
        "log_entry": log_entry,
        "n_leases": len(leases) if leases else 0,
    }


def fetch_entrata_parallel(
    properties: List[Dict],
    api_key: str,
    app_imports: Dict,
    workers: int = 8,
    verbose: bool = True,
) -> Tuple[List[Dict], List[Dict], List[Dict], List[Dict]]:
    """
    Parallel version of fetch_entrata_for_properties.

    Returns: (all_charges, results_log, all_ar_charges, all_raw_lease_arrays)
    Same shape as the sequential version — the caller doesn't need to change.
    """
    all_charges: List[Dict] = []
    all_ar_charges: List[Dict] = []
    all_raw_lease_arrays: List[Dict] = []
    results_log: List[Dict] = []

    n = len(properties)
    completed = 0
    t_start = time.time()

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {
            ex.submit(_fetch_one_entrata_property, prop, api_key, app_imports): prop
            for prop in properties
        }
        for fut in as_completed(futures):
            prop = futures[fut]
            try:
                out = fut.result()
            except Exception as e:
                results_log.append({
                    "property": prop.get("PROPERTY_NAME", ""),
                    "code": prop.get("ENTRATA_PROPERTY_ID") or prop.get("PROPERTY_CODE", ""),
                    "status": f"Error: {str(e)[:80]}",
                    "charges": 0,
                })
                completed += 1
                if verbose:
                    print(f"      [{completed}/{n}] {prop.get('PROPERTY_NAME','')} → exception: {e}")
                continue

            all_charges.extend(out["charges"])
            all_ar_charges.extend(out["ar_charges"])
            all_raw_lease_arrays.extend(out["raw_lease_arrays"])
            results_log.append(out["log_entry"])
            completed += 1

            if verbose and (completed % 10 == 0 or completed == n):
                elapsed = time.time() - t_start
                rate = completed / elapsed if elapsed > 0 else 0
                eta = (n - completed) / rate if rate > 0 else 0
                print(f"      [{completed}/{n}] {elapsed:.0f}s elapsed, "
                      f"~{eta:.0f}s remaining ({rate:.1f} props/sec)")

    return all_charges, results_log, all_ar_charges, all_raw_lease_arrays


# ─── Yardi (SOAP) parallel fetcher ──────────────────────────────────────
def _fetch_one_yardi_property(prop: Dict, app_imports: Dict, license_token: str,
                              soap_headers: Dict, move_date: str,
                              charge_from: str, charge_to: str) -> Dict[str, Any]:
    """Fetch + extract charges for a single Yardi property via SOAP."""
    import xml.etree.ElementTree as ET
    import requests

    build_soap_payload          = app_imports["build_soap_payload"]
    xml_to_dict                 = app_imports["xml_to_dict"]
    extract_property            = app_imports["extract_property"]
    extract_charges_from_prop   = app_imports["extract_charges_from_property"]

    prop_name = prop["PROPERTY_NAME"]
    prop_code = prop["PROPERTY_CODE"]

    import time as _time

    # 1 try + 2 retries with linear backoff on transient failures
    # (timeouts, HTTP 5xx, connection resets). Non-transient errors and
    # empty responses return immediately.
    max_attempts = 3
    for _attempt in range(1, max_attempts + 1):
        try:
            payload = build_soap_payload(prop, license_token, move_date, charge_from, charge_to)
            resp = requests.post(prop["RESIDENT_DATA_URL"], data=payload,
                                 headers=soap_headers, timeout=120)
            if resp.status_code != 200:
                if resp.status_code >= 500 and _attempt < max_attempts:
                    _time.sleep(3 * _attempt)
                    continue
                return {
                    "charges": [],
                    "log_entry": {"property": prop_name, "code": prop_code,
                                  "status": f"Error: HTTP {resp.status_code}", "charges": 0},
                }
            parsed = xml_to_dict(resp.text)
            extracted = extract_property(parsed)
            if not extracted:
                error_msg = "No data"
                for m in ET.fromstring(resp.text).iter():
                    if "Message" in m.tag and m.text:
                        error_msg = m.text.strip()[:60]
                        break
                return {
                    "charges": [],
                    "log_entry": {"property": prop_name, "code": prop_code,
                                  "status": f"Warning: {error_msg}", "charges": 0},
                }
            charges = extract_charges_from_prop(extracted, prop)
            return {
                "charges": charges,
                "log_entry": {"property": prop_name, "code": prop_code,
                              "status": "Success", "charges": len(charges)},
            }
        except requests.exceptions.Timeout:
            if _attempt < max_attempts:
                _time.sleep(3 * _attempt)
                continue
            return {
                "charges": [],
                "log_entry": {"property": prop_name, "code": prop_code,
                              "status": "Error: Timeout (after retries)", "charges": 0},
            }
        except Exception as exc:
            _msg = str(exc).lower()
            if _attempt < max_attempts and any(
                t in _msg for t in ("connection", "reset", "temporarily", "unavailable")
            ):
                _time.sleep(3 * _attempt)
                continue
            return {
                "charges": [],
                "log_entry": {"property": prop_name, "code": prop_code,
                              "status": f"Error: {str(exc)[:50]}", "charges": 0},
            }


def fetch_yardi_parallel(
    properties: List[Dict],
    app_imports: Dict,
    license_token: str,
    soap_headers: Dict,
    workers: int = 8,
    verbose: bool = True,
) -> Tuple[List[Dict], List[Dict]]:
    """Parallel version of fetch_rentroll_for_properties.

    Returns: (all_charges, results_log)
    """
    from datetime import datetime, timedelta, timezone

    today = datetime.now(timezone.utc).date()
    move_date   = (today - timedelta(days=10 * 365)).strftime("%Y-%m-%d")
    charge_from = "2000-01-01"
    charge_to   = today.strftime("%Y-%m-%d")

    all_charges: List[Dict] = []
    results_log: List[Dict] = []

    n = len(properties)
    completed = 0
    t_start = time.time()

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {
            ex.submit(_fetch_one_yardi_property, prop, app_imports,
                      license_token, soap_headers, move_date,
                      charge_from, charge_to): prop
            for prop in properties
        }
        for fut in as_completed(futures):
            prop = futures[fut]
            try:
                out = fut.result()
            except Exception as e:
                results_log.append({
                    "property": prop.get("PROPERTY_NAME", ""),
                    "code": prop.get("PROPERTY_CODE", ""),
                    "status": f"Error: {str(e)[:80]}",
                    "charges": 0,
                })
                completed += 1
                continue

            all_charges.extend(out["charges"])
            results_log.append(out["log_entry"])
            completed += 1

            if verbose and (completed % 10 == 0 or completed == n):
                elapsed = time.time() - t_start
                rate = completed / elapsed if elapsed > 0 else 0
                eta = (n - completed) / rate if rate > 0 else 0
                print(f"      [{completed}/{n}] {elapsed:.0f}s elapsed, "
                      f"~{eta:.0f}s remaining ({rate:.1f} props/sec)")

    return all_charges, results_log
