"""The app source, concatenated across the modular layout.

The app was split from a single app.py into config/services/analytics/
components/reports modules. Source-level regression tests assert against
this ordered concatenation so "the app contains X" keeps meaning the same
thing it did in the monolith. Order matters for tests that assert relative
positions (helpers before chart builders before the HTML report).
"""
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

SOURCE_FILES = [
    "app.py",
    "config.py",
    "services/snowflake_io.py",
    "services/yardi.py",
    "services/entrata.py",
    "services/realpage.py",
    "services/appfolio.py",
    "analytics/launch_analysis.py",
    "analytics/missing_pet_rent.py",
    "analytics/suspected_undisclosed.py",
    "components/ui_helpers.py",
    "components/charts.py",
    "reports/html_report.py",
    "reports/pdf_report.py",
]

APP_SOURCE = "\n".join((_ROOT / f).read_text() for f in SOURCE_FILES)
