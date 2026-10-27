"""Deterministic world state, simulation clock, and replayable snapshots."""

from __future__ import annotations

import copy
import random
from datetime import date, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import Field, PrivateAttr, model_validator

from opsdesk.core.models import (
    Absence,
    ActionLogEntry,
    AgencyOffer,
    AgencyRequest,
    CouncilSubmission,
    DemandSlot,
    Document,
    DomainModel,
    Employee,
    Event,
    HouseRulePolicy,
    Jurisdiction,
    Message,
    Roster,
    SimulationClock,
    StaffPersona,
    TimeClockEntry,
    PayrollExport,
    Ward,
)

SNAPSHOT_VERSION = 1


class WorldState(DomainModel):
    """Complete mutable state for one care-home simulation episode."""

    home_id: str = Field(min_length=1)
    home_name: str = Field(min_length=1)
    jurisdiction: Jurisdiction
    seed: int
    start_date: date
    horizon_days: int = Field(default=30, ge=1, le=366)
    care_rest_exception_enabled: bool = False
    sim_clock: SimulationClock
    employees: list[Employee] = Field(default_factory=list)
    wards: list[Ward] = Field(default_factory=list)
    absences: list[Absence] = Field(default_factory=list)
    demand_slots: list[DemandSlot] = Field(default_factory=list)
    roster: Roster = Field(default_factory=Roster)
    retired_assignment_ids: set[str] = Field(default_factory=set, exclude=True)
    messages: list[Message] = Field(default_factory=list)
    documents: list[Document] = Field(default_factory=list)
    events: list[Event] = Field(default_factory=list)
    action_log: list[ActionLogEntry] = Field(default_factory=list)
    house_rule_policy: HouseRulePolicy = Field(default_factory=HouseRulePolicy)
    staff_personas: list[StaffPersona] = Field(default_factory=list)
    timeclock_entries: list[TimeClockEntry] = Field(default_factory=list)
    agency_offers: list[AgencyOffer] = Field(default_factory=list)
    agency_requests: list[AgencyRequest] = Field(default_factory=list)
    agency_budget: Decimal = Field(default=Decimal("0"), ge=0)
    payroll_exports: list[PayrollExport] = Field(default_factory=list)
    council_submissions: list[CouncilSubmission] = Field(default_factory=list)
    works_council_required: bool = False

    _rng: random.Random = PrivateAttr(default_factory=random.Random)

    @model_validator(mode="before")
    @classmethod
    def default_simulation_clock(cls, values: Any) -> Any:
        """Construct the required clock from the world's episode settings."""
        if isinstance(values, dict) and values.get("sim_clock") is None and "start_date" in values:
            return {
                **values,
                "sim_clock": SimulationClock(
                    start_date=values["start_date"],
                    horizon_days=values.get("horizon_days", 30),
                ),
            }
        return values

    @model_validator(mode="after")
    def initialize_and_validate_clock(self) -> WorldState:
        """Initialize the episode clock and reject mismatched clock settings."""
        if (
            self.sim_clock.start_date != self.start_date
            or self.sim_clock.horizon_days != self.horizon_days
        ):
            raise ValueError("sim_clock settings must match the world start date and horizon")

        collections = {
            "employee_id": [item.employee_id for item in self.employees],
            "ward_id": [item.ward_id for item in self.wards],
            "absence_id": [item.absence_id for item in self.absences],
            "slot_id": [item.slot_id for item in self.demand_slots],
            "assignment_id": [item.assignment_id for item in self.roster.assignments],
            "message_id": [item.message_id for item in self.messages],
            "document_id": [item.document_id for item in self.documents],
            "event_id": [item.event_id for item in self.events],
            "persona employee_id": [item.employee_id for item in self.staff_personas],
            "timeclock entry_id": [item.entry_id for item in self.timeclock_entries],
            "agency offer_id": [item.offer_id for item in self.agency_offers],
            "agency request_id": [item.request_id for item in self.agency_requests],
            "payroll export_id": [item.export_id for item in self.payroll_exports],
            "council submission_id": [item.submission_id for item in self.council_submissions],
        }
        for label, identifiers in collections.items():
            if len(identifiers) != len(set(identifiers)):
                raise ValueError(f"duplicate {label} values are not allowed")
        return self

    def model_post_init(self, context: Any) -> None:
        """Initialize the private random generator from this world's seed."""
        self._rng.seed(self.seed)

    @property
    def current_date(self) -> date:
        """Return the current date in the simulation."""
        return self.sim_clock.current_date

    def advance_clock(self, days: int = 1) -> date:
        """Advance simulated time without reading the machine clock."""
        return self.sim_clock.advance(days)

    def record_action(self, tool: str, action: str, args: dict[str, Any] | None = None) -> None:
        """Append an action with a stable sequence and simulation-day index."""
        self.action_log.append(
            ActionLogEntry(
                sequence=len(self.action_log),
                tool=tool,
                action=action,
                args=copy.deepcopy(args or {}),
                sim_day_index=self.sim_clock.day_index,
            )
        )

    def random_int(self, lower: int, upper: int) -> int:
        """Draw an inclusive integer from this episode's seeded generator."""
        if lower > upper:
            raise ValueError("lower must not be greater than upper")
        return self._rng.randint(lower, upper)

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-compatible snapshot including private RNG state."""
        return {
            "version": SNAPSHOT_VERSION,
            "world": _json_safe(self.model_dump(mode="python")),
            "rng_state": _json_safe(self._rng.getstate()),
        }

    @classmethod
    def restore(cls, snapshot: dict[str, Any]) -> WorldState:
        """Restore a world and its random stream from a versioned snapshot."""
        if snapshot.get("version") != SNAPSHOT_VERSION:
            raise ValueError(f"unsupported snapshot version: {snapshot.get('version')!r}")
        if "world" not in snapshot or "rng_state" not in snapshot:
            raise ValueError("snapshot must contain world and rng_state")
        world = cls.model_validate(snapshot["world"])
        world._rng.setstate(_tuplify(snapshot["rng_state"]))
        return world


def _json_safe(value: Any) -> Any:
    """Convert state values to deterministic JSON primitives."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (date, Decimal)):
        return value.isoformat() if isinstance(value, date) else str(value)
    if isinstance(value, dict):
        return {
            str(key.value if isinstance(key, Enum) else key): _json_safe(item)
            for key, item in sorted(
                value.items(),
                key=lambda pair: str(pair[0].value if isinstance(pair[0], Enum) else pair[0]),
            )
        }
    if isinstance(value, (set, frozenset)):
        return sorted((_json_safe(item) for item in value), key=lambda item: str(item))
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _tuplify(value: Any) -> Any:
    """Rebuild tuples in Python's random state after JSON serialization."""
    if isinstance(value, list):
        return tuple(_tuplify(item) for item in value)
    return value