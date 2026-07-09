"""Tests for the shared calculation helpers used by the missing/suspected
pet-rent reports: fee estimation, recent-charge filtering, and row dedup.

Uses the same AST-extraction pattern as test_app_ui_regressions.py so app.py
is never imported (no Streamlit side effects) — but with real pandas/numpy
because these helpers do real DataFrame work.
"""
import ast
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

APP_SOURCE = Path("app.py").read_text()


def _load_app_functions(*names):
    module = ast.parse(APP_SOURCE)
    wanted = set(names)
    nodes = [n for n in module.body if isinstance(n, ast.FunctionDef) and n.name in wanted]
    found = {n.name for n in nodes}
    missing = wanted - found
    if missing:
        raise AssertionError(f"Missing functions in app.py: {sorted(missing)}")
    namespace = {
        "datetime": datetime,
        "timedelta": timedelta,
        "pd": pd,
        "np": np,
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "app.py", "exec"), namespace)
    return {name: namespace[name] for name in names}


# ── _estimate_property_fees ──────────────────────────────────────────

def _fees(rows, code_class):
    fn = _load_app_functions("_estimate_property_fees")["_estimate_property_fees"]
    df = pd.DataFrame(rows)
    df["_amt"] = pd.to_numeric(df["charge_amount"], errors="coerce").fillna(0)
    return fn(df, code_class)


def test_fee_estimate_is_per_tenant_not_per_row():
    """A RealPage-style tenant with 18 monthly rows must not outweigh a
    tenant with a single row. Median across tenants, not mean across rows."""
    rows = []
    # Tenant A: 18 monthly snapshot rows at $30/mo
    for i in range(18):
        rows.append({"property_name": "P", "tenant_code": "A",
                     "charge_code": "PETRENT", "charge_amount": "30"})
    # Tenants B, C: one row each at $50/mo
    rows.append({"property_name": "P", "tenant_code": "B",
                 "charge_code": "PETRENT", "charge_amount": "50"})
    rows.append({"property_name": "P", "tenant_code": "C",
                 "charge_code": "PETRENT", "charge_amount": "50"})
    rec, ot = _fees(rows, {("P", "PETRENT"): "recurring"})
    # Row-mean would give (18*30 + 100) / 20 = $32; tenant median = $50
    assert rec["P"] == 50
    assert ot == {}


def test_fee_estimate_sums_concurrent_recurring_codes_per_tenant():
    rows = [
        {"property_name": "P", "tenant_code": "A", "charge_code": "PETRENT", "charge_amount": "35"},
        {"property_name": "P", "tenant_code": "A", "charge_code": "PETRENT2", "charge_amount": "15"},
    ]
    rec, _ = _fees(rows, {("P", "PETRENT"): "recurring", ("P", "PETRENT2"): "recurring"})
    assert rec["P"] == 50


def test_fee_estimate_separates_onetime_deposits_from_recurring():
    rows = [
        {"property_name": "P", "tenant_code": "A", "charge_code": "PETRENT", "charge_amount": "35"},
        {"property_name": "P", "tenant_code": "A", "charge_code": "PETDEP", "charge_amount": "300"},
        {"property_name": "P", "tenant_code": "B", "charge_code": "PETRENT", "charge_amount": "45"},
    ]
    rec, ot = _fees(rows, {("P", "PETRENT"): "recurring", ("P", "PETDEP"): "onetime"})
    # Recurring median of [35, 45] = 40 — the $300 deposit must not leak in
    assert rec["P"] == 40
    assert ot["P"] == 300


def test_fee_estimate_ignores_negative_and_zero_amounts():
    rows = [
        {"property_name": "P", "tenant_code": "A", "charge_code": "PETRENT", "charge_amount": "35"},
        {"property_name": "P", "tenant_code": "A", "charge_code": "CONCPET", "charge_amount": "-35"},
        {"property_name": "P", "tenant_code": "B", "charge_code": "PETRENT", "charge_amount": "0"},
    ]
    rec, ot = _fees(rows, {("P", "PETRENT"): "recurring", ("P", "CONCPET"): "recurring"})
    assert rec["P"] == 35


