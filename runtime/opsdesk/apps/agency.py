"""Offline temporary-staff marketplace tools."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from pydantic import Field

from opsdesk.apps.base import AppTool, ToolArgs, ToolError, to_json_data
from opsdesk.apps.ids import next_monotonic_id
from opsdesk.core.models import (
    AgencyRequest,
    AgencyRequestStatus,
    Qualification,
    ShiftType,
)
from opsdesk.core.state import WorldState
from opsdesk.core.time_utils import shift_work_minutes
from opsdesk.core.models import Assignment


class ListAgencyOffersArgs(ToolArgs):
    """Availability filters for agency offers."""

    shift_date: date
    qualification: Qualification | None = None


class RequestAgencyStaffArgs(ToolArgs):
    """Temporary-staff request details."""

    offer_id: str = Field(min_length=1)
    requested_for: date
    ward_id: str = Field(min_length=1)
    shift_type: ShiftType
    staff_count: int = Field(default=1, ge=1, le=50)


class ListAgencyOffersTool(AppTool):
    """List matching offers in the local synthetic marketplace."""

    app = "agency"
    name = "agency.list_offers"
    description = "List available synthetic temp-staff offers by date and optional qualification."
    args_model = ListAgencyOffersArgs

    def execute(self, state: WorldState, args: ListAgencyOffersArgs) -> list[dict[str, Any]]:
        offers = []
        for offer in sorted(state.agency_offers, key=lambda item: (item.hourly_rate, item.offer_id)):
            if not offer.available_from <= args.shift_date <= offer.available_until:
                continue
            if args.qualification and offer.qualification != args.qualification:
                continue
            offers.append(to_json_data(offer))
        return offers


class RequestAgencyStaffTool(AppTool):
    """Book temporary staff subject to lead time and budget."""

    app = "agency"
    name = "agency.request"
    description = "Request temp staff for a shift; availability, lead time, capacity, and home budget are enforced."
    args_model = RequestAgencyStaffArgs

    def execute(self, state: WorldState, args: RequestAgencyStaffArgs) -> dict[str, Any]:
        offer = next((item for item in state.agency_offers if item.offer_id == args.offer_id), None)
        if offer is None:
            raise ToolError("Agency offer not found.")
        if not any(item.ward_id == args.ward_id for item in state.wards):
            raise ToolError("Ward not found.")
        if not state.start_date <= args.requested_for < state.start_date + timedelta(
            days=state.horizon_days
        ):
            raise ToolError("Requested date is outside this episode.")
        if args.staff_count > offer.max_staff:
            raise ToolError("Requested staff count exceeds the offer capacity.")
        already_requested = sum(
            item.staff_count
            for item in state.agency_requests
            if item.status == AgencyRequestStatus.CONFIRMED
            and item.offer_id == offer.offer_id
            and item.requested_for == args.requested_for
            and item.ward_id == args.ward_id
            and item.shift_type == args.shift_type
        )
        if already_requested + args.staff_count > offer.max_staff:
            raise ToolError(
                "Cumulative requests for this offer and shift exceed its capacity."
            )
        earliest = state.current_date + timedelta(days=offer.lead_time_days)
        if args.requested_for < earliest:
            raise ToolError(f"Offer lead time requires a date on or after {earliest.isoformat()}.")
        if not offer.available_from <= args.requested_for <= offer.available_until:
            raise ToolError("Agency offer is not available on the requested date.")

        shift = Assignment(
            assignment_id="agency-cost-calculation",
            employee_id="agency",
            date=args.requested_for,
            ward_id=args.ward_id,
            shift_type=args.shift_type,
        )
        hours = Decimal(shift_work_minutes(shift)) / Decimal(60)
        cost = (offer.hourly_rate * hours * args.staff_count).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )
        committed = sum(
            (
                request.estimated_cost
                for request in state.agency_requests
                if request.status == AgencyRequestStatus.CONFIRMED
            ),
            start=Decimal("0"),
        )
        if committed + cost > state.agency_budget:
            raise ToolError(
                f"Request costs {cost}; only {state.agency_budget - committed} remains in the agency budget."
            )

        request = AgencyRequest(
            request_id=_next_request_id(state),
            offer_id=offer.offer_id,
            agency_id=offer.agency_id,
            requested_for=args.requested_for,
            ward_id=args.ward_id,
            shift_type=args.shift_type,
            qualification=offer.qualification,
            staff_count=args.staff_count,
            estimated_cost=cost,
            status=AgencyRequestStatus.CONFIRMED,
        )
        state.agency_requests.append(request)
        return {
            "request": to_json_data(request),
            "remaining_budget": str(state.agency_budget - committed - cost),
        }


def _next_request_id(state: WorldState) -> str:
    """Allocate a deterministic agency request ID."""
    return next_monotonic_id(
        "agency-request", (item.request_id for item in state.agency_requests)
    )