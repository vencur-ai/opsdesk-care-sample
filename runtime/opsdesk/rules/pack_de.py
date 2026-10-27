"""German Working Time Act checks for the 30-day simulation horizon."""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import date, datetime, time, timedelta

from opsdesk.core.state import WorldState
from opsdesk.core.time_utils import shift_break_minutes, shift_work_minutes
from opsdesk.rules.base import (
    Rule,
    RulePack,
    Violation,
    employee_assignments,
    make_violation,
)
from opsdesk.rules.sunday_quota import required_free_sundays, sundays_in_horizon


class DEWorkingTimeRule(Rule):
    """Apply the eight-hour normal day and ten-hour extension limit."""

    id = "de.daily_hours"
    title = "Daily working-time limit and average"
    citation = "Arbeitszeitgesetz (ArbZG), section 3"
    text = (
        "Working time is limited to eight hours per working day and may be extended to ten hours "
        "if the statutory six-month or 24-week average remains at eight hours. This v0.1 check "
        "enforces the ten-hour daily ceiling and uses the current episode's working days as an "
        "approximation of the averaging period; legal compliance requires the full historical record."
    )
    needs_expert_review = True

    def check(self, state: WorldState) -> list[Violation]:
        violations: list[Violation] = []
        for employee in sorted(state.employees, key=lambda item: item.employee_id):
            daily_minutes: dict[date, int] = defaultdict(int)
            daily_assignments: dict[date, list[str]] = defaultdict(list)
            for assignment in employee_assignments(state, employee.employee_id):
                daily_minutes[assignment.date] += shift_work_minutes(assignment)
                daily_assignments[assignment.date].append(assignment.assignment_id)
            for work_date, minutes in sorted(daily_minutes.items()):
                if minutes > 10 * 60:
                    violations.append(
                        make_violation(
                            self,
                            f"{minutes / 60:g} working hours exceed the 10-hour daily ceiling.",
                            entity_ids=(employee.employee_id, *daily_assignments[work_date]),
                            occurred_on=work_date,
                        )
                    )
            total_minutes = sum(daily_minutes.values())
            workday_count = len(daily_minutes)
            if workday_count and total_minutes > 8 * 60 * workday_count:
                violations.append(
                    make_violation(
                        self,
                        "Average working time in the episode exceeds eight hours per working day.",
                        entity_ids=(employee.employee_id,),
                        occurred_on=min(daily_minutes),
                    )
                )
        return violations


class DERestBreakRule(Rule):
    """Apply statutory break durations according to shift working time."""

    id = "de.rest_break"
    title = "Breaks"
    citation = "Arbeitszeitgesetz (ArbZG), section 4"
    text = "At least 30 minutes of breaks are required for more than six working hours and 45 minutes for more than nine."
    needs_expert_review = True

    def check(self, state: WorldState) -> list[Violation]:
        violations: list[Violation] = []
        for assignment in sorted(state.roster.assignments, key=lambda item: item.assignment_id):
            worked = shift_work_minutes(assignment)
            required = 45 if worked > 9 * 60 else 30 if worked > 6 * 60 else 0
            provided = shift_break_minutes(assignment)
            if provided < required:
                violations.append(
                    make_violation(
                        self,
                        f"The shift provides {provided} minutes of break; {required} are required.",
                        entity_ids=(assignment.employee_id, assignment.assignment_id),
                        occurred_on=assignment.date,
                    )
                )
        return violations


