"""Tests for fetch_realpage_hybrid's stitching rule: observed staging
snapshots win per property+month; live rows fill unobserved months.

Extracts the function from app.py via AST (no Streamlit import) and stubs
both underlying fetchers plus the realpage_live_api module.
"""
import ast
import sys
import types
from collections import defaultdict
from pathlib import Path

from app_source import APP_SOURCE  # modular layout: ordered source concatenation


class _Dummy:
    def progress(self, *a, **k): pass
    def text(self, *a, **k): pass


def _make_hybrid(stg_result, live_result):
    module = ast.parse(APP_SOURCE)
    nodes = [n for n in module.body
             if isinstance(n, ast.FunctionDef) and n.name == "fetch_realpage_hybrid"]
    assert nodes, "fetch_realpage_hybrid missing from app.py"

    live_stub = types.ModuleType("realpage_live_api")
    live_stub.fetch_realpage_live = lambda *a, **k: live_result
    sys.modules["realpage_live_api"] = live_stub

    namespace = {
        "defaultdict": defaultdict,
        "JUNK_EMAILS": "'none@none.com'",
        "fetch_realpage_for_properties": lambda *a, **k: stg_result,
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "app.py", "exec"), namespace)
    return namespace["fetch_realpage_hybrid"]


def _charge(pid, month, amount, src):
    return {
        "property_id": pid,
        "property_name": f"Prop{pid}",
        "charge_from_date": f"{month}-01",
        "charge_to_date": f"{month}-28",
        "charge_code": "PETRENT",
        "charge_amount": str(amount),
        "_src": src,
    }


def test_staging_months_win_and_live_fills_the_rest():
    stg = [
        _charge(1, "2026-04", 100, "stg"),
        _charge(1, "2026-05", 100, "stg"),
    ]
    live = [
        _charge(1, "2026-05", 150, "live"),  # staging observed May → dropped
        _charge(1, "2026-06", 150, "live"),  # staging lacks June → kept
        _charge(1, "2026-07", 150, "live"),
    ]
    hybrid = _make_hybrid(
        (stg, [{"property": "Prop1", "code": "c1", "status": "Success (2 charges)", "charges": 2}]),
        (live, [{"property": "Prop1", "code": "c1", "status": "Success (3 charges)", "charges": 3}]),
    )
    props = [{"PROPERTY_ID": 1, "PROPERTY_NAME": "Prop1"}]
    combined, log = hybrid(props, _Dummy(), _Dummy())

    by_month = {(r["charge_from_date"][:7], r["_src"]) for r in combined}
    assert ("2026-05", "stg") in by_month
    assert ("2026-05", "live") not in by_month
    assert ("2026-06", "live") in by_month
    assert len(combined) == 4

    assert len(log) == 1
    assert "2 staging history + 2 live fill" in log[0]["status"]


def test_property_with_no_staging_rows_is_fully_live():
    live = [_charge(2, "2026-06", 50, "live"), _charge(2, "2026-07", 50, "live")]
    hybrid = _make_hybrid(
        ([], [{"property": "Prop2", "code": "c2", "status": "Warning: No charges found in Snowflake", "charges": 0}]),
        (live, [{"property": "Prop2", "code": "c2", "status": "Success (2 charges)", "charges": 2}]),
    )
    props = [{"PROPERTY_ID": 2, "PROPERTY_NAME": "Prop2"}]
    combined, log = hybrid(props, _Dummy(), _Dummy())
    assert len(combined) == 2
    assert all(r["_src"] == "live" for r in combined)
    assert log[0]["charges"] == 2
    assert log[0]["status"].startswith("Success")


def test_property_dead_in_both_sources_gets_warning():
    hybrid = _make_hybrid(
        ([], [{"property": "Prop3", "code": "c3", "status": "Warning: No charges found in Snowflake", "charges": 0}]),
        ([], [{"property": "Prop3", "code": "c3", "status": "Error: SOAP Fault", "charges": 0}]),
    )
    props = [{"PROPERTY_ID": 3, "PROPERTY_NAME": "Prop3"}]
    combined, log = hybrid(props, _Dummy(), _Dummy())
    assert combined == []
    assert log[0]["charges"] == 0
    assert log[0]["status"].startswith("Warning")
