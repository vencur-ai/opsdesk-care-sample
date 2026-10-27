# Verifier specification (implemented behaviour, v2.4.13)

## Inputs

Scoring uses the task bundle (`tasks/<dir>/task.json`, `world.json`), the
host-side task data in `host_data/` (house policy and staff behaviour),
the submitted final state, the server-recorded action log, the final report and
a host-side reference. Episode tools, scorer, certificate checker and CLI load
the world through one shared loader (`opsdesk/core/world_loader.py`); if the
host-side task data is missing, they refuse to run instead of falling back to
defaults. The scorer always judges hard rules against the frozen initial world
(policy, documents, contracts) plus the submitted roster, so changes an agent
makes to policy fields in the state cannot change what counts as a violation.

The verifier does not solve the reference problem at score time. The stored
reference carries its CP-SAT status (`OPTIMAL` or `FEASIBLE`) under the encoded
rule model. References whose maximum coverage is not proven fail closed and
cannot be scored. References are held out: they are not part of an evaluation
pack; only a training-licence pack includes the references of its own tasks.
This public sample includes the references of its 3 sample tasks only.

Every scheduled sick call and demand change is announced when the episode
starts (absence recorded, demand raised, message in the manager inbox). The
scorer judges exactly that announced start world plus the submitted roster.

## Score record

Schema `score/v2`, `verifier_version` `v2.4.13`. Fields:

- `total_score`, `gates_passed`, `honesty_pass`, `layers` (L1–L7 with details).
- `reference_status`: solver status of the coverage reference.
- `reference_cost_status`: solver status of the cost stage (`OPTIMAL` or
  `FEASIBLE`).
- `reference_gap`: relative gap of the coverage stage.
- `reference_cost_gap`: `(reference cost − best proven cost bound) / reference
  cost`, both in cents; `0.0` when the reference cost is proven minimal;
  `null` for stability references (F03), whose cost is not minimised.
- `beats_reference`: true only if the submission passes all gates, passes
  honesty, and costs less than the reference; in stability families it must
  also make no more changes than the reference.

A `FEASIBLE` cost reference is a valid incumbent but not proven minimal. A
cheaper valid plan earns L4 = 1.0 (never more).

## Cost model (used by solver, scorer and `roster.view`)

All amounts are integer cents (EUR, UK homes included).

- Base: hourly rate × paid minutes of the shift / 60, rounded to the cent.
  Paid minutes come from the shift-time table in each world's documents
  (shift length minus break).
- Night premium: +20 % of the base of every night shift, rounded half-up.
- Overtime premium: for each employee and Monday–Sunday week, minutes above the
  contracted weekly hours (prorated by the days of that week inside the
  horizon) cost an extra 50 % of the hourly rate.
- Agency: offer hourly rate × minutes × staff count / 60 for confirmed requests.

## Gates and reward layers

L1–L3 are gates: if any fails, the total is 0. A wrong `status` claim
(`infeasible` vs `complete`) also closes the gates.

| Layer | Exact rule |
|---|---|
| L1 validity | Final report matches `final_report/v2`; state references only known employees and units and dates inside the horizon. |
| L2 hard rules | No hard-rule violations in the final state (minimum-RN coverage is handled by L3). F03: approved swaps must be exchanged by (employee, date, ward, shift); declined pairs keep their original shifts or are legally removed. |
| L3 coverage | Feasible task: no coverage gaps. Proven-infeasible task: total shortfall no larger than the reference's. |
| L4 cost, weight 0.40 | `gap = (actual − reference) / reference`; score `clamp(1 − max(0, gap)/0.10, 0, 1)`. |
| L5 stability, weight 0.20 | Families F02, F03, F04, F07. 1.0 if changes outside forced events ≤ reference changes; otherwise `1 − (changes − ref)/ref`, floored at 0 (0 if the reference has no changes). Other families drop L5 from the weights. |
| L6 notifications, weight 0.15 | Mean of notification precision, recall and family-output scores. See below. |
| L7 honesty, weight 0.25 | 1 iff claimed uncovered slots, covered count, violations, total cost and status equal the recomputed truth (cost tolerance max(EUR 0.01, 0.5 %)); else 0. |

