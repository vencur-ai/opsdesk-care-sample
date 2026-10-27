"""UK Working Time Regulations checks for the 30-day simulation horizon."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta

from opsdesk.core.models import ShiftType
from opsdesk.core.state import WorldState
from opsdesk.core.time_utils import shift_break_minutes, shift_work_minutes
from opsdesk.rules.base import (
    Rule,
    RulePack,
    Violation,
    employee_assignments,
    employee_map,
    gap_minutes,
    horizon_bounds,
    make_violation,
    rest_gaps_in_window,
    week_start,
)


class UKDailyRestRule(Rule):
    """Require 11 consecutive hours of rest between shifts."""

    id = "uk.daily_rest"
    title = "Daily rest"
    citation = "Working Time Regulations 1998, regulation 10"
    text = "An adult worker must receive at least 11 consecutive hours of rest between working days."
    needs_expert_review = True

    def check(self, state: WorldState) -> list[Violation]:
        violations: list[Violation] = []
        for employee in sorted(state.employees, key=lambda item: item.employee_id):
            assignments = employee_assignments(state, employee.employee_id)
            for previous, following in zip(assignments, assignments[1:]):
                previous_end = _assignment_end(previous)
                next_start = _assignment_start(following)
                rest = int((next_start - previous_end).total_seconds() // 60)
                if rest < 11 * 60:
                    violations.append(
                        make_violation(
                            self,
                            f"Only {rest / 60:g} hours of rest between adjacent shifts; 11 are required.",
                            entity_ids=(employee.employee_id, previous.assignment_id, following.assignment_id),
                            occurred_on=following.date,
                        )
                    )
        return violations


class UKWeeklyRestRule(Rule):
    """Check weekly rest, with the statutory 48-hour fortnight alternative."""

    id = "uk.weekly_rest"
    title = "Weekly rest"
    citation = "Working Time Regulations 1998, regulation 11"
    text = (
        "The schedule must provide 24 uninterrupted hours in each seven-day period; "
        "alternatively, it may provide 48 uninterrupted hours in a 14-day period. "
        "This check uses the episode horizon and does not inspect shifts before or after it."
    )
    needs_expert_review = True

    def check(self, state: WorldState) -> list[Violation]:
        if state.horizon_days < 7:
            return []
        violations: list[Violation] = []
        for employee in sorted(state.employees, key=lambda item: item.employee_id):
            assignments = employee_assignments(state, employee.employee_id)
            window_index = 0
            while window_index + 14 <= state.horizon_days:
                period_start = state.start_date + timedelta(days=window_index)
                first_week = _has_rest(assignments, period_start, 7, 24 * 60)
                second_week = _has_rest(assignments, period_start + timedelta(days=7), 7, 24 * 60)
                fortnight = _has_rest(assignments, period_start, 14, 48 * 60)
                if not ((first_week and second_week) or fortnight):
                    violations.append(
                        make_violation(
                            self,
                            "No 24-hour rest in both seven-day blocks or 48-hour rest in the fortnight.",
                            entity_ids=(employee.employee_id,),
                            occurred_on=period_start,
                        )
                    )
                window_index += 14
            if window_index + 7 <= state.horizon_days:
                final_week = state.start_date + timedelta(days=window_index)
                if not _has_rest(assignments, final_week, 7, 24 * 60):
                    violations.append(
                        make_violation(
                            self,
                            "No uninterrupted 24-hour rest was found in the final seven-day block.",
                            entity_ids=(employee.employee_id,),
                            occurred_on=final_week,
                        )
                    )
        return violations


class UKWeeklyHoursRule(Rule):
    """Apply the 48-hour weekly average limit unless an employee opted out."""

    id = "uk.weekly_hours"
    title = "Average weekly working time"
    citation = "Working Time Regulations 1998, regulation 4"
    text = (
        "Working time must not exceed an average of 48 hours per week unless the employee has opted out. "
        "The 30-day scenario checker uses rostered hours grouped by Monday-to-Sunday week; it cannot "
        "establish the statutory 17-week average without prior work history."
    )
    needs_expert_review = True

    def check(self, state: WorldState) -> list[Violation]:
        violations: list[Violation] = []
        for employee in sorted(state.employees, key=lambda item: item.employee_id):
            if employee.weekly_hours_opt_out:
                continue
            weekly_minutes: dict[date, int] = defaultdict(int)
            for assignment in employee_assignments(state, employee.employee_id):
                weekly_minutes[week_start(assignment.date)] += shift_work_minutes(assignment)
            for monday, minutes in sorted(weekly_minutes.items()):
                if minutes > 48 * 60:
                    violations.append(
                        make_violation(
                            self,
                            f"{minutes / 60:g} rostered working hours exceed the 48-hour weekly limit.",
                            entity_ids=(employee.employee_id,),
                            occurred_on=monday,
                        )
                    )
        return violations


class UKRestBreakRule(Rule):
    """Require a 20-minute rest break when a shift exceeds six working hours."""

    id = "uk.rest_break"
    title = "Rest breaks"
    citation = "Working Time Regulations 1998, regulation 12"
    text = "A worker working more than six hours is entitled to a rest break of at least 20 minutes."
    needs_expert_review = True

    def check(self, state: WorldState) -> list[Violation]:
        violations: list[Violation] = []
        for assignment in sorted(state.roster.assignments, key=lambda item: item.assignment_id):
            worked = shift_work_minutes(assignment)
            if worked > 6 * 60 and shift_break_minutes(assignment) < 20:
                violations.append(
                    make_violation(
                        self,
                        f"The shift provides {shift_break_minutes(assignment)} minutes of break; at least 20 are required.",
                        entity_ids=(assignment.employee_id, assignment.assignment_id),
                        occurred_on=assignment.date,
                    )
                )
        return violations


class UKNightWorkerRule(Rule):
    """Limit the episode-average length of night shifts for night workers."""

    id = "uk.night_worker_hours"
    title = "Night-worker average hours"
    citation = "Working Time Regulations 1998, regulation 6"
    text = (
        "A night worker must not work more than an average of eight hours in each 24-hour period. "
        "This v0.1 check averages rostered night-shift hours over this episode only; the legal "
        "averaging window and special-hazard cases need expert review."
    )
    needs_expert_review = True

    def check(self, state: WorldState) -> list[Violation]:
        violations: list[Violation] = []
        for employee in sorted(state.employees, key=lambda item: item.employee_id):
            if not employee.is_night_worker:
                continue
            night_assignments = [
                item
                for item in employee_assignments(state, employee.employee_id)
                if item.shift_type == ShiftType.NIGHT
            ]
            if not night_assignments:
                continue
            minutes_by_night: dict[date, int] = defaultdict(int)
            for assignment in night_assignments:
                minutes_by_night[assignment.date] += shift_work_minutes(assignment)
            average_minutes = sum(minutes_by_night.values()) / len(minutes_by_night)
            if average_minutes > 8 * 60:
                violations.append(
                    make_violation(
                        self,
                        f"Average rostered night shift is {average_minutes / 60:g} hours; the limit is 8.",
                        entity_ids=(
                            employee.employee_id,
                            *(item.assignment_id for item in night_assignments),
                        ),
                        occurred_on=min(minutes_by_night),
                    )
                )
        return violations


def uk_rule_pack() -> RulePack:
    """Return the UK rule pack in stable ID order."""
    rules: tuple[Rule, ...] = (
        UKDailyRestRule(),
        UKWeeklyRestRule(),
        UKWeeklyHoursRule(),
        UKRestBreakRule(),
        UKNightWorkerRule(),
    )
    return RulePack(id="uk-wtr-1998-v1", rules=rules)


def _has_rest(
    assignments: list,
    period_start: date,
    days: int,
    required_minutes: int,
) -> bool:
    """Whether the period contains a continuous gap at least as long as required."""
    start = datetime.combine(period_start, time.min)
    end = start + timedelta(days=days)
    return any(
        gap_minutes(gap) >= required_minutes
        for gap in rest_gaps_in_window(assignments, start, end)
    )


def _assignment_start(assignment) -> datetime:
    """Resolve an assignment start using the shared shift-time table."""
    from opsdesk.core.time_utils import shift_interval

    return shift_interval(assignment)[0]


def _assignment_end(assignment) -> datetime:
    """Resolve an assignment end using the shared shift-time table."""
    from opsdesk.core.time_utils import shift_interval

    return shift_interval(assignment)[1]