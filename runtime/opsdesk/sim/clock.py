"""Episode clock advancement with deterministic event dispatch."""

from __future__ import annotations

from datetime import date

from opsdesk.core.models import DomainModel, Field
from opsdesk.core.state import WorldState
from opsdesk.sim.events import EventOutcome, process_due_events


class ClockAdvanceResult(DomainModel):
    """Result of advancing the episode clock."""

    start_date: date
    end_date: date
    days_advanced: int = Field(ge=1)
    event_outcomes: list[EventOutcome]


def advance_simulation(state: WorldState, days: int = 1) -> ClockAdvanceResult:
    """Advance one or more days and process every event due along the way."""
    if days < 1:
        raise ValueError("days must be a positive integer")
    if state.sim_clock.day_index + days >= state.horizon_days:
        raise ValueError("cannot advance beyond the simulation horizon")

    initial_date = state.current_date
    outcomes = process_due_events(state)
    for _ in range(days):
        state.advance_clock()
        outcomes.extend(process_due_events(state))
    state.record_action("sim", "advance", {"days": days})
    return ClockAdvanceResult(
        start_date=initial_date,
        end_date=state.current_date,
        days_advanced=days,
        event_outcomes=outcomes,
    )