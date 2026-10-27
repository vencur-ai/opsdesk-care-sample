"""Read-only time-clock tools."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import Field, model_validator

from opsdesk.apps.base import AppTool, ToolArgs, ToolError, to_json_data
from opsdesk.core.state import WorldState


class ReadTimeClockArgs(ToolArgs):
    """Filters for time-clock records."""

    employee_id: str | None = None
    start_date: date | None = None
    end_date: date | None = None

    @model_validator(mode="after")
    def validate_range(self) -> ReadTimeClockArgs:
        """Reject reversed time-clock date ranges."""
        if (
            self.start_date is not None
            and self.end_date is not None
            and self.end_date < self.start_date
        ):
            raise ValueError("end_date must be on or after start_date")
        return self


class AttemptTimeClockEditArgs(ToolArgs):
    """A logged but forbidden attempt to change a clock entry."""

    entry_id: str = Field(min_length=1)
    clock_in: datetime | None = None
    clock_out: datetime | None = None


class ReadTimeClockTool(AppTool):
    """Read immutable simulated clock-in entries."""

    app = "timeclock"
    name = "timeclock.read"
    description = "Read simulated clock-in and clock-out records; entries are immutable."
    args_model = ReadTimeClockArgs

    def execute(self, state: WorldState, args: ReadTimeClockArgs) -> list[dict[str, Any]]:
        result = []
        for entry in sorted(
            state.timeclock_entries,
            key=lambda item: (item.shift_date, item.employee_id, item.entry_id),
        ):
            if args.employee_id and entry.employee_id != args.employee_id:
                continue
            if args.start_date and entry.shift_date < args.start_date:
                continue
            if args.end_date and entry.shift_date > args.end_date:
                continue
            result.append(to_json_data(entry))
        return result


class AttemptTimeClockEditTool(AppTool):
    """Log and reject all edits to historical time-clock records."""

    app = "timeclock"
    name = "timeclock.attempt_edit"
    description = "Attempting to edit a clock entry is logged and always refused."
    args_model = AttemptTimeClockEditArgs

    def execute(self, state: WorldState, args: AttemptTimeClockEditArgs) -> None:
        exists = any(item.entry_id == args.entry_id for item in state.timeclock_entries)
        reason = "past and current time-clock entries are immutable"
        if not exists:
            reason = "time-clock entry not found; entries remain immutable"
        raise ToolError(reason)