"""Deterministic event scheduling and event application."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from opsdesk.core.models import (
    Absence,
    AbsenceType,
    AgencyRequestStatus,
    Event,
    EventType,
    Message,
    Qualification,
    DomainModel,
    Field,
)
from opsdesk.core.state import WorldState


class EventScheduleCounts(DomainModel):
    """Requested event counts for deterministic scenario scheduling."""

    sick_calls: int = Field(default=1, ge=0, le=100)
    swap_requests: int = Field(default=1, ge=0, le=100)
    demand_spikes: int = Field(default=1, ge=0, le=100)
    agency_cancellations: int = Field(default=1, ge=0, le=100)


class EventOutcome(DomainModel):
    """Recorded result for a processed event."""

    event_id: str
    event_type: EventType
    outcome: str
    details: dict[str, Any] = Field(default_factory=dict)


def generate_event_schedule(
    state: WorldState,
    counts: EventScheduleCounts | None = None,
) -> list[Event]:
    """Generate reproducible event records using only the world's seeded RNG."""
    plan = counts or EventScheduleCounts()
    employees = sorted(state.employees, key=lambda item: item.employee_id)
    assignments = sorted(state.roster.assignments, key=lambda item: item.assignment_id)
    demand_slots = sorted(
        state.demand_slots,
        key=lambda item: (item.date, item.slot_id),
    )
    agency_offers = sorted(state.agency_offers, key=lambda item: item.offer_id)
    scheduled: list[Event] = []

    candidates: list[tuple[EventType, list[dict[str, Any]], int]] = [
        (
            EventType.SICK_CALL,
            [{"employee_id": item.employee_id} for item in employees],
            plan.sick_calls,
        ),
        (
            EventType.SWAP_REQUEST,
            [
                {"assignment_id": item.assignment_id, "requester_id": item.employee_id}
                for item in assignments
            ],
            plan.swap_requests,
        ),
        (
            EventType.DEMAND_SPIKE,
            [{"slot_id": item.slot_id, "delta": 1} for item in demand_slots],
            plan.demand_spikes,
        ),
        (
            EventType.AGENCY_CANCELLATION,
            [{"agency_id": item.agency_id} for item in agency_offers],
            plan.agency_cancellations,
        ),
    ]
    first_future_offset = max(0, state.sim_clock.day_index + 1)
    last_offset = state.horizon_days - 1
    if first_future_offset > last_offset:
        return scheduled

    for event_type, options, count in candidates:
        if not options or count == 0:
            continue
        for _ in range(count):
            option_index = state.random_int(0, len(options) - 1)
            offset = state.random_int(first_future_offset, last_offset)
            event = Event(
                event_id=_next_event_id(state),
                event_type=event_type,
                occurs_on=state.start_date + timedelta(days=offset),
                payload=options[option_index],
            )
            state.events.append(event)
            scheduled.append(event)
    return scheduled


def process_due_events(state: WorldState) -> list[EventOutcome]:
    """Apply due, unprocessed events in stable date and ID order."""
    due = sorted(
        (
            event
            for event in state.events
            if not event.processed and event.occurs_on <= state.current_date
        ),
        key=lambda item: (item.occurs_on, item.event_id),
    )
    outcomes: list[EventOutcome] = []
    for event in due:
        outcome = _apply_event(state, event)
        event.processed = True
        state.record_action(
            "events",
            event.event_type.value,
            {"event_id": event.event_id, "outcome": outcome.outcome},
        )
        outcomes.append(outcome)
    return outcomes


