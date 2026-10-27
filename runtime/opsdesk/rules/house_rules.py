"""Seeded templates and checks for per-home operational rules."""

from __future__ import annotations

import random
from collections import defaultdict
from datetime import date, timedelta
from typing import ClassVar

from opsdesk.core.models import (
    AbsenceType,
    Assignment,
    EmploymentType,
    HouseRulePolicy,
    Qualification,
    ShiftType,
)
from opsdesk.core.state import WorldState
from opsdesk.core.time_utils import shift_interval, shift_work_minutes
from opsdesk.rules.base import (
    Rule,
    RulePack,
    Violation,
    employee_assignments,
    employee_map,
    make_violation,
    week_start,
)


class EmployeeAvailabilityRule(Rule):
    """Respect stated days off, night-only contracts, and recorded absences."""

    id = "house.employee_availability"
    title = "Employee availability"
    citation = "Employee contract and recorded absences"
    text = "Do not schedule an employee on a fixed day off, outside a night-only contract, or during an absence."

    def check(self, state: WorldState) -> list[Violation]:
        employees = employee_map(state)
        violations: list[Violation] = []
        for assignment in sorted(state.roster.assignments, key=lambda item: item.assignment_id):
            employee = employees.get(assignment.employee_id)
            if employee is None:
                continue
            if assignment.date.weekday() in employee.fixed_days_off:
                violations.append(
                    make_violation(
                        self,
                        "The employee is rostered on a fixed day off.",
                        entity_ids=(employee.employee_id, assignment.assignment_id),
                        occurred_on=assignment.date,
                    )
                )
            if employee.night_only and assignment.shift_type != ShiftType.NIGHT:
                violations.append(
                    make_violation(
                        self,
                        "The employee has a night-only contract but is assigned to a non-night shift.",
                        entity_ids=(employee.employee_id, assignment.assignment_id),
                        occurred_on=assignment.date,
                    )
                )
            start, end = shift_interval(assignment)
            for absence in sorted(state.absences, key=lambda item: item.absence_id):
                if absence.employee_id != employee.employee_id:
                    continue
                absence_start = _midnight(absence.start_date)
                absence_end = _midnight(absence.end_date + timedelta(days=1))
                if start < absence_end and end > absence_start:
                    violations.append(
                        make_violation(
                            self,
                            f"The shift overlaps the employee's {absence.absence_type.value} absence.",
                            entity_ids=(employee.employee_id, assignment.assignment_id, absence.absence_id),
                            occurred_on=assignment.date,
                        )
                    )
        return violations


class MaxConsecutiveNightsRule(Rule):
    """Limit consecutive calendar days containing a night shift."""

    id = "house.max_consecutive_nights"
    title = "Consecutive night shifts"
    citation = "Care-home house rule"
    text = "Do not schedule more consecutive night shifts than the home's configured maximum."

    def __init__(self, policy: HouseRulePolicy) -> None:
        self.policy = policy

    def check(self, state: WorldState) -> list[Violation]:
        violations: list[Violation] = []
        for employee in sorted(state.employees, key=lambda item: item.employee_id):
            nights = sorted(
                {
                    item.date
                    for item in employee_assignments(state, employee.employee_id)
                    if item.shift_type == ShiftType.NIGHT
                }
            )
            run: list[date] = []
            for night in nights:
                if run and night != run[-1] + timedelta(days=1):
                    run = []
                run.append(night)
                if len(run) == self.policy.max_consecutive_nights + 1:
                    assignment_ids = [
                        item.assignment_id
                        for item in employee_assignments(state, employee.employee_id)
                        if item.date in run and item.shift_type == ShiftType.NIGHT
                    ]
                    violations.append(
                        make_violation(
                            self,
                            "The assignment exceeds the consecutive-night limit.",
                            entity_ids=(employee.employee_id, *assignment_ids),
                            occurred_on=night,
                        )
                    )
        return violations


