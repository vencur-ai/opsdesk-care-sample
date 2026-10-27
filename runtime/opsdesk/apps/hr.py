"""Human-resources read tools."""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import Field, model_validator

from opsdesk.apps.base import AppTool, ToolArgs, to_json_data
from opsdesk.core.models import Qualification
from opsdesk.core.state import WorldState


class ListEmployeesArgs(ToolArgs):
    """Filters for the employee directory."""

    qualification: Qualification | None = None
    available_on: date | None = None
    compact: bool = False


class GetEmployeeArgs(ToolArgs):
    """Identifier for one employee record."""

    employee_id: str = Field(min_length=1)


class ListAbsencesArgs(ToolArgs):
    """Optional filters for recorded absences."""

    employee_id: str | None = None
    start_date: date | None = None
    end_date: date | None = None

    @model_validator(mode="after")
    def validate_range(self) -> ListAbsencesArgs:
        """Validate any supplied absence date range."""
        if (
            self.start_date is not None
            and self.end_date is not None
            and self.end_date < self.start_date
        ):
            raise ValueError("end_date must be on or after start_date")
        return self


class ListEmployeesTool(AppTool):
    """List employees, with optional qualification and availability filters."""

    app = "hr"
    name = "hr.list_employees"
    description = "List synthetic employees, optionally filtered by qualification or availability date."
    args_model = ListEmployeesArgs

    def execute(self, state: WorldState, args: ListEmployeesArgs) -> list[dict[str, Any]]:
        employees = []
        for employee in sorted(state.employees, key=lambda item: item.employee_id):
            if args.qualification and args.qualification not in employee.qualifications:
                continue
            if args.available_on and not _is_available(state, employee.employee_id, args.available_on):
                continue
            data = employee.model_dump(mode="json")
            # These are the only set-valued Employee fields; sorting preserves
            # the stable ordering guaranteed by to_json_data without its costly
            # recursive walk across every staff record on every read.
            data["qualifications"] = sorted(data["qualifications"])
            data["fixed_days_off"] = sorted(data["fixed_days_off"])
            if args.compact:
                data = {
                    key: data[key]
                    for key in ("employee_id", "qualifications", "contract", "fixed_days_off")
                }
            employees.append(data)
        return employees


class GetEmployeeTool(AppTool):
    """Read one employee, contract, qualification, persona, and absence record."""

    app = "hr"
    name = "hr.get_employee"
    description = "Get one employee's synthetic profile, contract, qualifications, preferences, and absences."
    args_model = GetEmployeeArgs

    def execute(self, state: WorldState, args: GetEmployeeArgs) -> dict[str, Any]:
        employee = next(
            (item for item in state.employees if item.employee_id == args.employee_id),
            None,
        )
        if employee is None:
            from opsdesk.apps.base import ToolError

            raise ToolError("Employee not found.")
        persona = next(
            (item for item in state.staff_personas if item.employee_id == args.employee_id),
            None,
        )
        absences = [
            to_json_data(item)
            for item in sorted(state.absences, key=lambda item: (item.start_date, item.absence_id))
            if item.employee_id == args.employee_id
        ]
        return {
            "employee": to_json_data(employee),
            # Recorded preferences only. Whether someone agrees to a swap or an
            # extra shift is learned by asking them (roster.swap / messaging),
            # not read from the HR file.
            "preferences": (
                {
                    key: value
                    for key, value in to_json_data(persona).items()
                    if key not in {"accepts_swaps", "accepts_extra_shifts"}
                }
                if persona
                else None
            ),
            "absences": absences,
        }


class ListAbsencesTool(AppTool):
    """List absences for an employee or date range."""

    app = "hr"
    name = "hr.list_absences"
    description = "List scheduled and recorded absences, optionally filtered by employee or date range."
    args_model = ListAbsencesArgs

    def execute(self, state: WorldState, args: ListAbsencesArgs) -> list[dict[str, Any]]:
        results = []
        for absence in sorted(state.absences, key=lambda item: (item.start_date, item.absence_id)):
            if args.employee_id and absence.employee_id != args.employee_id:
                continue
            if args.start_date and absence.end_date < args.start_date:
                continue
            if args.end_date and absence.start_date > args.end_date:
                continue
            results.append(to_json_data(absence))
        return results


def _is_available(state: WorldState, employee_id: str, on_date: date) -> bool:
    """Check simple HR availability without exposing roster-edit operations."""
    employee = next((item for item in state.employees if item.employee_id == employee_id), None)
    if employee is None or on_date.weekday() in employee.fixed_days_off:
        return False
    return not any(
        absence.employee_id == employee_id
        and absence.start_date <= on_date <= absence.end_date
        for absence in state.absences
    )