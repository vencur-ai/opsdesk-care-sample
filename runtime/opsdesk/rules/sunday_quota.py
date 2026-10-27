"""Shared Sunday-rest quota helpers for the German house rule."""

from datetime import date, timedelta


def sundays_in_horizon(start_date: date, horizon_days: int) -> list[date]:
    """Return Sundays in the half-open episode horizon."""
    return [
        start_date + timedelta(days=offset)
        for offset in range(horizon_days)
        if (start_date + timedelta(days=offset)).weekday() == 6
    ]


def required_free_sundays(sunday_count: int) -> int:
    """Require two free Sundays per five, never making all Sundays unavailable."""
    if sunday_count < 0:
        raise ValueError("sunday_count must be non-negative")
    return min((2 * sunday_count) // 5, max(0, sunday_count - 1))
