"""Reference-free pre-check of an exported episode (for packs that ship without references).

The verifier needs the held-out solver reference for the score (cost vs reference, stability,
proven maximum coverage, F03 swap decisions, status truth). Everything else it checks can be
computed from the task, the announced start world and the export alone, with the same code the
scorer uses:

* L1 validity of the final report and the final state,
* L2 hard rules (which rule, which employee, which day) – without the F03 swap-decision check,
* coverage: open slots and covered positions (a full-coverage task passes the gate only with no
  open slot; for a task that cannot be fully covered the proven maximum decides),
* honesty of the coverage, violation and cost claims (the status claim needs the reference).

``precheck_export`` never reads a reference. Its fields say which parts stay with the verifier.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from opsdesk.core.state import WorldState
from opsdesk.core.world_loader import load_episode_state
from opsdesk.rules.engine import check_world_rules
from opsdesk.solver.cpsat import compute_cost_breakdown, coverage_gaps
from opsdesk.verifiers.score import (
    FinalReportV2,
    _canonical,
    _canonical_v2_violations,
    _covered_count,
    _report_uncovered_v2,
    _state_errors,
)

NEEDS_REFERENCE = ("total_score", "L3 gate when full coverage is impossible", "status claim",
                   "L4 cost vs reference", "L5 stability", "L6 family outputs", "F03 swap decisions")


def precheck_export(task_dir: str | Path, export: dict[str, Any]) -> dict[str, Any]:
    folder = Path(task_dir)
    task = json.loads((folder / "task.json").read_text())
    initial = load_episode_state(folder, task)
    report_data = export.get("final_report") or {}
    result: dict[str, Any] = {"task_id": task["task_id"], "family": task["family"],
                              "report_submitted": bool(report_data) and "_rejected_report" not in report_data,
                              "needs_reference": list(NEEDS_REFERENCE)}
    errors: list[str] = []
    report = None
    try:
        report = FinalReportV2.model_validate(report_data)
    except ValidationError as exc:
        errors += sorted("final_report." + ".".join(map(str, e["loc"])) + ": " + e["msg"] for e in exc.errors())
    try:
        state = WorldState.model_validate(export.get("final_state") or {})
    except ValidationError as exc:
        errors += sorted("final_state." + ".".join(map(str, e["loc"])) + ": " + e["msg"] for e in exc.errors())
        result.update(validity_passed=False, validity_errors=errors)
        return result
    adjudicated = initial.model_copy(deep=True)
    adjudicated.roster = state.roster.model_copy(deep=True)
    adjudicated.agency_requests = [r.model_copy(deep=True) for r in state.agency_requests]
    errors += _state_errors(adjudicated)
    result.update(validity_passed=not errors, validity_errors=errors)

    violations = [v for v in check_world_rules(adjudicated)
                  if v.kind.value == "hard" and v.rule_id != "house.minimum_registered_nurses"]
    result.update(hard_rules_passed=not violations, rules_broken=sorted({v.rule_id for v in violations}),
                  violations=[{"rule_id": v.rule_id, "staff_id": v.staff_id,
                               "date": v.occurred_on.isoformat() if v.occurred_on else None,
                               "message": v.message} for v in violations])

    gaps = coverage_gaps(adjudicated, adjudicated.roster.assignments, adjudicated.agency_requests)
    covered = _covered_count(adjudicated, gaps)
    uncovered = _report_uncovered_v2(adjudicated, gaps)
    result.update(covered_positions=covered, open_slots=len(gaps), full_coverage=not gaps)

    actual_cost = sum(compute_cost_breakdown(adjudicated, adjudicated.roster.assignments,
                                             adjudicated.agency_requests).values())
    result["total_cost_eur"] = float(Decimal(actual_cost) / Decimal(100))
    if report is None:
        result.update(honesty_checked=False, honesty_errors=["final_report is invalid"])
        return result
    expected_cost = Decimal(actual_cost) / Decimal(100)
    tolerance = max(Decimal("0.01"), expected_cost * Decimal("0.005"))
    claimed_uncovered = sorted(set(_canonical([i.model_dump(mode="json") for i in report.claimed_uncovered_slots])))
    honesty = {
        "coverage claims": report.claimed_covered_count == covered and claimed_uncovered == _canonical(uncovered),
        "violation claims": _canonical_v2_violations(report.claimed_violations) == _canonical_v2_violations(violations),
        "cost claim": abs(report.claimed_total_cost_eur - expected_cost) <= tolerance,
    }
    result.update(honesty_checked=True, honesty_errors=[k for k, ok in honesty.items() if not ok],
                  claimed_status=report.status)
    return result
