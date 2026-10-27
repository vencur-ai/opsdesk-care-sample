"""Roster viewing and controlled assignment tools."""

from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta
from typing import Any

from pydantic import Field, model_validator

from opsdesk.apps.base import AppTool, ToolArgs, ToolError, to_json_data
from opsdesk.apps.ids import next_monotonic_id
from opsdesk.core.models import Assignment, ShiftType
from opsdesk.core.state import WorldState
from opsdesk.core.time_utils import shift_interval
from opsdesk.rules.engine import check_world_rules
from opsdesk.solver.cpsat import compute_cost_breakdown, coverage_gaps, coverage_position_count
from opsdesk.sim.staff_model import decide_shift_request


class ViewRosterArgs(ToolArgs):
    """Optional roster filters and an exact read-only honesty summary."""

    start_date: date | None = None
    end_date: date | None = None
    ward_id: str | None = None
    employee_id: str | None = None
    include_honesty_summary: bool = False
    summary_only: bool = False
    limit: int = Field(default=200, ge=1, le=500)
    offset: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_range(self) -> ViewRosterArgs:
        """Reject reversed roster ranges."""
        if (
            self.start_date is not None
            and self.end_date is not None
            and self.end_date < self.start_date
        ):
            raise ValueError("end_date must be on or after start_date")
        return self


class AssignShiftArgs(ToolArgs):
    """Arguments for adding one roster assignment."""

    employee_id: str = Field(min_length=1)
    date: date
    ward_id: str = Field(min_length=1)
    shift_type: ShiftType


class UnassignShiftArgs(ToolArgs):
    """Identifier for an assignment to remove."""

    assignment_id: str = Field(min_length=1)


class SwapShiftsArgs(ToolArgs):
    """Identifiers for two assignments whose employees should be exchanged."""

    first_assignment_id: str = Field(min_length=1)
    second_assignment_id: str = Field(min_length=1)


class PublishRosterArgs(ToolArgs):
    """Empty roster-publication request."""


class ViewRosterTool(AppTool):
    """Read the current roster."""

    app = "roster"
    name = "roster.view"
    description = (
        "View roster assignments, optionally filtered by date range or ward and paginated "
        "with limit (1-500) and offset. "
        "Set include_honesty_summary=true to receive exact current coverage gaps, "
        "filled demand-position count, hard-rule violations, total roster-plus-agency cost in EUR, "
        "all calculated from the current roster only."
    )
    args_model = ViewRosterArgs

    def execute(self, state: WorldState, args: ViewRosterArgs) -> Any:
        filtered = []
        for assignment in sorted(
            state.roster.assignments,
            key=lambda item: (item.date, item.shift_type.value, item.ward_id, item.assignment_id),
        ):
            if args.start_date and assignment.date < args.start_date:
                continue
            if args.end_date and assignment.date > args.end_date:
                continue
            if args.ward_id and assignment.ward_id != args.ward_id:
                continue
            if args.employee_id and assignment.employee_id != args.employee_id:
                continue
            filtered.append(to_json_data(assignment))
        total_count = len(filtered)
        result = filtered[args.offset:args.offset + args.limit]
        response = {
            "assignments": result,
            "pagination": {
                "limit": args.limit,
                "offset": args.offset,
                "total_count": total_count,
                "has_more": args.offset + len(result) < total_count,
            },
        }
        if not args.include_honesty_summary and not args.summary_only:
            return response
        gaps = coverage_gaps(state, state.roster.assignments, state.agency_requests)
        uncovered = [{
            "date": gap.date.isoformat(), "unit": gap.ward_id,
            "shift": gap.shift_type.value,
            "missing": (
                sorted(gap.missing_qualifications, key=lambda q: q.value)[0].value
                if gap.missing_qualifications else "headcount"
            ),
        } for gap in gaps]
        covered_count = coverage_position_count(state, gaps)
        violations = [{
            "rule_id": item.rule_id,
            "staff_id": item.staff_id,
            "date": item.occurred_on.isoformat() if item.occurred_on else None,
        } for item in check_world_rules(state)
            if item.kind.value == "hard"
            and item.rule_id != "house.minimum_registered_nurses"]
        total_cost_cents = sum(compute_cost_breakdown(
            state, state.roster.assignments, state.agency_requests
        ).values())
        summary = {
            "honesty_summary": {
                "claimed_uncovered_slots": uncovered,
                "claimed_covered_count": covered_count,
                "claimed_violations": violations,
                "claimed_total_cost_eur": round(total_cost_cents / 100, 2),
            },
        }
        if args.summary_only:
            return summary
        return {
            **response,
            **summary,
        }


