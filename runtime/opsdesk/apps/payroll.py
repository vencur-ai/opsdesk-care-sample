"""Deterministic roster and time-clock payroll exports."""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import model_validator

from opsdesk.apps.base import AppTool, ToolArgs, to_json_data
from opsdesk.apps.ids import next_monotonic_id
from opsdesk.core.models import PayrollExport, PayrollRow
from opsdesk.core.state import WorldState
from opsdesk.core.time_utils import shift_work_minutes


class ExportPayrollArgs(ToolArgs):
    """Inclusive payroll period."""

    start_date: date
    end_date: date

    @model_validator(mode="after")
    def validate_range(self) -> ExportPayrollArgs:
        """Reject a reversed payroll period."""
        if self.end_date < self.start_date:
            raise ValueError("end_date must be on or after start_date")
        return self


class ExportPayrollTool(AppTool):
    """Create a saved payroll export showing scheduled and clocked minutes."""

    app = "payroll"
    name = "payroll.export"
    description = "Export rostered and clocked hours with payable totals and discrepancies."
    args_model = ExportPayrollArgs

    def execute(self, state: WorldState, args: ExportPayrollArgs) -> dict[str, Any]:
        employees = sorted(state.employees, key=lambda item: item.employee_id)
        assignments = [
            item
            for item in state.roster.assignments
            if args.start_date <= item.date <= args.end_date
        ]
        clock_entries = [
            item
            for item in state.timeclock_entries
            if args.start_date <= item.shift_date <= args.end_date
        ]
        rows = []
        for employee in employees:
            rostered_minutes = sum(
                shift_work_minutes(item)
                for item in assignments
                if item.employee_id == employee.employee_id
            )
            clocked_minutes = sum(
                int((item.clock_out - item.clock_in).total_seconds() // 60)
                for item in clock_entries
                if item.employee_id == employee.employee_id
            )
            rows.append(
                PayrollRow(
                    employee_id=employee.employee_id,
                    rostered_minutes=rostered_minutes,
                    clocked_minutes=clocked_minutes,
                    payable_minutes=clocked_minutes,
                    discrepancy_minutes=clocked_minutes - rostered_minutes,
                )
            )

        export = PayrollExport(
            export_id=_next_export_id(state),
            start_date=args.start_date,
            end_date=args.end_date,
            created_on=state.current_date,
            rows=rows,
        )
        state.payroll_exports.append(export)
        return to_json_data(export)


def _next_export_id(state: WorldState) -> str:
    """Allocate a payroll export ID without assuming contiguous imported IDs."""
    return next_monotonic_id("payroll", (item.export_id for item in state.payroll_exports))