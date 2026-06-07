import ast
from datetime import datetime
from pathlib import Path

APP_SOURCE = Path("app.py").read_text()


def _load_app_function(name):
    module = ast.parse(APP_SOURCE)
    for node in module.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            namespace = {"datetime": datetime}
            exec(compile(ast.Module(body=[node], type_ignores=[]), "app.py", "exec"), namespace)
            return namespace[name]
    raise AssertionError(f"{name} not found in app.py")


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


def test_individual_chart_subtitle_uses_property_display_lift_helper():
    helper_idx = APP_SOURCE.index("_property_monthly_lift_for_display")
    chart_idx = APP_SOURCE.index("def build_individual_property_charts")
    chart_source = APP_SOURCE[chart_idx: APP_SOURCE.index("def generate_html_report", chart_idx)]

    assert helper_idx < chart_idx
    assert "_property_monthly_lift_for_display(" in chart_source
    assert 'a["diff_monthly"]' not in chart_source


def test_report_download_buttons_are_primary_when_generated():
    """All generated report downloads should use the same highlighted/ready style."""
    for label, key in (
        ("Enhanced PDF", "dl_exec_pdf"),
        ("Original PDF", "dl_orig_pdf"),
        ("Download HTML", "dl_exec_html"),
    ):
        key_pos = APP_SOURCE.index(f'key="{key}"')
        button_start = APP_SOURCE.rfind("st.download_button(", 0, key_pos)
        button_block = APP_SOURCE[button_start:key_pos]
        assert f'label="{label}"' in button_block, label
        assert 'type="primary"' in button_block, label
