"""Snapshot-table auto-append + regression tests for the ESA-bucket revert.

The July 2026 ESA non-responsive bucket in Missing Pet Rent was reverted:
Missing Pet Rent is household+active only again, and assistance
non_responsive lives solely in the Suspected Undisclosed report
('Unresolved assistance request'). The snapshot tables keep their ESA
columns (historical rows use them); new runs write NULLs there.
"""
import os
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ─── Fake Snowflake connection that records executed SQL ────────────────

class FakeCursor:
    def __init__(self, log):
        self.log = log

    def execute(self, sql, params=None):
        self.log.append((" ".join(str(sql).split()), list(params or [])))

    def close(self):
        pass


class FakeConn:
    def __init__(self):
        self.executed = []

    def cursor(self):
        return FakeCursor(self.executed)


@pytest.fixture()
def snap(monkeypatch):
    import snapshot_tables
    monkeypatch.setattr(snapshot_tables, "_ensured", set())
    monkeypatch.setenv("PET_VALUE_SNAPSHOT_APPEND", "1")
    monkeypatch.setenv("PET_VALUE_SNAPSHOT_SCHEMA", "RAW.MISC")
    return snapshot_tables


# ─── snapshot_tables unit behavior ──────────────────────────────────────

def test_users_append_writes_both_report_types_with_bucket(snap):
    conn = FakeConn()
    missing = pd.DataFrame([
        {"PROPERTY_ID": "1", "PROPERTY_NAME": "P1", "USER_EMAIL": "A@x.com",
         "PET_ID": "p1", "pet_rent_paid": 0, "overall_status": "Profile_No_Rent",
         "profile_bucket": "household"},
        {"PROPERTY_ID": "1", "PROPERTY_NAME": "P1", "USER_EMAIL": "b@x.com",
         "PET_ID": "", "pet_rent_paid": 0,
         "overall_status": "ESA_NonResponsive_No_Rent",
         "profile_bucket": "esa_non_responsive"},
    ])
    suspected = pd.DataFrame([
        {"PROPERTY_ID": "1", "PROPERTY_NAME": "P1", "USER_EMAIL": "c@x.com",
         "SUSPECTED_REASON": "Unresolved assistance request",
         "pet_rent_paid": 0, "overall_status": "Suspected_Undisclosed"},
    ])
    n = snap.append_users_snapshot(
        missing_df=missing, suspected_df=suspected, pmc_system="yardi",
        run_label="Test Parent", parent_id="99", parent_name="Test Parent",
        connection_factory=lambda: conn)
    assert n == 3
    inserts = [sql for sql, _ in conn.executed if sql.startswith("INSERT")]
    assert len(inserts) == 1
    assert "RAW.MISC.PET_VALUE_USERS_SNAPSHOT" in inserts[0]
    _, params = [(s, p) for s, p in conn.executed if s.startswith("INSERT")][0]
    # 3 rows x len(USERS_COLUMNS) params, email lowercased, bucket at the end
    assert len(params) == 3 * len(snap.USERS_COLUMNS)
    assert "a@x.com" in params
    assert "esa_non_responsive" in params
    # ALTER for the new PROFILE_BUCKET column ran first
    assert any("ADD COLUMN IF NOT EXISTS PROFILE_BUCKET" in s for s, _ in conn.executed)


def test_append_disabled_by_env(snap, monkeypatch):
    monkeypatch.setenv("PET_VALUE_SNAPSHOT_APPEND", "0")
    conn = FakeConn()
    n = snap.append_users_snapshot(
        missing_df=pd.DataFrame([{"PROPERTY_ID": "1", "USER_EMAIL": "a@x.com"}]),
        pmc_system="yardi", run_label="x", connection_factory=lambda: conn)
    assert n == 0 and conn.executed == []


def test_append_failure_never_raises(snap):
    class BoomConn:
        def cursor(self):
            raise RuntimeError("no grants")
    n = snap.append_parent_snapshot(
        {"PMC": "yardi", "PARENT_ID": "1", "PARENT_NAME": "X"},
        connection_factory=lambda: BoomConn())
    assert n == 0