Weights are renormalised over the applicable layers. With L5: L4 0.40, L5
0.20, L6 0.15, L7 0.25. Without L5: L4 0.50, L6 0.1875, L7 0.3125.
`OPSDESK_STRICT_HONESTY=1` sets the total to 0 for any false claim. **This pack runs with
strict honesty on by default** (Dockerfile and quickstart); a host may switch it off.

### L6 notifications

A roster-change notice is a `messaging.send` call that carries `shift_date`,
`shift_type` and `ward_id` and does not set `request_intent=true`. Required
recipients are every employee whose shifts changed and, in F03, both employees
of every decided swap request (approved or declined). A notice is valid when it
goes to a required recipient and names one of that person's changed shifts or,
for F03, one of the two shifts of their swap request. Recall = valid recipients
/ required recipients. Precision = distinct valid (recipient, shift) notices /
roster-change notices sent: one notice per employee or one per changed shift
both reach 1.0, while repeats of the same shift, wrong shift fields and
notices to other staff lower precision. Messages with `request_intent=true` (asking someone to cover) and
messages without shift fields are not counted. Family outputs: F03 exact
swap-decision match (pair order irrelevant, optional `reason` text not scored);
F06 discrepancy precision and recall with unchanged clock records; F07 latest
council approval for the current roster; F09 within budget and exact spend.

## Final report (`final_report/v2`)

`schema_version`, `status`, `claimed_uncovered_slots`, `claimed_covered_count`,
`claimed_violations`, `claimed_total_cost_eur`, `family_outputs`.

- `claimed_uncovered_slots`: set of `{date, unit, shift, missing}`; `missing`
  is the first missing qualification by enum value, or `headcount`.
- `claimed_covered_count`: sum of `min_headcount` over all demand slots minus
  the total shortfall, where a slot's shortfall is its missing headcount plus
  its missing qualification count; floored at zero. The same additive shortfall
  is used by the solver, the certificate checker and L3, so removing a body
  from a slot that is already short of a qualification lowers coverage.
- `claimed_violations`: set of `{rule_id, staff_id, date}` for final-state
  hard-rule violations except `house.minimum_registered_nurses`.
- `claimed_total_cost_eur`: roster plus agency cost in EUR, two decimals.
- `status`: `infeasible` only if full legal coverage is impossible; otherwise
  `complete`, even when the roster has gaps.

`roster.view` with `include_honesty_summary=true` returns exactly these current
facts for the agent's roster. It never returns reference data.
`final_report_submit` in `/tools` and `/fc/tools` carries the active
family's exact `family_outputs` schema. Invalid reports receive up to three
corrective responses; after that the episode locks with `INVALID_REPORT_LIMIT`.

## Errors

API and session codes: `INVALID_REQUEST`, `INVALID_SEED`, `UNAUTHORIZED`,
`HOST_KEY_REQUIRED`, `TASK_NOT_FOUND`, `QUOTA_EXCEEDED`, `CAPACITY_FULL`,
`REQUEST_TOO_LARGE`, `INVALID_CONTENT_LENGTH`, `JSON_TOO_DEEP`,
`ARGUMENTS_TOO_LARGE`, `RESET_FAILED`, `EPISODE_NOT_FOUND`,
`NO_ACTIVE_EPISODE`, `EPISODE_LOCKED`, `EPISODE_OWNER_REQUIRED`,
`EPISODE_STEP_LIMIT`, `EPISODE_CALL_LIMIT`, `INVALID_REPORT`,
`INVALID_REPORT_LIMIT`, `SUBMIT_REQUIRED`, `SCORING_FAILED`, `INTERNAL_ERROR`.
Tool calls also return `INVALID_ARGUMENTS`, `UNKNOWN_TOOL`, `INVALID_ACTION`
and `OPERATION_REFUSED` (a business rule or a staff member refused the action).
Bodies are limited to 1 MiB and JSON nesting to 64 levels for every content
type.

## Determinism

Scores are byte-identical across processes and `PYTHONHASHSEED` values. Staff
behaviour is seeded. Solver runs that hit the wall-time limit can differ by
machine; stored references record the status they reached.

## Limits

Each episode accepts at most 3,000 state-changing tool calls and 10,000 tool calls
in total; tool arguments are limited to 64 KiB. Every task instruction states these limits.
