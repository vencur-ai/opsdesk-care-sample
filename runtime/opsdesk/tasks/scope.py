"""Which coverage gaps at episode start does a task's instruction ask the agent to close?

The coverage gate (L3) checks the whole horizon. A gap that exists when the episode
starts is only fair if the instruction covers it: a full-horizon build, the stated
time window, the announced sick calls or demand spikes, an explicitly named gap,
or a gap listed by date, ward and shift. The generator names every other start gap
in the instruction; the QA gate fails a task that still has one.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from opsdesk.core.state import WorldState
from opsdesk.solver.cpsat import CoverageGap, coverage_gaps

NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
                "nine": 9, "ten": 10, "fourteen": 14}
FULL_SCOPE = re.compile(r"full[- ]horizon|full roster|blank board|whole (month|horizon)|entire (month|horizon)|"
                        r"most complete legal roster", re.IGNORECASE)
NAMED_GAPS = re.compile(r"coverage gap|open slot|uncovered", re.IGNORECASE)


def gap_label(gap: CoverageGap) -> str:
    return f"{gap.date.isoformat()} {gap.ward_id} {gap.shift_type.value}"


def instruction_window(instruction: str, start: date, horizon: int) -> tuple[date, date] | None:
    match = re.search(r"next (\d+|" + "|".join(NUMBER_WORDS) + r") days", instruction, re.IGNORECASE)
    if not match:
        return None
    token = match.group(1).lower()
    days = int(token) if token.isdigit() else NUMBER_WORDS[token]
    return start, min(start + timedelta(days=days - 1), start + timedelta(days=horizon - 1))


def unexplained_start_gaps(instruction: str, family: str, state: WorldState) -> tuple[list[CoverageGap], dict[str, Any]]:
    """Start gaps the instruction does not cover, plus a description of the scope used."""
    gaps = coverage_gaps(state, state.roster.assignments, state.agency_requests)
    full = bool(FULL_SCOPE.search(instruction)) or family == "F10_infeasible_month"
    window = instruction_window(instruction, state.current_date, state.horizon_days)
    sick = "sick" in instruction.lower()
    spike = bool(re.search(r"demand increased|increased demand|demand spike", instruction, re.IGNORECASE))
    named = bool(NAMED_GAPS.search(instruction))
    sick_dates = {a.start_date + timedelta(days=i) for a in state.absences
                  if str(getattr(a.absence_type, "value", a.absence_type)) == "sick"
                  for i in range((a.end_date - a.start_date).days + 1)}
    spike_slots = {sid for e in state.events if str(getattr(e.event_type, "value", e.event_type)) == "demand_spike"
                   for sid in (e.payload.get("slot_ids") or [e.payload.get("slot_id")]) if sid}
    unexplained = []
    for gap in gaps:
        if gap_label(gap) in instruction:
            continue  # listed explicitly
        if window and not window[0] <= gap.date <= window[1]:
            unexplained.append(gap)
            continue
        if full or named:
            continue
        if sick and gap.date in sick_dates:
            continue
        if spike and gap.slot_id in spike_slots:
            continue
        unexplained.append(gap)
    scope = {"start_gaps": len(gaps), "full_horizon": full, "window": window, "sick": sick, "spike": spike,
             "named": named}
    return unexplained, scope


def name_open_gaps(gaps: list[CoverageGap]) -> str:
    """Instruction sentence that lists start gaps outside the task's own scope."""
    if not gaps:
        return ""
    labels = sorted({gap_label(g) for g in gaps})
    return (" These positions were already open before this request and must also be covered where the "
            "rules allow (date, ward, shift): " + "; ".join(labels) + ".")