# ── _recent_paying_charges ───────────────────────────────────────────

def _recent(rows, days=90):
    fn = _load_app_functions("_recent_paying_charges")["_recent_paying_charges"]
    return fn(pd.DataFrame(rows), days=days)


def test_recent_filter_drops_previous_tenants_old_charges():
    old = (datetime.now() - timedelta(days=400)).strftime("%Y-%m-%d")
    new = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")
    rows = [
        {"tenant_code": "OLD", "tenant_status": "Past", "charge_to_date": old},
        {"tenant_code": "NEW", "tenant_status": "Current", "charge_to_date": new},
    ]
    out = _recent(rows)
    assert list(out["tenant_code"]) == ["NEW"]


def test_recent_filter_keeps_openended_current_but_not_openended_past():
    rows = [
        {"tenant_code": "CUR", "tenant_status": "Current", "charge_to_date": ""},
        {"tenant_code": "PAST", "tenant_status": "Past", "charge_to_date": ""},
    ]
    out = _recent(rows)
    assert list(out["tenant_code"]) == ["CUR"]


def test_recent_filter_keeps_future_end_dates():
    future = (datetime.now() + timedelta(days=200)).strftime("%Y-%m-%d")
    rows = [{"tenant_code": "A", "tenant_status": "Current", "charge_to_date": future}]
    assert len(_recent(rows)) == 1


# ── _dedup_missing_rows ──────────────────────────────────────────────

def test_dedup_prefers_row_with_lease_dates_and_keeps_one_per_email():
    fn = _load_app_functions("_dedup_missing_rows")["_dedup_missing_rows"]
    df = pd.DataFrame([
        {"USER_EMAIL": "a@x.com", "TENANT_CODE": "t-no-info"},
        {"USER_EMAIL": "a@x.com", "TENANT_CODE": "t-with-info"},
        {"USER_EMAIL": "b@x.com", "TENANT_CODE": "t2"},
    ])
    tenant_info = {("P", "t-with-info"): {"move_in": None}}
    out = fn(df, "P", tenant_info)
    assert len(out) == 2
    assert out[out["USER_EMAIL"] == "a@x.com"]["TENANT_CODE"].iloc[0] == "t-with-info"


# ─── Pet rent pricing (own-book distribution, replaced network benchmark) ───

def test_build_pet_rent_pricing_distribution():
    from benchmarks import build_pet_rent_pricing
    r = build_pet_rent_pricing({"A": 25, "B": 35, "C": 45, "D": 30, "E": 300}, 100)
    # $300 is outside the plausibility band and must be excluded
    assert r["n_props"] == 4
    assert r["min"] == 25 and r["max"] == 45
    assert r["min"] <= r["p25"] <= r["median"] <= r["p75"] <= r["max"]
    assert r["paying_count"] == 100


def test_build_pet_rent_pricing_single_property_and_empty():
    from benchmarks import build_pet_rent_pricing
    one = build_pet_rent_pricing({"A": 40})
    assert one["n_props"] == 1 and one["median"] == 40.0
    assert build_pet_rent_pricing({}) is None
    assert build_pet_rent_pricing({"A": 999}) is None  # implausible fee only


def test_assistance_ratio_benchmark_removed():
    """The assistance-animal ratio and network fee pool are gone from the
    report surfaces; only the own-book pricing summary remains."""
    import benchmarks
    assert not hasattr(benchmarks, "fetch_assistance_benchmark")
    assert not hasattr(benchmarks, "build_benchmark_comparison")
    from pathlib import Path
    app_src = Path(__file__).resolve().parent.parent / "app.py"
    text = app_src.read_text()
    assert "ASSISTANCE-ANIMAL SHARE" not in text
    assert "Assistance-animal share" not in text
    assert "build_pet_rent_pricing" in text
