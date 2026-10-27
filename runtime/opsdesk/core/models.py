"""Typed domain models for the OpsDesk-Care simulation."""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class DomainModel(BaseModel):
    """Base model that rejects accidental fields and validates updates."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class Jurisdiction(StrEnum):
    """Supported legal jurisdictions."""

    UK = "uk"
    DE = "de"


class Qualification(StrEnum):
    """Staff qualifications recognized by the simulator."""

    RN = "registered_nurse"
    CA = "care_assistant"
    TL = "team_lead"


class EmploymentType(StrEnum):
    """Contract categories used by household policies."""

    FULL_TIME = "full_time"
    PART_TIME = "part_time"


class ShiftType(StrEnum):
    """Shift patterns available to a care home."""

    EARLY = "early"
    LATE = "late"
    NIGHT = "night"
    LONG_DAY = "long_day"


class AbsenceType(StrEnum):
    """Reasons an employee is unavailable."""

    HOLIDAY = "holiday"
    SICK = "sick"
    OTHER = "other"


class EventType(StrEnum):
    """Simulation events that may occur during an episode."""

    SICK_CALL = "sick_call"
    SWAP_REQUEST = "swap_request"
    DEMAND_SPIKE = "demand_spike"
    AGENCY_CANCELLATION = "agency_cancellation"
    CUSTOM = "custom"


class AgencyRequestStatus(StrEnum):
    """Lifecycle status for an agency-staffing request."""

    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"


class CouncilStatus(StrEnum):
    """Works-council decision for a roster submission."""

    APPROVED = "approved"
    REJECTED = "rejected"


class Contract(DomainModel):
    """An employee's contracted weekly hours and employment category."""

    employment_type: EmploymentType
    hours_per_week: float = Field(gt=0, le=168)


class Employee(DomainModel):
    """A synthetic care-home employee and their scheduling constraints."""

    employee_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    qualifications: set[Qualification] = Field(min_length=1)
    contract: Contract
    hourly_rate: Decimal = Field(gt=0)
    max_nights_per_month: int = Field(default=31, ge=0, le=31)
    fixed_days_off: set[int] = Field(default_factory=set)
    night_only: bool = False
    is_night_worker: bool = False
    weekly_hours_opt_out: bool = False

    @field_validator("fixed_days_off")
    @classmethod
    def validate_fixed_days_off(cls, value: set[int]) -> set[int]:
        """Restrict weekday numbers to Python's Monday-zero convention."""
        if any(day < 0 or day > 6 for day in value):
            raise ValueError("fixed_days_off values must be between 0 (Monday) and 6 (Sunday)")
        return value


class ShiftDefinition(DomainModel):
    """Clock times and scheduled break length for a shift type."""

    shift_type: ShiftType
    start_time: time
    end_time: time
    break_minutes: int = Field(ge=0)


class Ward(DomainModel):
    """A care-home ward."""

    ward_id: str = Field(min_length=1)
    name: str = Field(min_length=1)


class Absence(DomainModel):
    """An employee absence over an inclusive date range."""

    absence_id: str = Field(min_length=1)
    employee_id: str = Field(min_length=1)
    absence_type: AbsenceType
    start_date: date
    end_date: date

    @model_validator(mode="after")
    def validate_date_range(self) -> Absence:
        """Ensure the absence end date does not precede its start date."""
        if self.end_date < self.start_date:
            raise ValueError("end_date must be on or after start_date")
        return self


class DemandSlot(DomainModel):
    """Minimum staffing demand for one ward, date, and shift."""

    slot_id: str = Field(min_length=1)
    date: date
    ward_id: str = Field(min_length=1)
    shift_type: ShiftType
    min_headcount: int = Field(ge=0)
    min_qualifications: dict[Qualification, int] = Field(default_factory=dict)

    @field_validator("min_qualifications")
    @classmethod
    def validate_qualification_counts(
        cls, value: dict[Qualification, int]
    ) -> dict[Qualification, int]:
        """Reject negative qualification requirements."""
        if any(count < 0 for count in value.values()):
            raise ValueError("qualification demand counts cannot be negative")
        return value


class Assignment(DomainModel):
    """One employee's rostered shift."""

    assignment_id: str = Field(min_length=1)
    employee_id: str = Field(min_length=1)
    date: date
    ward_id: str = Field(min_length=1)
    shift_type: ShiftType
    break_minutes: int | None = Field(default=None, ge=0, le=720)


class Roster(DomainModel):
    """The current roster state."""

    assignments: list[Assignment] = Field(default_factory=list)
    published: bool = False


class Message(DomainModel):
    """A simulated staff message."""

    message_id: str = Field(min_length=1)
    sender_id: str = Field(min_length=1)
    recipient_id: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    body: str
    sent_on: date
    is_read: bool = False


class Document(DomainModel):
    """A policy or operational document visible in the simulation."""

    document_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    body: str
    source: str = Field(min_length=1)
    effective_from: date
    effective_until: date | None = None

    @model_validator(mode="after")
    def validate_effective_range(self) -> Document:
        """Ensure an optional end date does not predate the effective date."""
        if self.effective_until is not None and self.effective_until < self.effective_from:
            raise ValueError("effective_until must be on or after effective_from")
        return self