def _apply_event(state: WorldState, event: Event) -> EventOutcome:
    """Apply one event and return an explicit outcome."""
    payload = event.payload
    if event.event_type == EventType.SICK_CALL:
        employee_id = str(payload.get("employee_id", ""))
        if not any(item.employee_id == employee_id for item in state.employees):
            return _outcome(event, "ignored", reason="employee_not_found")
        absence_id = f"absence-{event.event_id}"
        if not any(item.absence_id == absence_id for item in state.absences):
            end_date = _parse_date(payload.get("end_date", event.occurs_on))
            state.absences.append(
                Absence(
                    absence_id=absence_id,
                    employee_id=employee_id,
                    absence_type=AbsenceType.SICK,
                    start_date=event.occurs_on,
                    end_date=max(event.occurs_on, end_date),
                )
            )
        return _outcome(event, "applied", employee_id=employee_id, absence_id=absence_id)

    if event.event_type == EventType.SWAP_REQUEST:
        assignment_id = str(payload.get("assignment_id", ""))
        assignment = next(
            (item for item in state.roster.assignments if item.assignment_id == assignment_id),
            None,
        )
        requester_id = str(payload.get("requester_id", ""))
        if assignment is None or not any(item.employee_id == requester_id for item in state.employees):
            return _outcome(event, "ignored", reason="assignment_or_requester_not_found")
        target_assignment_id = str(payload.get("target_assignment_id", ""))
        target_assignment = next(
            (
                item
                for item in state.roster.assignments
                if item.assignment_id == target_assignment_id
            ),
            None,
        )
        request_text = (
            f"Could you exchange assignment {assignment_id} with assignment "
            f"{target_assignment_id}?"
            if target_assignment is not None
            else f"Please arrange cover or a swap for assignment {assignment_id}."
        )
        message_id = f"message-event-{event.event_id}"
        if not any(item.message_id == message_id for item in state.messages):
            state.messages.append(
                Message(
                    message_id=message_id,
                    sender_id=requester_id,
                    recipient_id="manager",
                    subject="Shift swap request",
                    body=request_text,
                    sent_on=event.occurs_on,
                )
            )
        return _outcome(
            event,
            "applied",
            message_id=message_id,
            assignment_id=assignment_id,
            target_assignment_id=target_assignment_id or None,
        )

    if event.event_type == EventType.DEMAND_SPIKE:
        raw_slot_ids = payload.get("slot_ids")
        if raw_slot_ids is None:
            raw_slot_ids = [payload.get("slot_id", "")]
        if not isinstance(raw_slot_ids, list):
            return _outcome(event, "ignored", reason="slot_ids_must_be_a_list")
        requested_ids = sorted({str(item) for item in raw_slot_ids if item})
        slots_by_id = {item.slot_id: item for item in state.demand_slots}
        target_slots = [slots_by_id[item] for item in requested_ids if item in slots_by_id]
        if not target_slots:
            return _outcome(event, "ignored", reason="demand_slot_not_found")
        delta = int(payload.get("delta", 1))
        if delta < 1:
            return _outcome(event, "ignored", reason="delta_must_be_positive")
        qualification_value = payload.get("qualification")
        qualification = None
        if qualification_value is not None:
            try:
                qualification = Qualification(str(qualification_value))
            except ValueError:
                return _outcome(event, "ignored", reason="unknown_qualification")
        for slot in target_slots:
            slot.min_headcount += delta
            if qualification_value is not None:
                updated_qualifications = dict(slot.min_qualifications)
                updated_qualifications[qualification] = (
                    updated_qualifications.get(qualification, 0) + delta
                )
                slot.min_qualifications = updated_qualifications
        return _outcome(
            event,
            "applied",
            slot_ids=[item.slot_id for item in target_slots],
            added_headcount=delta,
        )

    if event.event_type == EventType.AGENCY_CANCELLATION:
        agency_id = str(payload.get("agency_id", ""))
        cancelled_ids: list[str] = []
        for request in sorted(state.agency_requests, key=lambda item: item.request_id):
            if (
                request.agency_id == agency_id
                and request.status == AgencyRequestStatus.CONFIRMED
                and request.requested_for >= event.occurs_on
            ):
                request.status = AgencyRequestStatus.CANCELLED
                cancelled_ids.append(request.request_id)
        return _outcome(event, "applied", agency_id=agency_id, cancelled_request_ids=cancelled_ids)

    return _outcome(event, "ignored", reason="no_handler_for_event_type")


def _outcome(event: Event, result: str, **details: Any) -> EventOutcome:
    """Create an event outcome with stable identifiers."""
    return EventOutcome(
        event_id=event.event_id,
        event_type=event.event_type,
        outcome=result,
        details=details,
    )


def _parse_date(value: Any) -> date:
    """Parse an ISO date from event payload data."""
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value)
    raise ValueError("event end_date must be an ISO date")


def _next_event_id(state: WorldState) -> str:
    """Allocate an event ID that cannot collide with imported scenario data."""
    existing = {item.event_id for item in state.events}
    index = len(existing) + 1
    while f"event-{index:04d}" in existing:
        index += 1
    return f"event-{index:04d}"


def announce_sick_calls(state: WorldState) -> int:
    """Make every scheduled future event known at episode start (v2.4.12).

    The runtime exposes no clock tool, while references and the verifier project
    events to the horizon. Sick calls and demand changes are therefore announced
    up front: the absence or demand change is recorded for its real dates and a
    message lands in the manager inbox.
    """
    count = 0
    for event in sorted(state.events, key=lambda item: (item.occurs_on, item.event_id)):
        if event.processed:
            continue
        outcome = _apply_event(state, event)
        event.processed = True
        state.record_action("events", event.event_type.value,
                            {"event_id": event.event_id, "outcome": outcome.outcome, "announced": True})
        if outcome.outcome != "applied":
            continue
        message_id = f"message-event-{event.event_id}"
        if any(item.message_id == message_id for item in state.messages):
            continue
        if event.event_type == EventType.SICK_CALL:
            employee_id = str(event.payload.get("employee_id", ""))
            end_date = _parse_date(event.payload.get("end_date", event.occurs_on))
            span = event.occurs_on.isoformat() if end_date <= event.occurs_on else (
                f"{event.occurs_on.isoformat()} to {end_date.isoformat()}")
            state.messages.append(Message(
                message_id=message_id, sender_id=employee_id, recipient_id="manager",
                subject="Sick call", body=f"I am sick and cannot work my shifts on {span}.",
                sent_on=state.current_date))
        elif event.event_type == EventType.DEMAND_SPIKE:
            slots = {item.slot_id: item for item in state.demand_slots}
            hit = sorted((slots[i] for i in outcome.details.get("slot_ids", []) if i in slots),
                         key=lambda x: (x.date, x.ward_id, x.shift_type.value))
            listed = ", ".join(f"{x.date.isoformat()} {x.ward_id} {x.shift_type.value}" for x in hit)
            qual = event.payload.get("qualification")
            extra = f" ({qual})" if qual else ""
            state.messages.append(Message(
                message_id=message_id, sender_id="clinical-lead", recipient_id="manager",
                subject="Demand increase",
                body=(f"Resident acuity has risen. Each of these shifts now needs "
                      f"{outcome.details.get('added_headcount', 1)} more staff{extra}: {listed}."),
                sent_on=state.current_date))
        count += 1
    return count