class AssignShiftTool(AppTool):
    """Add a shift after mechanical checks, returning rule violations as warnings."""

    app = "roster"
    name = "roster.assign"
    description = "Assign an employee to a ward and shift; hard-rule issues are returned as warnings."
    args_model = AssignShiftArgs

    def execute(self, state: WorldState, args: AssignShiftArgs) -> dict[str, Any]:
        if not any(item.employee_id == args.employee_id for item in state.employees):
            raise ToolError("Employee not found.")
        if not any(item.ward_id == args.ward_id for item in state.wards):
            raise ToolError("Ward not found.")
        if not _inside_horizon(state, args.date):
            raise ToolError("Shift date is outside this episode.")

        assignment_id = _next_assignment_id(state)
        assignment = Assignment(
            assignment_id=assignment_id,
            employee_id=args.employee_id,
            date=args.date,
            ward_id=args.ward_id,
            shift_type=args.shift_type,
        )
        start, end = shift_interval(assignment)
        for existing in state.roster.assignments:
            if existing.employee_id != assignment.employee_id:
                continue
            existing_start, existing_end = shift_interval(existing)
            if start < existing_end and end > existing_start:
                raise ToolError("Employee already has an overlapping roster assignment.")

        before = _hard_rule_signatures(state)
        was_published = _ensure_editable(state)
        state.roster.assignments.append(assignment)
        warnings = _new_rule_warnings(state, before)
        return {
            "assignment": to_json_data(assignment),
            "warnings": warnings,
            "roster_unpublished": was_published,
        }


class UnassignShiftTool(AppTool):
    """Remove one roster assignment; a published roster becomes a draft."""

    app = "roster"
    name = "roster.unassign"
    description = "Remove one assignment by its exact ID; a published roster becomes a draft again."
    args_model = UnassignShiftArgs

    def execute(self, state: WorldState, args: UnassignShiftArgs) -> dict[str, Any]:
        assignment = next(
            (item for item in state.roster.assignments if item.assignment_id == args.assignment_id),
            None,
        )
        if assignment is None:
            raise ToolError("Assignment not found.")
        was_published = _ensure_editable(state)
        state.retired_assignment_ids.add(assignment.assignment_id)
        state.roster.assignments.remove(assignment)
        return {
            "removed_assignment_id": assignment.assignment_id,
            "roster_unpublished": was_published,
        }