class DERestPeriodRule(Rule):
    """Require 11 hours between shifts, with compensated care-sector reduction."""

    id = "de.daily_rest"
    title = "Daily rest period"
    citation = "Arbeitszeitgesetz (ArbZG), section 5(1) and section 5(2)"
    text = (
        "At least 11 hours of uninterrupted rest are required. In qualifying care facilities the "
        "period may be reduced to ten hours only if another rest period is extended to at least "
        "12 hours within the statutory compensation window. The exception is opt-in per world."
    )
    needs_expert_review = True

    def check(self, state: WorldState) -> list[Violation]:
        violations: list[Violation] = []
        for employee in sorted(state.employees, key=lambda item: item.employee_id):
            assignments = employee_assignments(state, employee.employee_id)
            rest_pairs: list[tuple[datetime, datetime, int, str, str, date | None]] = []
            for previous, following in zip(assignments, assignments[1:]):
                previous_end = _shift_end(previous)
                next_start = _shift_start(following)
                minutes = int((next_start - previous_end).total_seconds() // 60)
                rest_pairs.append(
                    (
                        previous_end,
                        next_start,
                        minutes,
                        previous.assignment_id,
                        following.assignment_id,
                        following.date,
                    )
                )
            for gap_start, gap_end, minutes, previous_id, following_id, following_date in rest_pairs:
                if minutes >= 11 * 60:
                    continue
                compensated = (
                    state.care_rest_exception_enabled
                    and minutes >= 10 * 60
                    and _has_compensating_rest(rest_pairs, gap_start)
                )
                if not compensated:
                    violations.append(
                        make_violation(
                            self,
                            f"Only {minutes / 60:g} hours of rest between shifts; 11 are required "
                            "unless a qualifying compensated care-sector reduction applies.",
                            entity_ids=(employee.employee_id, previous_id, following_id),
                            occurred_on=following_date,
                        )
                    )
        return violations


class DESundayRestRule(Rule):
    """Enforce the home's prorated free-Sunday house quota."""

    id = "de.free_sundays"
    title = "Free Sundays"
    citation = "Care-home house rule; Arbeitszeitgesetz (ArbZG) §11(1)"
    text = (
        "The 15-Sundays requirement is in ArbZG §11(1); §11(3) concerns substitute rest days. "
        "Separately, this home requires floor(2 × Sundays in the episode / 5) free Sundays, "
        "capped at the number of Sundays in the episode minus one. Thus at most three of each "
        "five Sundays may be worked, while a one-Sunday episode does not forbid all Sunday work. "
        "A Sunday is worked if any assigned shift overlaps it."
    )
    needs_expert_review = True

    def check(self, state: WorldState) -> list[Violation]:
        violations: list[Violation] = []
        all_sundays = sundays_in_horizon(state.start_date, state.horizon_days)
        required_free = required_free_sundays(len(all_sundays))
        if not all_sundays or required_free == 0:
            return violations
        for employee in sorted(state.employees, key=lambda item: item.employee_id):
            assignments = employee_assignments(state, employee.employee_id)
            worked_sundays = [
                sunday
                for sunday in all_sundays
                if _works_on_sunday(assignments, sunday)
            ]
            free_count = len(all_sundays) - len(worked_sundays)
            if free_count < required_free:
                violations.append(
                    make_violation(
                        self,
                        f"Only {free_count} Sundays are free; at least {required_free} are required for this horizon.",
                        entity_ids=(employee.employee_id,),
                        occurred_on=worked_sundays[0] if worked_sundays else state.start_date,
                    )
                )
        return violations


def german_rule_pack() -> RulePack:
    """Return the German rule pack in stable ID order."""
    rules: tuple[Rule, ...] = (
        DEWorkingTimeRule(),
        DERestBreakRule(),
        DERestPeriodRule(),
        DESundayRestRule(),
    )
    return RulePack(id="de-arbzg-v1", rules=rules)


def _shift_start(assignment) -> datetime:
    """Resolve an assignment start using the shared shift-time table."""
    from opsdesk.core.time_utils import shift_interval

    return shift_interval(assignment)[0]


def _shift_end(assignment) -> datetime:
    """Resolve an assignment end using the shared shift-time table."""
    from opsdesk.core.time_utils import shift_interval

    return shift_interval(assignment)[1]


def _has_compensating_rest(
    rest_pairs: list[tuple[datetime, datetime, int, str, str, date | None]],
    reduced_gap_start: datetime,
) -> bool:
    """Find a distinct 12-hour rest gap within four weeks of a reduced gap."""
    compensation_window = timedelta(days=28)
    return any(
        minutes >= 12 * 60
        and gap_start != reduced_gap_start
        and abs(gap_start - reduced_gap_start) <= compensation_window
        for gap_start, _gap_end, minutes, _previous_id, _following_id, _following_date in rest_pairs
    )


def _works_on_sunday(assignments: list, sunday: date) -> bool:
    """Whether any shift overlaps local Sunday midnight through Monday midnight."""
    sunday_start = datetime.combine(sunday, time.min)
    sunday_end = sunday_start + timedelta(days=1)
    return any(
        shift_start < sunday_end and shift_end > sunday_start
        for shift_start, shift_end in (_shift_bounds(item) for item in assignments)
    )


def _shift_bounds(assignment) -> tuple[datetime, datetime]:
    """Resolve an assignment interval using the shared shift-time table."""
    from opsdesk.core.time_utils import shift_interval

    return shift_interval(assignment)