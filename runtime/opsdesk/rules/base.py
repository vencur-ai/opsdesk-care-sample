"""Shared rule interfaces, violation models, and deterministic check helpers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from typing import ClassVar, Iterable

from pydantic import BaseModel, ConfigDict

from opsdesk.core.models import Assignment, DomainModel, Employee
from opsdesk.core.state import WorldState
from opsdesk.core.time_utils import horizon_end_exclusive, shift_interval


class RuleKind(StrEnum):
    """Whether a rule is mandatory or contributes a soft penalty."""

    HARD = "hard"
    SOFT = "soft"


class Violation(DomainModel):
    """A deterministic, structured rule-check result."""

    rule_id: str
    kind: RuleKind
    message: str
    entity_ids: tuple[str, ...] = ()
    staff_id: str = ""
    occurred_on: date | None = None


class Rule(ABC):
    """Interface implemented by every rule in a rule pack."""

    id: ClassVar[str]
    title: ClassVar[str]
    citation: ClassVar[str]
    text: ClassVar[str]
    kind: ClassVar[RuleKind] = RuleKind.HARD
    weight: ClassVar[float] = 1.0
    needs_expert_review: ClassVar[bool] = False

    @abstractmethod
    def check(self, state: WorldState) -> list[Violation]:
        """Return all violations of this rule in the supplied state."""


@dataclass(frozen=True)
class RulePack:
    """An immutable, ordered collection of checks."""

    id: str
    rules: tuple[Rule, ...]

    def __post_init__(self) -> None:
        ids = [rule.id for rule in self.rules]
        if len(ids) != len(set(ids)):
            raise ValueError(f"rule ids in pack {self.id!r} must be unique")


def check_rules(state: WorldState, rules: Iterable[Rule]) -> list[Violation]:
    """Check rules in stable order and sort violations for reproducible output."""
    violations: list[Violation] = []
    for rule in sorted(rules, key=lambda item: item.id):
        violations.extend(rule.check(state))
    return sorted(
        violations,
        key=lambda item: (
            item.rule_id,
            item.occurred_on.isoformat() if item.occurred_on else "",
            item.entity_ids,
            item.message,
        ),
    )


def make_violation(
    rule: Rule,
    message: str,
    *,
    entity_ids: Iterable[str] = (),
    staff_id: str | None = None,
    occurred_on: date | None = None,
) -> Violation:
    """Build a normalized violation for a rule."""
    original_entity_ids = tuple(entity_ids)
    return Violation(
        rule_id=rule.id,
        kind=rule.kind,
        message=message,
        entity_ids=tuple(sorted(set(original_entity_ids))),
        # Per-employee checks pass the employee first. Preserve that identity
        # before sorting the mixed employee/assignment/absence entity list.
        staff_id=staff_id if staff_id is not None else (
            original_entity_ids[0] if original_entity_ids else ""
        ),
        occurred_on=occurred_on,
    )


def employee_assignments(state: WorldState, employee_id: str) -> list[Assignment]:
    """Return an employee's assignments in stable chronological order."""
    return sorted(
        (
            assignment
            for assignment in state.roster.assignments
            if assignment.employee_id == employee_id
        ),
        key=lambda assignment: (
            shift_interval(assignment)[0],
            shift_interval(assignment)[1],
            assignment.assignment_id,
        ),
    )


def employee_map(state: WorldState) -> dict[str, Employee]:
    """Return employees keyed by ID in deterministic insertion order."""
    return {employee.employee_id: employee for employee in sorted(state.employees, key=lambda e: e.employee_id)}


def horizon_bounds(state: WorldState) -> tuple[datetime, datetime]:
    """Return the half-open local datetime interval covered by the episode."""
    start = datetime.combine(state.start_date, time.min)
    end = datetime.combine(horizon_end_exclusive(state.start_date, state.horizon_days), time.min)
    return start, end


def rest_gaps_in_window(
    assignments: Iterable[Assignment], start: datetime, end: datetime
) -> list[tuple[datetime, datetime]]:
    """Return unworked gaps inside a half-open time window."""
    intervals = sorted(
        (shift_interval(item) for item in assignments),
        key=lambda interval: (interval[0], interval[1]),
    )
    cursor = start
    gaps: list[tuple[datetime, datetime]] = []
    for shift_start, shift_end in intervals:
        if shift_end <= start or shift_start >= end:
            continue
        clipped_start = max(shift_start, start)
        clipped_end = min(shift_end, end)
        if clipped_start > cursor:
            gaps.append((cursor, clipped_start))
        cursor = max(cursor, clipped_end)
    if cursor < end:
        gaps.append((cursor, end))
    return gaps


def gap_minutes(gap: tuple[datetime, datetime]) -> int:
    """Return whole minutes in a rest gap."""
    return int((gap[1] - gap[0]).total_seconds() // 60)


def week_start(day: date) -> date:
    """Return the Monday for a date."""
    return day - timedelta(days=day.weekday())