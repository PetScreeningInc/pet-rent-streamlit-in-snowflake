import ast
from datetime import datetime
from pathlib import Path

APP_SOURCE = Path("app.py").read_text()


class _PandasStub:
    @staticmethod
    def isna(value):
        return value is None


def _load_app_function(name):
    module = ast.parse(APP_SOURCE)
    for node in module.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            namespace = {"datetime": datetime, "pd": _PandasStub}
            exec(compile(ast.Module(body=[node], type_ignores=[]), "app.py", "exec"), namespace)
            return namespace[name]
    raise AssertionError(f"{name} not found in app.py")


def test_compute_launch_analysis_uses_observed_pre_months_not_missing_calendar_zeroes():
    """Sparse history should not dilute one real pre-PS bar into a fake 6-month average."""
    compute = _load_app_function("compute_launch_analysis")
    months = [
        datetime(2026, 1, 1),
        datetime(2026, 2, 1),
        datetime(2026, 3, 1),
        datetime(2026, 4, 1),
        datetime(2026, 5, 1),
        datetime(2026, 6, 1),  # only observed pre-PS month
        datetime(2026, 7, 1),  # launch/current month
    ]
    monthly_by_prop = {"Joinery": {months[5]: 50, months[6]: 50}}
    launch_dates = {"Joinery": datetime(2026, 7, 1)}

    analysis = compute(monthly_by_prop, months, launch_dates)["Joinery"]

    assert analysis["pre_avg"] == 50
    assert analysis["n_pre"] == 1
    assert analysis["baseline_month_label"] == "1 observed pre month"


def test_low_data_pre_baseline_still_counts_as_comparable_when_meaningful():
    """A property with 1-2 observed pre months should feed the aggregate, matching its chart badge."""
    is_comparable = _load_app_function("_launch_analysis_is_comparable")

    assert is_comparable({"n_pre": 2, "baseline_reliable": False, "baseline_meaningful": True}) is True
    assert is_comparable({"n_pre": 1, "baseline_reliable": False, "baseline_meaningful": True}) is True
    assert is_comparable({"n_pre": 2, "baseline_reliable": False, "baseline_meaningful": False}) is False
    assert is_comparable({"n_pre": 0, "baseline_reliable": True, "baseline_meaningful": True}) is False


def test_fee_collection_aggregate_uses_same_comparable_rule_as_individual_lift_badges():
    """Top Fee Collection metrics should not drop low-data properties that show lift in charts."""
    fc_start = APP_SOURCE.index("# KPI ROW 1: Launch impact metrics")
    fc_end = APP_SOURCE.index("# ═══════════════════════════════════════════════════════════\n                # KPI ROW 2", fc_start)
    fc_source = APP_SOURCE[fc_start:fc_end]

    comparable_start = fc_source.index("comparable = {p: a for p, a in launch_analysis.items()")
    comparable_end = fc_source.index("n_no_pre = len(launch_analysis) - len(comparable)", comparable_start)
    comparable_source = fc_source[comparable_start:comparable_end]

    assert "_launch_analysis_is_comparable(a)" in comparable_source
    assert 'a.get("baseline_reliable", True)' not in comparable_source


def test_property_monthly_lift_for_display_uses_latest_month_minus_pre_baseline():
    """Property chart lift should match default summary methodology: latest month - pre baseline."""
    calc = _load_app_function("_property_monthly_lift_for_display")
    months = [
        datetime(2024, 1, 1),
        datetime(2024, 2, 1),
        datetime(2024, 3, 1),
        datetime(2024, 4, 1),
    ]
    prop_data = {
        months[0]: 100,
        months[1]: 100,
        months[2]: 200,
        months[3]: 50,
    }
    analysis = {
        "pre_avg": 100,
        "diff_monthly": 25,  # old chart subtitle showed this, which is wrong for default Monthly Lift
    }

    assert calc(prop_data, months, analysis) == -50


def test_property_monthly_lift_is_zero_when_only_real_pre_bar_matches_latest_month():
    calc = _load_app_function("_property_monthly_lift_for_display")
    months = [datetime(2026, 6, 1), datetime(2026, 7, 1)]
    prop_data = {months[0]: 50, months[1]: 50}

    assert calc(prop_data, months, {"pre_avg": 50}) == 0