class MaximumMonthlyNightsRule(Rule):
    """Enforce each employee's monthly night-shift cap."""

    id = "house.monthly_night_limit"
    title = "Monthly night-shift limit"
    citation = "Employee contract"
    text = "Do not exceed the maximum number of night shifts stated in the employee record."

    def check(self, state: WorldState) -> list[Violation]:
        violations: list[Violation] = []
        for employee in sorted(state.employees, key=lambda item: item.employee_id):
            monthly: dict[tuple[int, int], list[Assignment]] = defaultdict(list)
            for assignment in employee_assignments(state, employee.employee_id):
                if assignment.shift_type == ShiftType.NIGHT:
                    monthly[(assignment.date.year, assignment.date.month)].append(assignment)
            for (year, month), assignments in sorted(monthly.items()):
                if len(assignments) > employee.max_nights_per_month:
                    violations.append(
                        make_violation(
                            self,
                            f"{len(assignments)} night shifts exceed the employee's monthly limit of "
                            f"{employee.max_nights_per_month}.",
                            entity_ids=(
                                employee.employee_id,
                                *(item.assignment_id for item in assignments),
                            ),
                            occurred_on=date(year, month, 1),
                        )
                    )
        return violations


class MinimumRegisteredNursesRule(Rule):
    """Require the configured RN count for each demanded ward and shift."""

    id = "house.minimum_registered_nurses"
    title = "Registered-nurse coverage"
    citation = "Care-home house rule"
    text = "Every demanded ward, date, and shift must have the home's configured minimum number of RNs."

    def __init__(self, policy: HouseRulePolicy) -> None:
        self.policy = policy

    def check(self, state: WorldState) -> list[Violation]:
        employees = employee_map(state)
        requirements: dict[tuple[date, str, ShiftType], int] = {}
        for slot in sorted(
            state.demand_slots,
            key=lambda item: (item.date, item.ward_id, item.shift_type.value, item.slot_id),
        ):
            key = (slot.date, slot.ward_id, slot.shift_type)
            requirements[key] = max(
                requirements.get(key, 0),
                self.policy.min_registered_nurses_per_shift,
                slot.min_qualifications.get(Qualification.RN, 0),
            )
        if not requirements:
            requirements = {
                (item.date, item.ward_id, item.shift_type): self.policy.min_registered_nurses_per_shift
                for item in state.roster.assignments
            }

        violations: list[Violation] = []
        offers = {item.offer_id: item for item in state.agency_offers}
        for (shift_date, ward_id, shift_type), required in sorted(
            requirements.items(),
            key=lambda item: (item[0][0], item[0][1], item[0][2].value),
        ):
            if required == 0:
                continue
            assignments = [
                item
                for item in state.roster.assignments
                if item.date == shift_date
                and item.ward_id == ward_id
                and item.shift_type == shift_type
            ]
            nurses = {
                item.employee_id
                for item in assignments
                if item.employee_id in employees
                and Qualification.RN in employees[item.employee_id].qualifications
            }
            agency_nurses = sum(
                request.staff_count
                for request in state.agency_requests
                if request.status.value == "confirmed"
                and request.requested_for == shift_date
                and request.ward_id == ward_id
                and request.shift_type == shift_type
                and request.offer_id in offers
                and offers[request.offer_id].qualification == Qualification.RN
            )
            if len(nurses) + agency_nurses < required:
                violations.append(
                    make_violation(
                        self,
                        f"{len(nurses) + agency_nurses} RNs cover {shift_type.value} in {ward_id}; {required} are required.",
                        entity_ids=(ward_id, *(item.assignment_id for item in assignments)),
                        staff_id="",
                        occurred_on=shift_date,
                    )
                )
        return violations


class NoLateToEarlyRule(Rule):
    """Forbid late shifts immediately followed by an early shift."""

    id = "house.no_late_to_early"
    title = "Late-to-early turnaround"
    citation = "Care-home house rule"
    text = "An employee must not work a late shift and then an early shift on the following day."

    def __init__(self, policy: HouseRulePolicy) -> None:
        self.policy = policy

    def check(self, state: WorldState) -> list[Violation]:
        if not self.policy.forbid_late_to_early:
            return []
        violations: list[Violation] = []
        for employee in sorted(state.employees, key=lambda item: item.employee_id):
            assignments = employee_assignments(state, employee.employee_id)
            for previous, following in zip(assignments, assignments[1:]):
                if (
                    previous.shift_type == ShiftType.LATE
                    and following.shift_type == ShiftType.EARLY
                    and following.date == previous.date + timedelta(days=1)
                ):
                    violations.append(
                        make_violation(
                            self,
                            "A late shift is followed by an early shift the next day.",
                            entity_ids=(employee.employee_id, previous.assignment_id, following.assignment_id),
                            occurred_on=following.date,
                        )
                    )
        return violations


