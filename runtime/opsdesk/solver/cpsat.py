"""Cost and coverage helpers of the OpsDesk-Care verifier (public sample build).

The CP-SAT model that produced the references is not part of this sample.
"""

from __future__ import annotations
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP
from enum import StrEnum
from typing import Iterable
from pydantic import Field
from opsdesk.core.models import AgencyRequest, AgencyRequestStatus, Assignment, DomainModel, Qualification, ShiftType
from opsdesk.core.state import WorldState
from opsdesk.core.time_utils import SHIFT_TIMES, shift_interval, shift_work_minutes
from opsdesk.rules.sunday_quota import required_free_sundays, sundays_in_horizon
STABILITY_WEIGHT_CENTS = 10000000000
WORK_MINUTES = {shift_type: int((datetime.combine(date(2000, 1, 1) + timedelta(days=int(shift_times.end <= shift_times.start)), shift_times.end) - datetime.combine(date(2000, 1, 1), shift_times.start)).total_seconds() // 60) - shift_times.break_minutes for shift_type, shift_times in SHIFT_TIMES.items()}

class SolveStatus(StrEnum):
    """Normalized solver status codes stored in task references."""
    OPTIMAL = 'OPTIMAL'
    FEASIBLE = 'FEASIBLE'
    INFEASIBLE = 'INFEASIBLE'
    UNKNOWN = 'UNKNOWN'
    MODEL_INVALID = 'MODEL_INVALID'

class SolverConfig(DomainModel):
    """Fixed solver settings; deterministic work limits and one worker preserve repeatability."""
    deterministic_time_limit: float = Field(default=5.0, gt=0, le=600)
    safety_wall_time_limit_seconds: float = Field(default=600.0, gt=0, le=3600)
    random_seed: int = Field(default=0, ge=0)
    num_search_workers: int = Field(default=1, ge=1, le=1)

class CoverageGap(DomainModel):
    """A precise uncovered ward/date/shift and its outstanding requirements."""
    slot_id: str
    date: date
    ward_id: str
    shift_type: ShiftType
    required_headcount: int
    covered_headcount: int
    missing_headcount: int
    required_qualifications: dict[Qualification, int]
    covered_qualifications: dict[Qualification, int]
    missing_qualifications: dict[Qualification, int]
    reason: str
_CONSTRAINT_GROUP_LABELS = {'one_shift_per_day': 'one shift per employee per day', 'minimum_rest': 'minimum rest between adjacent shifts', 'max_consecutive_work_days': 'maximum consecutive work days', 'max_consecutive_nights': 'maximum consecutive nights', 'monthly_night_cap': 'monthly night-shift caps', 'german_sunday_rest': 'German Sunday-rest requirements', 'weekly_hours_limit': 'weekly legal-hours limits', 'contract_hours_limit': 'part-time contract-hours limits', 'contract_overtime_accounting': 'contract overtime accounting', 'german_daily_working_time': 'German daily and episode-average working-time limits', 'uk_night_worker_hours': 'UK night-worker average-hours limits', 'headcount_coverage': 'per-shift headcount coverage', 'registered_nurse_coverage': 'registered-nurse coverage', 'agency_budget': 'the agency staffing budget'}

def coverage_gaps(state: WorldState, assignments: Iterable[Assignment], agency_requests: Iterable[AgencyRequest]=()) -> list[CoverageGap]:
    """Return exact missing headcount and qualification coverage."""
    employee_by_id = {item.employee_id: item for item in state.employees}
    offer_by_id = {item.offer_id: item for item in state.agency_offers}
    assignment_list = list(assignments)
    requests = [item for item in agency_requests if item.status.value == 'confirmed']
    gaps: list[CoverageGap] = []
    for slot in sorted(state.demand_slots, key=lambda item: (item.date, item.ward_id, item.shift_type.value, item.slot_id)):
        on_slot = [item for item in assignment_list if (item.date, item.ward_id, item.shift_type) == (slot.date, slot.ward_id, slot.shift_type)]
        slot_requests = [item for item in requests if (item.requested_for, item.ward_id, item.shift_type) == (slot.date, slot.ward_id, slot.shift_type)]
        covered_headcount = len(on_slot) + sum((item.staff_count for item in slot_requests))
        required = _required_qualifications(state, slot)
        covered_qualifications: dict[Qualification, int] = defaultdict(int)
        for assignment in on_slot:
            employee = employee_by_id.get(assignment.employee_id)
            if employee is not None:
                for qualification in employee.qualifications:
                    covered_qualifications[qualification] += 1
        for request in slot_requests:
            offer = offer_by_id.get(request.offer_id)
            if offer is not None:
                covered_qualifications[offer.qualification] += request.staff_count
        missing_qualifications = {qualification: max(0, count - covered_qualifications.get(qualification, 0)) for qualification, count in required.items() if covered_qualifications.get(qualification, 0) < count}
        required_qualifications = dict(sorted(required.items(), key=lambda item: item[0].value))
        covered_qualifications = dict(sorted(covered_qualifications.items(), key=lambda item: item[0].value))
        missing_qualifications = dict(sorted(missing_qualifications.items(), key=lambda item: item[0].value))
        missing_headcount = max(0, slot.min_headcount - covered_headcount)
        if not missing_headcount and (not missing_qualifications):
            continue
        shortages = []
        if missing_headcount:
            shortages.append(f'headcount {covered_headcount}/{slot.min_headcount}')
        shortages.extend((f'{qualification.value} missing {missing}' for qualification, missing in sorted(missing_qualifications.items(), key=lambda item: item[0].value)))
        if set(missing_qualifications) == {Qualification.RN} and (not missing_headcount):
            reason = f'Insufficient legally schedulable registered nurses: {covered_qualifications.get(Qualification.RN, 0)} available for {required[Qualification.RN]} required.'
        else:
            reason = 'Insufficient coverage: ' + '; '.join(shortages) + '.'
        gaps.append(CoverageGap(slot_id=slot.slot_id, date=slot.date, ward_id=slot.ward_id, shift_type=slot.shift_type, required_headcount=slot.min_headcount, covered_headcount=covered_headcount, missing_headcount=missing_headcount, required_qualifications=required_qualifications, covered_qualifications=dict(covered_qualifications), missing_qualifications=missing_qualifications, reason=reason))
    return gaps

def coverage_total_shortage(state: WorldState, gaps: Iterable[CoverageGap]) -> int:
    """Total shortfall: missing bodies plus missing qualifications per slot.

    Additive on purpose: removing a body from a slot that is already short of a
    qualification must still lower coverage (shared by solver, scorer and
    certificate checker)."""
    by_slot = {gap.slot_id: gap for gap in gaps}
    return sum((by_slot[slot.slot_id].missing_headcount + sum(by_slot[slot.slot_id].missing_qualifications.values()) if slot.slot_id in by_slot else 0 for slot in state.demand_slots))

def coverage_position_count(state: WorldState, gaps: Iterable[CoverageGap]) -> int:
    """Count filled positions under the shared coverage shortfall rule."""
    required = sum((slot.min_headcount for slot in state.demand_slots))
    return max(0, required - coverage_total_shortage(state, gaps))

def compute_cost_breakdown(state: WorldState, assignments: Iterable[Assignment], agency_requests: Iterable[AgencyRequest]) -> dict[str, int]:
    """Recompute reference cost components from a frozen schedule."""
    employee_by_id = {item.employee_id: item for item in state.employees}
    offer_by_id = {item.offer_id: item for item in state.agency_offers}
    regular = 0
    night_premium = 0
    overtime_premium = 0
    agency_cost = 0
    weekly_minutes: dict[tuple[str, date], int] = defaultdict(int)
    for assignment in assignments:
        employee = employee_by_id[assignment.employee_id]
        minutes = shift_work_minutes(assignment)
        base = employee_base_cost_cents(employee.hourly_rate, minutes)
        regular += base
        if assignment.shift_type == ShiftType.NIGHT:
            night_premium += employee_night_premium_cents(base)
        weekly_minutes[employee.employee_id, _week_start(assignment.date)] += minutes
    horizon_end = state.start_date + timedelta(days=state.horizon_days)
    for (employee_id, week), minutes in weekly_minutes.items():
        employee = employee_by_id[employee_id]
        active_days = max(0, (min(horizon_end, week + timedelta(days=7)) - max(state.start_date, week)).days)
        contracted = int((Decimal(str(employee.contract.hours_per_week)) * Decimal(60) * Decimal(active_days) / Decimal(7)).to_integral_value(rounding=ROUND_HALF_UP))
        overtime = max(0, minutes - contracted)
        overtime_premium += overtime_premium_cents(employee.hourly_rate, overtime)
    for request in agency_requests:
        if request.status != AgencyRequestStatus.CONFIRMED:
            continue
        offer = offer_by_id[request.offer_id]
        agency_cost += agency_shift_cost_cents(offer.hourly_rate, shift_work_minutes_for_type(request.shift_type), request.staff_count)
    return {'regular_and_base_cents': regular, 'night_premium_cents': night_premium, 'overtime_premium_cents': overtime_premium, 'agency_cents': agency_cost}

def _required_qualifications(state: WorldState, slot) -> dict[Qualification, int]:
    """Merge slot qualification needs with the active house RN minimum."""
    required = dict(slot.min_qualifications)
    required[Qualification.RN] = max(required.get(Qualification.RN, 0), state.house_rule_policy.min_registered_nurses_per_shift)
    return {qualification: count for qualification, count in sorted(required.items(), key=lambda item: item[0].value) if count > 0}

def employee_base_cost_cents(rate: Decimal, minutes: int) -> int:
    return _money_cents(Decimal(rate) * Decimal(minutes) / Decimal(60))

def employee_night_premium_cents(base_cents: int) -> int:
    return int((Decimal(base_cents) * Decimal('0.20')).to_integral_value(rounding=ROUND_HALF_UP))

def overtime_premium_cents(rate: Decimal, minutes: int) -> int:
    return _money_cents(Decimal(rate) * Decimal(minutes) * Decimal('0.5') / Decimal(60))

def agency_shift_cost_cents(rate: Decimal, minutes: int, staff_count: int) -> int:
    return _money_cents(Decimal(rate) * Decimal(minutes) * Decimal(staff_count) / Decimal(60))

def shift_work_minutes_for_type(shift_type: ShiftType) -> int:
    """Return the configured work duration for one shift type."""
    return WORK_MINUTES[shift_type]

def _week_start(work_date: date) -> date:
    """Return the Monday starting the rule-checker's calendar week."""
    return work_date - timedelta(days=work_date.weekday())

def _money_cents(amount: Decimal) -> int:
    """Convert a decimal currency amount to integer cents."""
    return int((amount * Decimal(100)).to_integral_value(rounding=ROUND_HALF_UP))