def test_individual_chart_subtitle_uses_property_display_lift_helper_even_with_sparse_baseline():
    helper_idx = APP_SOURCE.index("_property_monthly_lift_for_display")
    chart_idx = APP_SOURCE.index("def build_individual_property_charts")
    chart_source = APP_SOURCE[chart_idx: APP_SOURCE.index("def generate_html_report", chart_idx)]

    assert helper_idx < chart_idx
    assert "_property_monthly_lift_for_display(" in chart_source
    assert 'a["diff_monthly"]' not in chart_source
    assert 'a.get("baseline_meaningful", True)' in chart_source
    assert 'and a.get("baseline_reliable", True)' not in chart_source
    assert 'baseline_month_label' in chart_source


def test_summary_downloads_only_include_enhanced_pdf_and_html():
    """The Summary section should not offer the old duplicate Original PDF download."""
    summary_start = APP_SOURCE.index("# ─── TAB 2: Summary")
    summary_end = APP_SOURCE.index("# ─── TAB 3: Missing Pet Rent Report", summary_start)
    summary_source = APP_SOURCE[summary_start:summary_end]

    assert 'label="Enhanced PDF"' in summary_source
    assert 'key="dl_exec_pdf"' in summary_source
    assert 'label="Download HTML"' in summary_source
    assert 'key="dl_exec_html"' in summary_source
    assert "Original PDF" not in summary_source
    assert "dl_orig_pdf" not in summary_source


def test_generated_report_download_buttons_are_primary_when_generated():
    """Generated report downloads should use the same highlighted/ready style."""
    for label, key in (
        ("Enhanced PDF", "dl_exec_pdf"),
        ("Download HTML", "dl_exec_html"),
    ):
        key_pos = APP_SOURCE.index(f'key="{key}"')
        button_start = APP_SOURCE.rfind("st.download_button(", 0, key_pos)
        button_block = APP_SOURCE[button_start:key_pos]
        assert f'label="{label}"' in button_block, label
        assert 'type="primary"' in button_block, label


def test_documentation_explains_current_baseline_and_monthly_lift_methodology():
    docs_idx = APP_SOURCE.index("**Launch Date Handling — Charts & Impact Calculation**")
    docs_source = APP_SOURCE[docs_idx:APP_SOURCE.index("# ══════════════════════════════════════════════════════════", docs_idx)]

    assert "observed pre-launch months" in docs_source
    assert "Latest/current-month lift" in docs_source
    assert "missing months are not averaged as $0" in docs_source
    assert "Post-launch current lift" not in docs_source


def test_summary_tab_uses_at_a_glance_cards_instead_of_old_three_section_layout():
    summary_start = APP_SOURCE.index("# ─── TAB 2: Summary")
    summary_end = APP_SOURCE.index("# ═══════════════════════════════════════════════════════════════\n                #  PROPERTY MANAGERS & EMAIL", summary_start)
    summary_source = APP_SOURCE[summary_start:summary_end]

    assert "At a Glance" in summary_source
    assert "_summary_glance_card" in summary_source
    assert "_render_summary_card_row" in summary_source
    assert "Comparable Units" in summary_source
    assert "Pre-PS Revenue" in summary_source
    assert "Current Revenue" in summary_source
    assert "Lift per Unit" in summary_source
    assert "Est. Asset Value Impact" in summary_source
    assert "Pet Revenue Found" in summary_source
    assert "Found per Unit" in summary_source
    assert "Est. Value Impact (Found)" in summary_source
    assert "Tenants Not Paying" in summary_source
    assert "Suspected Undisclosed" in summary_source
    assert "Average {_adopt_type_label} Adoption" in summary_source
    assert "n_props_with_data" in summary_source
    assert "_total_units" in summary_source
    assert "properties with charge data" in summary_source
    assert "units represented" in summary_source

    at_a_glance_source = summary_source[summary_source.index("#  SUMMARY AT A GLANCE"):]
    assert "SECTION 1: VALUE CREATED" not in at_a_glance_source
    assert "SECTION 2: REVENUE OPPORTUNITY" not in at_a_glance_source
    assert "SECTION 3: PORTFOLIO HEALTH" not in at_a_glance_source
