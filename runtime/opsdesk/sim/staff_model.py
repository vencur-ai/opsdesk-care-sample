"""Rule-based staff responses with no external model or ambient randomness."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Iterable

from opsdesk.core.models import (
    DomainModel,
    Qualification,
    ShiftType,
    StaffPersona,
)
from opsdesk.core.state import WorldState
from opsdesk.core.time_utils import shift_interval
from opsdesk.rules.base import RuleKind
from opsdesk.rules.engine import check_world_rules


class StaffDecision(DomainModel):
    """A deterministic decision returned by a staff persona."""

    accepted: bool
    reason: str


def decide_shift_request(
    state: WorldState,
    employee_id: str,
    shift_date: date,
    shift_type: ShiftType,
    ward_id: str,
    *,
    assignment_id: str | None = None,
    ignore_assignment_ids: Iterable[str] = (),
    is_swap: bool = False,
) -> StaffDecision:
    """Decide whether an employee will accept a proposed shift."""
    employees = {item.employee_id: item for item in state.employees}
    employee = employees.get(employee_id)
    if employee is None:
        return StaffDecision(accepted=False, reason="Employee not found.")
    if shift_date < state.start_date or shift_date >= state.start_date + timedelta(
        days=state.horizon_days
    ):
        return StaffDecision(accepted=False, reason="The requested shift is outside this episode.")
    if shift_date.weekday() in employee.fixed_days_off:
        return StaffDecision(accepted=False, reason="The employee has this weekday fixed off.")
    if employee.night_only and shift_type != ShiftType.NIGHT:
        return StaffDecision(accepted=False, reason="The employee only accepts night shifts.")

    persona = next(
        (
            item
            for item in state.staff_personas
            if item.employee_id == employee_id
        ),
        StaffPersona(employee_id=employee_id),
    )
    if persona.never_sundays and _shift_overlaps_sunday(shift_date, shift_type):
        return StaffDecision(accepted=False, reason="The employee does not work Sundays.")
    if persona.preferred_shift_types and shift_type not in persona.preferred_shift_types:
        return StaffDecision(accepted=False, reason="The requested shift is not one of the employee's preferred shifts.")
    if is_swap and not persona.accepts_swaps:
        return StaffDecision(accepted=False, reason="The employee declines shift swaps.")
    if not is_swap and not persona.accepts_extra_shifts:
        return StaffDecision(accepted=False, reason="The employee declines additional shifts.")

    proposed = {
        "assignment_id": assignment_id or f"staff-check-{employee_id}-{shift_date.isoformat()}-{shift_type.value}",
        "employee_id": employee_id,
        "date": shift_date,
        "ward_id": ward_id,
        "shift_type": shift_type,
    }
    from opsdesk.core.models import Assignment

    assignment = Assignment(**proposed)
    start, end = shift_interval(assignment)
    ignored = set(ignore_assignment_ids)
    existing = [
        item
        for item in state.roster.assignments
        if item.employee_id == employee_id and item.assignment_id not in ignored
    ]
    if any(
        start < existing_end and end > existing_start
        for existing_start, existing_end in (
            shift_interval(item) for item in existing
        )
    ):
        return StaffDecision(accepted=False, reason="The employee is already rostered during that time.")
    if any(
        absence.employee_id == employee_id
        and start < datetime.combine(absence.end_date + timedelta(days=1), time.min)
        and end > datetime.combine(absence.start_date, time.min)
        for absence in state.absences
    ):
        return StaffDecision(accepted=False, reason="The employee has an absence covering that shift.")

    matching_demand = [
        item
        for item in state.demand_slots
        if item.date == shift_date and item.ward_id == ward_id and item.shift_type == shift_type
    ]
    already_assigned = [
        item
        for item in state.roster.assignments
        if item.date == shift_date
        and item.ward_id == ward_id
        and item.shift_type == shift_type
        and item.assignment_id not in ignored
    ]
    for demand in matching_demand:
        for qualification, required in sorted(
            demand.min_qualifications.items(), key=lambda pair: pair[0].value
        ):
            covered = sum(
                1
                for current in already_assigned
                if current.employee_id in employees
                and qualification in employees[current.employee_id].qualifications
            )
            if covered < required and qualification not in employee.qualifications:
                return StaffDecision(
                    accepted=False,
                    reason=f"The shift needs a {qualification.value} and the employee does not hold that qualification.",
                )

    baseline_state = state.model_copy(deep=True)
    baseline_state.roster.assignments = [
        item
        for item in baseline_state.roster.assignments
        if item.assignment_id not in ignored
    ]
    baseline_signatures = {
        _violation_signature(item)
        for item in check_world_rules(baseline_state)
        if item.kind == RuleKind.HARD
    }
    proposed_state = baseline_state.model_copy(deep=True)
    proposed_state.roster.assignments.append(assignment)
    new_violations = [
        item
        for item in check_world_rules(proposed_state)
        if item.kind == RuleKind.HARD
        and _violation_signature(item) not in baseline_signatures
        and (
            employee_id in item.entity_ids
            or assignment.assignment_id in item.entity_ids
        )
    ]
    if new_violations:
        return StaffDecision(
            accepted=False,
            reason=f"The requested shift would violate {new_violations[0].rule_id}.",
        )
    return StaffDecision(accepted=True, reason="The employee accepts the requested shift.")


def _violation_signature(violation) -> tuple:
    """Build a stable identity for before-and-after rule comparisons."""
    return (
        violation.rule_id,
        violation.entity_ids,
        violation.occurred_on,
        violation.message,
    )


def _shift_overlaps_sunday(shift_date: date, shift_type: ShiftType) -> bool:
    """Return whether a shift intersects any Sunday, including overnight shifts."""
    probe = {
        "assignment_id": "sunday-check",
        "employee_id": "sunday-check",
        "date": shift_date,
        "ward_id": "sunday-check",
        "shift_type": shift_type,
    }
    from opsdesk.core.models import Assignment

    start, end = shift_interval(Assignment(**probe))
    sunday = start.date() - timedelta(days=(start.date().weekday() + 1) % 7)
    while sunday <= end.date():
        sunday_start = datetime.combine(sunday, time.min)
        if sunday.weekday() == 6 and start < sunday_start + timedelta(days=1) and end > sunday_start:
            return True
        sunday += timedelta(days=1)
    return False