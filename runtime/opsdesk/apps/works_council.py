"""Rule-based works-council submissions."""

from __future__ import annotations

from typing import Any

from opsdesk.apps.base import AppTool, ToolArgs, to_json_data
from opsdesk.apps.ids import next_monotonic_id
from opsdesk.core.models import CouncilStatus, CouncilSubmission
from opsdesk.core.state import WorldState
from opsdesk.rules.engine import check_world_rules
from opsdesk.apps.roster import roster_fingerprint


class SubmitRosterArgs(ToolArgs):
    """Empty works-council submission request."""


class SubmitRosterTool(AppTool):
    """Approve or reject a roster using deterministic active-rule checks."""

    app = "works_council"
    name = "works_council.submit"
    description = "Submit the current roster; the council returns a reproducible approval or rejection with reasons."
    args_model = SubmitRosterArgs

    def execute(self, state: WorldState, args: SubmitRosterArgs) -> dict[str, Any]:
        reasons = sorted(
            {
                f"{item.rule_id}: {item.message}"
                for item in check_world_rules(state)
                if item.kind.value == "hard"
            }
        )
        submission = CouncilSubmission(
            submission_id=_next_submission_id(state),
            submitted_on=state.current_date,
            status=CouncilStatus.REJECTED if reasons else CouncilStatus.APPROVED,
            reasons=reasons,
            roster_fingerprint=roster_fingerprint(state),
        )
        state.council_submissions.append(submission)
        return to_json_data(submission)


def _next_submission_id(state: WorldState) -> str:
    """Allocate a council submission ID without assuming contiguous IDs."""
    return next_monotonic_id(
        "council", (item.submission_id for item in state.council_submissions)
    )