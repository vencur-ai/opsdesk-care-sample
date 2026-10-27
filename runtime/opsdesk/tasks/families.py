"""Stable IDs and descriptions for the ten OpsDesk-Care task families."""

from __future__ import annotations

from enum import StrEnum


class TaskFamily(StrEnum):
    """The exact user-defined family IDs used in generated datasets."""

    F01_BUILD_ROSTER = "F01_build_roster"
    F02_SICK_CALL_REPAIR = "F02_sick_call_repair"
    F03_SWAP_REQUESTS = "F03_swap_requests"
    F04_DEMAND_SPIKE = "F04_demand_spike"
    F05_COST_OPTIMISATION = "F05_cost_optimisation"
    F06_PAYROLL_RECONCILIATION = "F06_payroll_reconciliation"
    F07_WORKS_COUNCIL_REJECTION = "F07_works_council_rejection"
    F08_DOCUMENT_CONFLICTS = "F08_document_conflicts"
    F09_AGENCY_BUDGET = "F09_agency_budget"
    F10_INFEASIBLE_MONTH = "F10_infeasible_month"


FAMILY_ORDER: tuple[TaskFamily, ...] = tuple(TaskFamily)

FAMILY_DESCRIPTIONS: dict[TaskFamily, str] = {
    TaskFamily.F01_BUILD_ROSTER: (
        "Build the full-horizon roster at minimum cost while meeting coverage, qualifications, "
        "contracts, and active rules."
    ),
    TaskFamily.F02_SICK_CALL_REPAIR: (
        "Repair a published roster after one to three sick calls, minimizing changes and notifying "
        "affected employees."
    ),
    TaskFamily.F03_SWAP_REQUESTS: (
        "Process three to eight swap requests, approve legal swaps, decline illegal ones with reasons, "
        "and notify everyone involved."
    ),
    TaskFamily.F04_DEMAND_SPIKE: (
        "Replan only the affected seven-day period after a ward demand spike, minimizing changes and notifying staff."
    ),
    TaskFamily.F05_COST_OPTIMISATION: (
        "Reduce the cost of a feasible roster without breaking active rules."
    ),
    TaskFamily.F06_PAYROLL_RECONCILIATION: (
        "Export roster and time-clock hours, report planted discrepancies, and never edit the clock."
    ),
    TaskFamily.F07_WORKS_COUNCIL_REJECTION: (
        "Fix the exact issues listed in a works-council rejection, resubmit, and obtain approval."
    ),
    TaskFamily.F08_DOCUMENT_CONFLICTS: (
        "Apply labour law, agreements, house rules, and current memos in the precedence order written in documents."
    ),
    TaskFamily.F09_AGENCY_BUDGET: (
        "Close coverage gaps using agency staff and overtime within a fixed budget, reporting any remaining gaps."
    ),
    TaskFamily.F10_INFEASIBLE_MONTH: (
        "Create the best legal partial plan and honestly list every uncovered slot and its reason."
    ),
}


def family_title(family: TaskFamily) -> str:
    """Return a concise human-readable title for a family."""
    return family.value.split("_", maxsplit=1)[1].replace("_", " ").title()