import ast
from datetime import date, datetime
from pathlib import Path

from app_source import APP_SOURCE  # modular layout: ordered source concatenation


def _load_app_function(name):
    module = ast.parse(APP_SOURCE)
    namespace = {"date": date, "datetime": datetime}
    for node in module.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_format_appfolio_date":
            exec(compile(ast.Module(body=[node], type_ignores=[]), "app.py", "exec"), namespace)
            break
    for node in module.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            exec(compile(ast.Module(body=[node], type_ignores=[]), "app.py", "exec"), namespace)
            return namespace[name]
    raise AssertionError(f"{name} not found in app.py")


def test_appfolio_normalizer_maps_snowflake_row_to_unified_charge_contract():
    normalize = _load_app_function("_normalize_appfolio_snowflake_charge_row")
    row = {
        "PARENT_COMPANY": "Demo Parent",
        "PROPERTY_ID": 123,
        "PROPERTY_NAME": "Demo Property",
        "PROPERTY_CODE": "appfolio-prop-1",
        "LAUNCH_DATE": date(2025, 1, 15),
        "UNIT_CODE": "A-101",
        "UNIT_ID": "unit-1",
        "LEASE_ID": "occ-1",
        "TENANT_CODE": "tenant-1",
        "FIRST_NAME": "Ada",
        "LAST_NAME": "Lovelace",
        "TENANT_STATUS": "Current",
        "TENANT_TYPE": "Financially Responsible",
        "PRIMARY_TENANT": True,
        "LEASE_FROM": date(2024, 1, 1),
        "LEASE_TO": None,
        "MOVE_IN": date(2024, 1, 3),
        "MOVE_OUT": None,
        "EMAIL": "ada@example.com",
        "CHARGE_CODE": "Pet Rent",
        "CHARGE_TYPE": "gl-1",
        "CHARGE_AMOUNT": 35.0,
        "CHARGE_FROM_DATE": date(2024, 2, 1),
        "CHARGE_TO_DATE": None,
        "FREQUENCY": None,
        "RECURRING_CHARGE_ID": "rc-1",
        "DATABASE_ID": "db-1",
        "OCCUPANCY_ID": "occ-1",
        "APPFOLIO_UNIT_ID": "unit-1",
    }

    charge = normalize(row)

    assert charge["parent_company"] == "Demo Parent"
    assert charge["property_id"] == 123
    assert charge["property_code"] == "appfolio-prop-1"
    assert charge["unit_code"] == "A-101"
    assert charge["lease_id"] == "occ-1"
    assert charge["tenant_code"] == "tenant-1"
    assert charge["charge_code"] == "Pet Rent"
    assert charge["charge_type"] == "gl-1"
    assert charge["charge_amount"] == 35.0
    assert charge["charge_from_date"] == "2024-02-01"
    assert charge["charge_to_date"] == ""
    assert charge["frequency"] == "recurring"
    assert charge["_appfolio_charge_id"] == "rc-1"
    assert charge["_appfolio_occupancy_id"] == "occ-1"


def test_appfolio_normalizer_uses_occupancy_fallback_when_tenant_id_missing():
    normalize = _load_app_function("_normalize_appfolio_snowflake_charge_row")
    charge = normalize({
        "TENANT_CODE": "",
        "LEASE_ID": "occ-fallback",
        "OCCUPANCY_ID": "occ-fallback",
        "CHARGE_CODE": "Monthly Pet Rent",
        "FREQUENCY": "",
    })

    assert charge["tenant_code"] == "occ-fallback"
    assert charge["lease_id"] == "occ-fallback"
    assert charge["frequency"] == "recurring"


def test_appfolio_is_routed_as_snowflake_backed_provider_with_live_api_toggle_placeholder():
    assert '"AppFolio"' in APP_SOURCE
    assert '"appfolio"' in APP_SOURCE
    assert "load_appfolio_parent_companies" in APP_SOURCE
    assert "load_appfolio_all_properties" in APP_SOURCE
    assert "load_appfolio_properties_for_selection" in APP_SOURCE
    assert "fetch_appfolio_for_properties" in APP_SOURCE

    routing_source = APP_SOURCE[APP_SOURCE.index("def _load_parent_companies_for"):APP_SOURCE.index("# ─── Session state")]
    assert 'if pmc_system == "appfolio"' in routing_source
    assert "return load_appfolio_parent_companies()" in routing_source
    assert "return load_appfolio_all_properties()" in routing_source
    assert "return load_appfolio_properties_for_selection(**kwargs)" in routing_source
    assert "appfolio_use_live_api" in routing_source
    assert "AppFolio live API is not enabled yet" in routing_source
    assert "fetch_appfolio_for_properties(props, progress_bar, status_text, lookback_months)" in APP_SOURCE
    assert "t.tenant_unit_id" not in APP_SOURCE
    assert "LEFT JOIN latest_units u ON u.unit_id = t.unit_id" in APP_SOURCE


def test_appfolio_recurring_classification_matches_realpage_provider_override():
    assert '_grp_system in ("real_page", "appfolio")' in APP_SOURCE
    assert '_grp_sys in ("real_page", "appfolio")' in APP_SOURCE
    assert "AppFolio getrecurringcharges rows are recurring by endpoint semantics" in APP_SOURCE


def test_batch_scripts_include_realpage_and_appfolio_provider_paths():
    batch_pdf = Path("batch_pdf.py").read_text()
    batch_csv = Path("batch_csv.py").read_text()

    for source in (batch_pdf, batch_csv):
        assert '"real_page"' in source
        assert '"appfolio"' in source
        assert "load_realpage_properties_for_selection" in source
        assert "load_appfolio_properties_for_selection" in source
        assert "fetch_realpage_for_properties" in source
        assert "fetch_appfolio_for_properties" in source
