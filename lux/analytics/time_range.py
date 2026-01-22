"""Time range utilities for analytics."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import calendar


@dataclass(frozen=True)
class TimeRangeResult:
    start: datetime
    end: datetime
    compare_start: datetime | None
    compare_end: datetime | None


def _to_utc_start(day: date) -> datetime:
    return datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)


def _to_utc_end(day: date) -> datetime:
    return datetime.combine(day, datetime.max.time(), tzinfo=timezone.utc)


def _quarter_bounds(year: int, quarter: int) -> tuple[date, date]:
    if quarter not in {1, 2, 3, 4}:
        raise ValueError("quarter must be 1-4")
    start_month = (quarter - 1) * 3 + 1
    end_month = start_month + 2
    start_day = date(year, start_month, 1)
    end_day = date(year, end_month, calendar.monthrange(year, end_month)[1])
    return start_day, end_day


def _resolve_primary(preset: str, today: date) -> tuple[date, date]:
    preset = preset.lower()
    if preset == "last_week":
        end_day = today
        start_day = today - timedelta(days=6)
    elif preset == "this_week":
        weekday = today.weekday()
        start_day = today - timedelta(days=weekday)
        end_day = start_day + timedelta(days=6)
    elif preset == "last_month":
        end_day = today
        start_day = today - timedelta(days=29)
    elif preset == "this_month":
        start_day = date(today.year, today.month, 1)
        end_day = date(today.year, today.month, calendar.monthrange(today.year, today.month)[1])
    elif preset == "last_quarter":
        end_day = today
        start_day = today - timedelta(days=89)
    elif preset == "this_quarter":
        quarter = (today.month - 1) // 3 + 1
        start_day, end_day = _quarter_bounds(today.year, quarter)
    elif preset in {"last_year", "year"}:
        end_day = today
        start_day = today - timedelta(days=364)
    elif preset == "this_year":
        start_day = date(today.year, 1, 1)
        end_day = today
    elif preset == "last_q1":
        year = today.year - 1
        start_day, end_day = _quarter_bounds(year, 1)
    elif preset == "last_q2":
        year = today.year - 1
        start_day, end_day = _quarter_bounds(year, 2)
    elif preset == "last_q3":
        year = today.year - 1
        start_day, end_day = _quarter_bounds(year, 3)
    elif preset == "last_q4":
        year = today.year - 1
        start_day, end_day = _quarter_bounds(year, 4)
    else:
        raise ValueError(f"Unknown time range preset: {preset}")
    return start_day, end_day


def _resolve_compare(
    preset: str,
    primary_start: date,
    primary_end: date,
    today: date,
) -> tuple[date, date] | None:
    preset = preset.lower()
    if preset == "previous_period":
        delta = (primary_end - primary_start) + timedelta(days=1)
        compare_end = primary_start - timedelta(days=1)
        compare_start = compare_end - delta + timedelta(days=1)
        return compare_start, compare_end
    if preset == "this_week":
        weekday = today.weekday()
        start = today - timedelta(days=weekday)
        end = start + timedelta(days=6)
        return start, end
    if preset == "this_month":
        start = date(today.year, today.month, 1)
        end = date(today.year, today.month, calendar.monthrange(today.year, today.month)[1])
        return start, end
    if preset == "this_quarter":
        quarter = (today.month - 1) // 3 + 1
        return _quarter_bounds(today.year, quarter)
    if preset == "this_year":
        return date(today.year, 1, 1), today
    if preset == "previous_year_period":
        try:
            year_delta = primary_start.replace(year=primary_start.year - 1)
            end_delta = primary_end.replace(year=primary_end.year - 1)
        except ValueError:
            year_delta = date(primary_start.year - 1, primary_start.month, primary_start.day - 1)
            end_delta = date(primary_end.year - 1, primary_end.month, primary_end.day - 1)
        return year_delta, end_delta
    if preset == "this_q1":
        return _quarter_bounds(today.year, 1)
    if preset == "this_q2":
        return _quarter_bounds(today.year, 2)
    if preset == "this_q3":
        return _quarter_bounds(today.year, 3)
    if preset == "this_q4":
        return _quarter_bounds(today.year, 4)
    if preset == "custom":
        return None
    raise ValueError(f"Unknown compare preset: {preset}")


def parse_time_range(
    preset: str,
    compare_preset: str | None = None,
    *,
    today: date | None = None,
    custom_start: date | None = None,
    custom_end: date | None = None,
    compare_start: date | None = None,
    compare_end: date | None = None,
) -> TimeRangeResult:
    """Return UTC time range for analytics queries."""
    today = today or date.today()
    if preset == "custom":
        if not (custom_start and custom_end):
            raise ValueError("custom_start and custom_end required for custom preset")
        primary_start, primary_end = custom_start, custom_end
    else:
        primary_start, primary_end = _resolve_primary(preset, today)

    compare_start_day = compare_end_day = None
    if compare_preset:
        if compare_preset == "custom":
            if compare_start and compare_end:
                compare_start_day, compare_end_day = compare_start, compare_end
        else:
            result = _resolve_compare(compare_preset, primary_start, primary_end, today)
            if result:
                compare_start_day, compare_end_day = result

    return TimeRangeResult(
        start=_to_utc_start(primary_start),
        end=_to_utc_end(primary_end),
        compare_start=_to_utc_start(compare_start_day) if compare_start_day else None,
        compare_end=_to_utc_end(compare_end_day) if compare_end_day else None,
    )
