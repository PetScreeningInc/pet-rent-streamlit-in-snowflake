"""Tests for the batch reliability helpers: Yardi window math and
transient-error detection."""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from batch_pdf import (
    yardi_window_open,
    seconds_until_yardi_window,
    id_needs_yardi,
    is_transient_error,
)


def test_window_open_weekday_business_hours():
    assert yardi_window_open(datetime(2026, 7, 6, 10, 0))   # Monday 10am
    assert yardi_window_open(datetime(2026, 7, 10, 17, 59)) # Friday 5:59pm


def test_window_closed_evenings_and_weekends():
    assert not yardi_window_open(datetime(2026, 7, 6, 8, 59))   # Mon before 9
    assert not yardi_window_open(datetime(2026, 7, 6, 18, 0))   # Mon 6pm
    assert not yardi_window_open(datetime(2026, 7, 4, 12, 0))   # Saturday
    assert not yardi_window_open(datetime(2026, 7, 5, 12, 0))   # Sunday


def test_seconds_until_window():
    # Saturday noon → Monday 9:00 = 45 hours
    assert seconds_until_yardi_window(datetime(2026, 7, 4, 12, 0)) == 45 * 3600
    # Monday 8:00 → 9:00 = 1 hour
    assert seconds_until_yardi_window(datetime(2026, 7, 6, 8, 0)) == 3600
    # Friday 19:00 → Monday 9:00
    assert seconds_until_yardi_window(datetime(2026, 7, 10, 19, 0)) == (24 + 24 + 14) * 3600
    # Open now → 0
    assert seconds_until_yardi_window(datetime(2026, 7, 6, 10, 0)) == 0


def test_id_needs_yardi():
    assert id_needs_yardi("yardi")
    assert id_needs_yardi("all")
    assert not id_needs_yardi("entrata")
    assert not id_needs_yardi("real_page")


def test_transient_error_detection():
    assert is_transient_error("Error: Timeout after 120s")
    assert is_transient_error("SOAP Fault: service temporarily unavailable")
    assert is_transient_error("HTTP 503")
    assert not is_transient_error("ID not found in PROD.COMMON.D_PROPERTIES")
    assert not is_transient_error("No pet-related charge codes found")
    assert not is_transient_error("")
