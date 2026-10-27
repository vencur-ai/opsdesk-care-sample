"""Deterministic shift-time utilities shared by rule packs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from opsdesk.core.models import Assignment, ShiftType


@dataclass(frozen=True)
class ShiftTimes:
    """Clock-time definition of one shift type."""

    start: time
    end: time
    break_minutes: int


SHIFT_TIMES: dict[ShiftType, ShiftTimes] = {
    ShiftType.EARLY: ShiftTimes(time(6, 0), time(14, 0), 30),
    ShiftType.LATE: ShiftTimes(time(14, 0), time(22, 0), 30),
    ShiftType.NIGHT: ShiftTimes(time(22, 0), time(6, 0), 30),
    ShiftType.LONG_DAY: ShiftTimes(time(7, 0), time(19, 30), 45),
}


def shift_interval(assignment: Assignment) -> tuple[datetime, datetime]:
    """Return naive local datetimes for an assignment's shift interval."""
    definition = SHIFT_TIMES[assignment.shift_type]
    starts_at = datetime.combine(assignment.date, definition.start)
    ends_at = datetime.combine(assignment.date, definition.end)
    if definition.end <= definition.start:
        ends_at += timedelta(days=1)
    return starts_at, ends_at


def shift_total_minutes(assignment: Assignment) -> int:
    """Return elapsed shift length before deducting its break."""
    starts_at, ends_at = shift_interval(assignment)
    return int((ends_at - starts_at).total_seconds() // 60)


def shift_break_minutes(assignment: Assignment) -> int:
    """Return the assignment's explicit break or its shift-type default."""
    if assignment.break_minutes is not None:
        return assignment.break_minutes
    return SHIFT_TIMES[assignment.shift_type].break_minutes


def shift_work_minutes(assignment: Assignment) -> int:
    """Return scheduled working minutes after deducting the assigned break."""
    total = shift_total_minutes(assignment)
    break_minutes = shift_break_minutes(assignment)
    if break_minutes > total:
        raise ValueError(f"break exceeds total shift duration for {assignment.assignment_id}")
    return total - break_minutes


def horizon_end_exclusive(start_date: date, horizon_days: int) -> date:
    """Return the exclusive end date of an episode."""
    return start_date + timedelta(days=horizon_days)