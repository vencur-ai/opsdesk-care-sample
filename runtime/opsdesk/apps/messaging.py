"""Staff inbox and rule-based messaging tools."""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import Field, model_validator

from opsdesk.apps.base import AppTool, ToolArgs, ToolError, to_json_data
from opsdesk.apps.ids import next_monotonic_id
from opsdesk.core.models import Message, ShiftType
from opsdesk.core.state import WorldState
from opsdesk.sim.staff_model import decide_shift_request

MANAGER_ID = "manager"


class ReadInboxArgs(ToolArgs):
    """Inbox filtering options."""

    unread_only: bool = True


class SendMessageArgs(ToolArgs):
    """Message payload; a shift is a request only when request_intent is true."""

    recipient_id: str = Field(min_length=1, max_length=128)
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=4000)
    request_intent: bool = False
    shift_date: date | None = None
    shift_type: ShiftType | None = None
    ward_id: str | None = None

    @model_validator(mode="after")
    def validate_shift_request(self) -> SendMessageArgs:
        """Require a complete date, shift, and ward for shift requests."""
        has_date = self.shift_date is not None
        has_shift = self.shift_type is not None
        has_ward = self.ward_id is not None
        if any((has_date, has_shift, has_ward)) and not all((has_date, has_shift, has_ward)):
            raise ValueError("shift_date, shift_type, and ward_id must be supplied together")
        if self.request_intent and not all((has_date, has_shift, has_ward)):
            raise ValueError(
                "request_intent requires shift_date, shift_type, and ward_id"
            )
        return self


class ReadInboxTool(AppTool):
    """Read manager-facing messages and mark returned messages read."""

    app = "messaging"
    name = "messaging.read_inbox"
    description = "Read staff messages addressed to the manager; returned messages are marked read."
    args_model = ReadInboxArgs

    def execute(self, state: WorldState, args: ReadInboxArgs) -> list[dict[str, Any]]:
        messages = [
            item
            for item in state.messages
            if item.recipient_id == MANAGER_ID and (not args.unread_only or not item.is_read)
        ]
        messages.sort(key=lambda item: (item.sent_on, item.message_id))
        for message in messages:
            message.is_read = True
        return [to_json_data(item) for item in messages]


class SendMessageTool(AppTool):
    """Send a message and receive a deterministic simulated staff reply."""

    app = "messaging"
    name = "messaging.send"
    description = "Message one staff member; shift requests receive a deterministic rule-based reply."
    args_model = SendMessageArgs

    def execute(self, state: WorldState, args: SendMessageArgs) -> dict[str, Any]:
        employee = next(
            (item for item in state.employees if item.employee_id == args.recipient_id),
            None,
        )
        if employee is None:
            raise ToolError("Employee not found.")
        outgoing = Message(
            message_id=_next_message_id(state),
            sender_id=MANAGER_ID,
            recipient_id=employee.employee_id,
            subject=args.subject,
            body=args.body,
            sent_on=state.current_date,
        )
        state.messages.append(outgoing)

        if (
            args.request_intent
            and args.shift_date is not None
            and args.shift_type is not None
            and args.ward_id is not None
        ):
            decision = decide_shift_request(
                state,
                employee.employee_id,
                args.shift_date,
                args.shift_type,
                args.ward_id,
                assignment_id=f"message-check-{outgoing.message_id}",
            )
            reply_text = (
                f"I accept the {args.shift_type.value} shift on {args.shift_date.isoformat()}."
                if decision.accepted
                else f"I cannot accept that shift: {decision.reason}"
            )
            accepted = decision.accepted
        elif args.shift_date is not None:
            return {
                "sent_message": to_json_data(outgoing),
                "staff_reply": None,
                "accepted_shift_request": None,
            }
        else:
            reply_text = "Message received."
            accepted = None

        reply = Message(
            message_id=_next_message_id(state),
            sender_id=employee.employee_id,
            recipient_id=MANAGER_ID,
            subject=f"Re: {args.subject}",
            body=reply_text,
            sent_on=state.current_date,
        )
        state.messages.append(reply)
        return {
            "sent_message": to_json_data(outgoing),
            "staff_reply": to_json_data(reply),
            "accepted_shift_request": accepted,
        }


def _next_message_id(state: WorldState) -> str:
    """Allocate a deterministic manager-message ID."""
    return next_monotonic_id("message", (item.message_id for item in state.messages))