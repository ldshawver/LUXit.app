from datetime import date, timezone

import pytest

from lux.analytics.time_range import parse_time_range


def test_last_week_range():
    result = parse_time_range("last_week", today=date(2024, 5, 8))
    assert result.start.date() == date(2024, 5, 2)
    assert result.end.date() == date(2024, 5, 8)


def test_last_quarter_range():
    result = parse_time_range("last_quarter", today=date(2024, 5, 8))
    assert result.start.date() == date(2024, 2, 9)
    assert result.end.date() == date(2024, 5, 8)


def test_last_q1_range():
    result = parse_time_range("last_q1", today=date(2024, 8, 20))
    assert result.start.date() == date(2023, 1, 1)
    assert result.end.date() == date(2023, 3, 31)


def test_compare_previous_period():
    result = parse_time_range("last_week", "previous_period", today=date(2024, 5, 8))
    assert result.compare_start.date() == date(2024, 4, 25)
    assert result.compare_end.date() == date(2024, 5, 1)


def test_dst_boundary_utc_safe():
    result = parse_time_range("last_week", today=date(2024, 3, 12))
    assert result.start.tzinfo is not None
    assert result.end.tzinfo is not None
    assert result.start.tzinfo == timezone.utc
