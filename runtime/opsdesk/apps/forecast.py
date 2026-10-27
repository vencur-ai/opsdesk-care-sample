"""Deterministic demand and coverage forecasts."""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import Field, model_validator

from opsdesk.apps.base import AppTool, ToolArgs
from opsdesk.core.models import AgencyRequestStatus, Qualification
from opsdesk.core.state import WorldState
from opsdesk.core.time_utils import SHIFT_TIMES


class DemandForecastArgs(ToolArgs):
    """Date and ward filters for demand coverage."""

    start_date: date
    end_date: date
    ward_id: str | None = None
    only_shortfalls: bool = False

    @model_validator(mode="after")
    def validate_range(self) -> DemandForecastArgs:
        """Reject a reversed forecast date range."""
        if self.end_date < self.start_date:
            raise ValueError("end_date must be on or after start_date")
        return self


class DemandForecastTool(AppTool):
    """Summarize required and currently covered staff per demand slot."""

    app = "forecast"
    name = "forecast.get"
    description = "Compare forecast staffing demand with roster and confirmed agency coverage."
    args_model = DemandForecastArgs

    def execute(self, state: WorldState, args: DemandForecastArgs) -> list[dict[str, Any]]:
        employees = {item.employee_id: item for item in state.employees}
        result: list[dict[str, Any]] = []
        for slot in sorted(
            state.demand_slots,
            key=lambda item: (item.date, item.ward_id, item.shift_type.value, item.slot_id),
        ):
            if not args.start_date <= slot.date <= args.end_date:
                continue
            if args.ward_id and slot.ward_id != args.ward_id:
                continue
            assigned = [
                item
                for item in state.roster.assignments
                if item.date == slot.date
                and item.ward_id == slot.ward_id
                and item.shift_type == slot.shift_type
            ]
            agency_requests = [
                item
                for item in state.agency_requests
                if item.requested_for == slot.date
                and item.ward_id == slot.ward_id
                and item.shift_type == slot.shift_type
                and item.status == AgencyRequestStatus.CONFIRMED
            ]
            agency_headcount = sum(item.staff_count for item in agency_requests)
            qualifications: dict[str, dict[str, int]] = {}
            for qualification, required in sorted(
                slot.min_qualifications.items(), key=lambda pair: pair[0].value
            ):
                staff_count = sum(
                    1
                    for assignment in assigned
                    if assignment.employee_id in employees
                    and qualification in employees[assignment.employee_id].qualifications
                )
                agency_count = sum(
                    item.staff_count
                    for item in agency_requests
                    if item.qualification == qualification
                )
                qualifications[qualification.value] = {
                    "required": required,
                    "assigned": staff_count,
                    "agency": agency_count,
                    "shortfall": max(0, required - staff_count - agency_count),
                }
            total_covered = len(assigned) + agency_headcount
            shift_times = SHIFT_TIMES[slot.shift_type]
            result.append(
                {
                    "slot_id": slot.slot_id,
                    "date": slot.date.isoformat(),
                    "ward_id": slot.ward_id,
                    "shift_type": slot.shift_type.value,
                    "shift_start_time": shift_times.start.strftime("%H:%M"),
                    "shift_end_time": shift_times.end.strftime("%H:%M"),
                    "break_minutes": shift_times.break_minutes,
                    "required_headcount": slot.min_headcount,
                    "rostered_headcount": len(assigned),
                    "agency_headcount": agency_headcount,
                    "headcount_shortfall": max(0, slot.min_headcount - total_covered),
                    "qualification_coverage": qualifications,
                }
            )
        if args.only_shortfalls:
            result = [
                item for item in result
                if item["headcount_shortfall"] > 0
                or any(
                    value["shortfall"] > 0
                    for value in item["qualification_coverage"].values()
                )
            ]
        return result