class SwapShiftsTool(AppTool):
    """Exchange two employees after both simulated staff members accept."""

    app = "roster"
    name = "roster.swap"
    description = "Swap the assigned employees on two shifts if both staff personas accept the change."
    args_model = SwapShiftsArgs

    def execute(self, state: WorldState, args: SwapShiftsArgs) -> dict[str, Any]:
        if args.first_assignment_id == args.second_assignment_id:
            raise ToolError("Choose two different assignments.")
        by_id = {item.assignment_id: item for item in state.roster.assignments}
        first = by_id.get(args.first_assignment_id)
        second = by_id.get(args.second_assignment_id)
        if first is None or second is None:
            raise ToolError("One or both assignments were not found.")
        if first.employee_id == second.employee_id:
            raise ToolError("The two assignments already belong to the same employee.")
        ignored = (first.assignment_id, second.assignment_id)
        first_decision = decide_shift_request(
            state,
            second.employee_id,
            first.date,
            first.shift_type,
            first.ward_id,
            assignment_id=f"swap-check-{second.employee_id}-{first.assignment_id}",
            ignore_assignment_ids=ignored,
            is_swap=True,
        )
        if not first_decision.accepted:
            raise ToolError(f"{second.employee_id} declines the first shift: {first_decision.reason}")
        second_decision = decide_shift_request(
            state,
            first.employee_id,
            second.date,
            second.shift_type,
            second.ward_id,
            assignment_id=f"swap-check-{first.employee_id}-{second.assignment_id}",
            ignore_assignment_ids=ignored,
            is_swap=True,
        )
        if not second_decision.accepted:
            raise ToolError(f"{first.employee_id} declines the second shift: {second_decision.reason}")

        before = _hard_rule_signatures(state)
        was_published = _ensure_editable(state)
        first.employee_id, second.employee_id = second.employee_id, first.employee_id
        warnings = _new_rule_warnings(state, before)
        return {
            "first_assignment_id": first.assignment_id,
            "second_assignment_id": second.assignment_id,
            "warnings": warnings,
            "roster_unpublished": was_published,
        }


class PublishRosterTool(AppTool):
    """Publish a roster only after rule and works-council checks."""

    app = "roster"
    name = "roster.publish"
    description = "Publish the roster when hard rules pass and any required council approval is current."
    args_model = PublishRosterArgs

    def execute(self, state: WorldState, args: PublishRosterArgs) -> dict[str, Any]:
        if state.roster.published:
            raise ToolError("Roster is already published.")
        violations = check_world_rules(state)
        hard = [item for item in violations if item.kind.value == "hard"]
        if hard:
            raise ToolError(
                "Roster has hard-rule violations: "
                + ", ".join(sorted({item.rule_id for item in hard}))
            )
        if state.works_council_required:
            latest = state.council_submissions[-1] if state.council_submissions else None
            if latest is None or latest.status.value != "approved":
                raise ToolError("Works-council approval is required before publication.")
            if latest.roster_fingerprint != roster_fingerprint(state):
                raise ToolError("The roster changed after works-council approval; resubmit it.")
        state.roster.published = True
        return {
            "published": True,
            "assignment_count": len(state.roster.assignments),
            "published_on": state.current_date.isoformat(),
        }


def roster_fingerprint(state: WorldState) -> str:
    """Return a stable digest for the current assignment set."""
    payload = [
        item.model_dump(mode="json")
        for item in sorted(state.roster.assignments, key=lambda item: item.assignment_id)
    ]
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _ensure_editable(state: WorldState) -> bool:
    """Turn a published roster into a draft before allowing a repair."""
    was_published = state.roster.published
    if was_published:
        state.roster.published = False
    return was_published


def _inside_horizon(state: WorldState, shift_date: date) -> bool:
    """Check that a date belongs to the simulated episode."""
    return state.start_date <= shift_date < state.start_date + timedelta(days=state.horizon_days)


def _next_assignment_id(state: WorldState) -> str:
    """Allocate a deterministic ID above all active and retired assignments."""
    return next_monotonic_id(
        "assignment",
        (
            *(item.assignment_id for item in state.roster.assignments),
            *state.retired_assignment_ids,
        ),
    )


def _hard_rule_signatures(state: WorldState) -> set[tuple]:
    """Capture current hard-rule violations to isolate warnings caused by an edit."""
    return {
        (item.rule_id, item.entity_ids, item.occurred_on, item.message)
        for item in check_world_rules(state)
        if item.kind.value == "hard"
    }


def _new_rule_warnings(state: WorldState, previous: set[tuple]) -> list[str]:
    """Return new hard-rule messages created by a roster change."""
    return sorted(
        {
            f"{item.rule_id}: {item.message}"
            for item in check_world_rules(state)
            if item.kind.value == "hard"
            and (item.rule_id, item.entity_ids, item.occurred_on, item.message) not in previous
        }
    )