import sys
from pathlib import Path
from datetime import datetime

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from parent_property_value_export import (
    AGGREGATE_COLUMNS,
    PROPERTY_COLUMNS,
    build_aggregate_row,
    current_monthly_revenue,
    latest_observed_revenue_month,
    normalize_pmc_source,
    write_workbook,
)


def test_normalize_pmc_source_handles_supported_system_names():
    assert normalize_pmc_source("Yardi") == "yardi"
    assert normalize_pmc_source("Entrata") == "entrata"
    assert normalize_pmc_source("RealPage / OneSite") == "real_page"
    assert normalize_pmc_source("AppFolio") == "appfolio"
    assert normalize_pmc_source("unknown") == "unknown"


def test_parent_export_current_revenue_uses_latest_observed_month_not_empty_calendar_month():
    months = [datetime(2026, 4, 1), datetime(2026, 5, 1), datetime(2026, 6, 1)]
    monthly_by_prop = {"AR Building": {months[0]: 1200, months[1]: 1300}}

    assert latest_observed_revenue_month(monthly_by_prop, months, ["AR Building"]) == months[1]
    value, month = current_monthly_revenue(monthly_by_prop, months, ["AR Building"])
    assert value == 1300
    assert month == months[1]


def test_build_aggregate_row_sums_successful_property_metrics_only():
    rows = [
        {
            "status": "success",
            "parent_company_name": "Asset Living",
            "parent_company_id": "123",
            "pmc": "yardi",
            "total_units": 100,
            "comparable_count": 1,
            "pre_baseline_monthly": 500,
            "current_monthly_revenue": 700,
            "comparable_current_monthly_revenue": 650,
            "default_monthly_lift": 150,
            "average_monthly_lift": 125,
            "selected_monthly_lift": 150,
            "missing_pet_rent_tenants": 3,
            "missing_pet_rent_monthly": 90,
            "suspected_undisclosed_profiles": 2,
            "suspected_undisclosed_monthly": 60,
        },
        {
            "status": "success",
            "parent_company_name": "Asset Living",
            "parent_company_id": "123",
            "pmc": "entrata",
            "total_units": 50,
            "comparable_count": 0,
            "pre_baseline_monthly": 0,
            "current_monthly_revenue": 80,
            "comparable_current_monthly_revenue": 0,
            "default_monthly_lift": 0,
            "average_monthly_lift": 0,
            "selected_monthly_lift": 0,
            "missing_pet_rent_tenants": 1,
            "missing_pet_rent_monthly": 30,
            "suspected_undisclosed_profiles": 1,
            "suspected_undisclosed_monthly": 20,
        },
        {
            "status": "failed",
            "parent_company_name": "Asset Living",
            "parent_company_id": "123",
            "pmc": "yardi",
            "total_units": 999,
            "comparable_count": 1,
            "selected_monthly_lift": 999,
            "missing_pet_rent_monthly": 999,
        },
    ]

    aggregate = build_aggregate_row(rows, parent_id="123", parent_name="Asset Living")

    assert aggregate["parent_company_name"] == "Asset Living"
    assert aggregate["parent_company_id"] == "123"
    assert aggregate["properties_total"] == 3
    assert aggregate["properties_success"] == 2
    assert aggregate["properties_failed"] == 1
    assert aggregate["pmc_systems"] == "entrata; yardi"
    assert aggregate["total_units"] == 150
    assert aggregate["comparable_properties"] == 1
    assert aggregate["comparable_units"] == 100
    assert aggregate["pre_baseline_monthly"] == 500
    assert aggregate["current_monthly_revenue"] == 780
    assert aggregate["comparable_current_monthly_revenue"] == 650
    assert aggregate["default_monthly_lift"] == 150
    assert aggregate["average_monthly_lift"] == 125
    assert aggregate["selected_monthly_lift"] == 150
    assert aggregate["missing_pet_rent_tenants"] == 4
    assert aggregate["missing_pet_rent_monthly"] == 120
    assert aggregate["suspected_undisclosed_profiles"] == 3
    assert aggregate["suspected_undisclosed_monthly"] == 80
    assert aggregate["combined_delivered_plus_missing_monthly"] == 270
    assert aggregate["combined_delivered_plus_missing_annual"] == 3240
    assert aggregate["combined_delivered_plus_missing_asset_value_5_cap"] == 64800


def test_write_workbook_creates_property_and_aggregate_sheets(tmp_path):
    property_rows = [{col: "" for col in PROPERTY_COLUMNS}]
    property_rows[0].update(
        {
            "parent_company_name": "Asset Living",
            "parent_company_id": "123",
            "property_id": "999",
            "property_name": "Example Property",
            "status": "success",
            "pmc": "yardi",
            "total_units": 100,
            "comparable_count": 1,
            "selected_monthly_lift": 150,
        }
    )
    aggregate_row = {col: "" for col in AGGREGATE_COLUMNS}
    aggregate_row.update(
        {
            "parent_company_name": "Asset Living",
            "parent_company_id": "123",
            "properties_success": 1,
            "selected_monthly_lift": 150,
        }
    )

    output = tmp_path / "parent_export.xlsx"
    write_workbook(output, property_rows, aggregate_row)

    workbook = pd.read_excel(output, sheet_name=None)
    assert list(workbook.keys()) == ["properties", "aggregate"]
    assert workbook["properties"].loc[0, "property_name"] == "Example Property"
    assert workbook["aggregate"].loc[0, "parent_company_name"] == "Asset Living"
