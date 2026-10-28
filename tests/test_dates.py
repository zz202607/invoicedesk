from __future__ import annotations

from app.core.dates import extract_date


def test_strict_date() -> None:
    assert extract_date("开票日期:2024年12月02日") == "2024-12-02"


def test_spaced_old_vat_date() -> None:
    assert extract_date("2024  12  04", allow_compact=True) == "2024-12-04"


def test_compact_date_only_when_allowed() -> None:
    assert extract_date("20241112") is None
    assert extract_date("20241112", allow_compact=True) == "2024-11-12"


def test_invalid_dates_are_rejected() -> None:
    assert extract_date("2024年13月40日") is None
    assert extract_date("not a date") is None


def test_calendar_dates_are_validated() -> None:
    assert extract_date("2024年02月31日") is None
    assert extract_date("2024年02月29日") == "2024-02-29"
    assert extract_date("2023年02月29日") is None


def test_compact_date_scans_past_invalid_candidate() -> None:
    assert extract_date("99999999 20241112", allow_compact=True) == "2024-11-12"