class Event(DomainModel):
    """A scheduled event in the simulation."""

    event_id: str = Field(min_length=1)
    event_type: EventType
    occurs_on: date
    payload: dict[str, Any] = Field(default_factory=dict)
    processed: bool = False


class StaffPersona(DomainModel):
    """Deterministic response preferences for a synthetic staff member."""

    employee_id: str = Field(min_length=1)
    preferred_shift_types: set[ShiftType] = Field(default_factory=set)
    never_sundays: bool = False
    accepts_extra_shifts: bool = True
    accepts_swaps: bool = True


class TimeClockEntry(DomainModel):
    """Read-only synthetic clock record for one employee and shift date."""

    entry_id: str = Field(min_length=1)
    employee_id: str = Field(min_length=1)
    shift_date: date
    clock_in: datetime
    clock_out: datetime
    source: str = Field(default="simulated", min_length=1)

    @model_validator(mode="after")
    def validate_clock_interval(self) -> TimeClockEntry:
        """Require a positive, timezone-naive simulated interval."""
        if self.clock_in.tzinfo is not None or self.clock_out.tzinfo is not None:
            raise ValueError("simulated clock entries must use timezone-naive local times")
        if self.clock_out <= self.clock_in:
            raise ValueError("clock_out must be after clock_in")
        return self


class AgencyOffer(DomainModel):
    """An available synthetic temporary-staff offer."""

    offer_id: str = Field(min_length=1)
    agency_id: str = Field(min_length=1)
    agency_name: str = Field(min_length=1)
    qualification: Qualification
    available_from: date
    available_until: date
    hourly_rate: Decimal = Field(gt=0)
    max_staff: int = Field(ge=1)
    lead_time_days: int = Field(default=0, ge=0, le=30)

    @model_validator(mode="after")
    def validate_availability(self) -> AgencyOffer:
        """Ensure the offer end date does not precede its start date."""
        if self.available_until < self.available_from:
            raise ValueError("available_until must be on or after available_from")
        return self


class AgencyRequest(DomainModel):
    """A recorded request for temporary staff."""

    request_id: str = Field(min_length=1)
    offer_id: str = Field(min_length=1)
    agency_id: str = Field(min_length=1)
    requested_for: date
    ward_id: str = Field(min_length=1)
    shift_type: ShiftType
    qualification: Qualification
    staff_count: int = Field(ge=1)
    estimated_cost: Decimal = Field(gt=0)
    status: AgencyRequestStatus = AgencyRequestStatus.CONFIRMED


class PayrollRow(DomainModel):
    """Roster and clock-hour totals for one employee."""

    employee_id: str = Field(min_length=1)
    rostered_minutes: int = Field(ge=0)
    clocked_minutes: int = Field(ge=0)
    payable_minutes: int = Field(ge=0)
    discrepancy_minutes: int


class PayrollExport(DomainModel):
    """Immutable point-in-time payroll export."""

    export_id: str = Field(min_length=1)
    start_date: date
    end_date: date
    created_on: date
    rows: list[PayrollRow] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_export_range(self) -> PayrollExport:
        """Ensure the export date range is ordered."""
        if self.end_date < self.start_date:
            raise ValueError("end_date must be on or after start_date")
        return self


class CouncilSubmission(DomainModel):
    """Works-council decision and rule-based rejection reasons."""

    submission_id: str = Field(min_length=1)
    submitted_on: date
    status: CouncilStatus
    reasons: list[str] = Field(default_factory=list)
    roster_fingerprint: str = Field(min_length=1)


class HouseRulePolicy(DomainModel):
    """Home-specific operational constraints used by the house-rule pack."""

    policy_id: str = Field(default="house-default-v1", min_length=1)
    max_consecutive_nights: int = Field(default=3, ge=1, le=14)
    min_registered_nurses_per_shift: int = Field(default=1, ge=0, le=20)
    forbid_late_to_early: bool = True
    max_consecutive_work_days: int = Field(default=5, ge=1, le=31)
    part_time_hours_margin_percent: float = Field(default=10.0, ge=0, le=100)


class ActionLogEntry(DomainModel):
    """A deterministic record of an agent action."""

    sequence: int = Field(ge=0)
    tool: str = Field(min_length=1)
    action: str = Field(min_length=1)
    args: dict[str, Any] = Field(default_factory=dict)
    sim_day_index: int = Field(ge=0)


class SimulationClock(DomainModel):
    """Episode-relative clock; it never consults wall-clock time."""

    start_date: date
    horizon_days: int = Field(default=30, ge=1, le=366)
    day_index: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_day_index(self) -> SimulationClock:
        """Keep the clock inside its configured episode."""
        if self.day_index >= self.horizon_days:
            raise ValueError("day_index must be less than horizon_days")
        return self

    @property
    def current_date(self) -> date:
        """Return the current simulated date."""
        from datetime import timedelta

        return self.start_date + timedelta(days=self.day_index)

    def advance(self, days: int = 1) -> date:
        """Advance the simulated clock and return its new date."""
        if days < 1:
            raise ValueError("days must be a positive integer")
        next_index = self.day_index + days
        if next_index >= self.horizon_days:
            raise ValueError("cannot advance beyond the simulation horizon")
        self.day_index = next_index
        return self.current_date