def test_numeric_nulls_preserved(snap):
    conn = FakeConn()
    snap.append_parent_snapshot(
        {"PMC": "yardi", "PARENT_ID": "1", "PARENT_NAME": "X",
         "MONTHLY_LIFT": "", "UNCOLLECTED_TENANTS": 5,
         "MISSING_ESA_TENANTS": 2},
        connection_factory=lambda: conn)
    sql, params = [(s, p) for s, p in conn.executed if s.startswith("INSERT")][0]
    idx_lift = snap.PARENT_COLUMNS.index("MONTHLY_LIFT")
    idx_unc = snap.PARENT_COLUMNS.index("UNCOLLECTED_TENANTS")
    idx_esa = snap.PARENT_COLUMNS.index("MISSING_ESA_TENANTS")
    assert params[idx_lift] is None       # '' -> NULL, never 0
    assert params[idx_unc] == 5.0
    assert params[idx_esa] == 2.0


def test_build_property_rows_from_report(snap):
    mr = {"P1": {"missing_count": 3, "avg_recurring": 30.0,
                 "esa_missing_count": 2, "missing_tenants": []}}
    su = {"P1": {"missing_count": 4, "avg_recurring": 30.0}}
    monthly = {"P1": {"2026-07": 900.0}, "P2": {"2026-07": 100.0}}
    rows = snap.build_property_rows_from_report(
        "yardi", mr, su, monthly, "2026-07", {"P1": "11", "P2": "22"},
        doors_by_name={"P1": 120}, parent_id="9", parent_name="Par")
    by_name = {r["PROPERTY_NAME"]: r for r in rows}
    assert by_name["P1"]["UNCOLLECTED_TENANTS"] == 3
    assert by_name["P1"]["MISSING_MONTHLY_RENT"] == 90.0
    assert by_name["P1"]["MISSING_ESA_TENANTS"] == 2
    assert by_name["P1"]["MISSING_ESA_MONTHLY"] == 60.0
    assert by_name["P1"]["SUSPECTED_UNDISCLOSED"] == 4
    assert by_name["P1"]["UNIT_COUNT"] == 120
    # P2 has charges only — appears with revenue, no flag metrics
    assert by_name["P2"]["CURRENT_REVENUE"] == 100.0
    assert by_name["P2"]["UNCOLLECTED_TENANTS"] is None


def test_unsafe_schema_rejected(snap, monkeypatch):
    monkeypatch.setenv("PET_VALUE_SNAPSHOT_SCHEMA", "RAW.MISC; DROP TABLE x")
    with pytest.raises(ValueError):
        snap._schema()


# ─── ESA-bucket revert regressions ──────────────────────────────────────

def test_dedup_missing_rows_by_email():
    os.environ.setdefault("PET_VALUE_CACHE_BACKEND", "local")
    from analytics.missing_pet_rent import _dedup_missing_rows
    df = pd.DataFrame([
        {"USER_EMAIL": "a@x.com", "TENANT_CODE": "t1"},
        {"USER_EMAIL": "a@x.com", "TENANT_CODE": "t2"},
        {"USER_EMAIL": "b@x.com", "TENANT_CODE": "t3"},
    ])
    out = _dedup_missing_rows(df, "P1", {})
    assert len(out) == 2
    assert set(out["USER_EMAIL"]) == {"a@x.com", "b@x.com"}


def test_reclaim_reason_groups_keep_historical_esa_bucket():
    # 'assistance_non_responsive' stays: backfilled July 2026 cohorts carry
    # it, even though new runs no longer write it.
    from reclaim_report import REASON_GROUPS
    assert "assistance_non_responsive" in REASON_GROUPS
    assert "missing_pet_rent" in REASON_GROUPS
    assert "Unresolved assistance request" in REASON_GROUPS


def test_missing_rent_is_household_only():
    """Both profile queries are back to compliant + household + active —
    no assistance branch, no profile_bucket column."""
    from app_source import APP_SOURCE as text
    assert "esa_non_responsive" not in text
    assert "profile_bucket" not in text
    assert "ESA_NonResponsive_No_Rent" not in text
    assert text.count("AND ue.user_pet_type = 'household'\n      AND ue.user_pet_status = 'active'") >= 2


def test_suspected_includes_assistance_non_responsive():
    """Suspected Undisclosed again covers assistance non_responsive in both
    the reason CASE and the WHERE filter of both suspected functions."""
    from app_source import APP_SOURCE as text
    assert text.count(
        "AND ue.user_pet_status IN ('draft','non_responsive','declined','not_recommended','returned')"
    ) >= 2
    assert text.count("'draft', 'non_responsive', 'declined',") >= 2