class MaximumConsecutiveWorkDaysRule(Rule):
    """Limit uninterrupted runs of working calendar days."""

    id = "house.max_consecutive_work_days"
    title = "Consecutive working days"
    citation = "Care-home house rule"
    text = "Do not schedule more consecutive working days than the home's configured maximum."

    def __init__(self, policy: HouseRulePolicy) -> None:
        self.policy = policy

    def check(self, state: WorldState) -> list[Violation]:
        violations: list[Violation] = []
        for employee in sorted(state.employees, key=lambda item: item.employee_id):
            assignments = employee_assignments(state, employee.employee_id)
            assignment_days: dict[date, list[Assignment]] = defaultdict(list)
            for assignment in assignments:
                assignment_days[assignment.date].append(assignment)
            days = sorted(assignment_days)
            run: list[date] = []
            for work_date in days:
                if run and work_date != run[-1] + timedelta(days=1):
                    run = []
                run.append(work_date)
                if len(run) == self.policy.max_consecutive_work_days + 1:
                    ids = [
                        item.assignment_id
                        for day in run
                        for item in assignment_days[day]
                    ]
                    violations.append(
                        make_violation(
                            self,
                            f"The employee exceeds {self.policy.max_consecutive_work_days} consecutive working days.",
                            entity_ids=(employee.employee_id, *ids),
                            occurred_on=work_date,
                        )
                    )
        return violations


class PartTimeHoursRule(Rule):
    """Keep part-time hours within the configured contract margin."""

    id = "house.part_time_hours"
    title = "Part-time contract hours"
    citation = "Care-home house rule and employee contract"
    text = "A part-time employee must not exceed contracted weekly hours plus the configured margin."

    def __init__(self, policy: HouseRulePolicy) -> None:
        self.policy = policy

    def check(self, state: WorldState) -> list[Violation]:
        violations: list[Violation] = []
        for employee in sorted(state.employees, key=lambda item: item.employee_id):
            if employee.contract.employment_type != EmploymentType.PART_TIME:
                continue
            weekly_assignments: dict[date, list[Assignment]] = defaultdict(list)
            for assignment in employee_assignments(state, employee.employee_id):
                weekly_assignments[week_start(assignment.date)].append(assignment)
            allowance = employee.contract.hours_per_week * (
                1 + self.policy.part_time_hours_margin_percent / 100
            )
            for monday, assignments in sorted(weekly_assignments.items()):
                hours = sum(shift_work_minutes(item) for item in assignments) / 60
                if hours > allowance:
                    violations.append(
                        make_violation(
                            self,
                            f"{hours:g} hours exceed the part-time allowance of {allowance:g} hours.",
                            entity_ids=(
                                employee.employee_id,
                                *(item.assignment_id for item in assignments),
                            ),
                            occurred_on=monday,
                        )
                    )
        return violations


def generate_house_rule_policy(rng: random.Random, home_id: str) -> HouseRulePolicy:
    """Generate a home policy from fixed templates using the caller's seeded RNG."""
    if not home_id:
        raise ValueError("home_id must not be empty")
    return HouseRulePolicy(
        policy_id=f"{home_id}-house-v1",
        max_consecutive_nights=rng.choice((2, 3, 3, 4)),
        min_registered_nurses_per_shift=rng.choice((1, 1, 1, 2)),
        forbid_late_to_early=rng.choice((True, True, False)),
        max_consecutive_work_days=rng.choice((5, 5, 6)),
        part_time_hours_margin_percent=rng.choice((10.0, 10.0, 15.0)),
    )


def build_house_rule_pack(policy: HouseRulePolicy) -> RulePack:
    """Build the operational rule pack for a home-specific policy."""
    rules: tuple[Rule, ...] = (
        EmployeeAvailabilityRule(),
        MaxConsecutiveNightsRule(policy),
        MaximumMonthlyNightsRule(),
        MinimumRegisteredNursesRule(policy),
        NoLateToEarlyRule(policy),
        MaximumConsecutiveWorkDaysRule(policy),
        PartTimeHoursRule(policy),
    )
    return RulePack(id=policy.policy_id, rules=rules)


def _midnight(day: date):
    """Return naive midnight for a simulation date."""
    from datetime import datetime, time

    return datetime.combine(day, time.min)