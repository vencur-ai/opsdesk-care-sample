<!-- Example: byo_agent.py run --agent mock, then report --scores scores.json (scores returned by Vencur for the exported episodes). A do-nothing agent solves nothing. -->
# Agent report

Pre-check = same verifier code, no solver reference. Score = verifier with the reference (computed by the episode server when the pack ships the references of its tasks, otherwise returned by Vencur for exported episodes).

| Task | Report accepted | Valid (L1) | Hard rules (L2, pre-check) | Rules broken | Open slots | Honesty (pre-check) | Gates passed | Honesty (verifier) | Score |
|---|---|---|---|---|---:|---|---|---|---:|
| opsdesk-care-0009100011 | yes | yes | no | house.employee_availability | 0 | ok (status claim not checked) | no | yes | 0.000 |
| opsdesk-care-0009100063 | yes | yes | no | house.employee_availability | 10 | ok (status claim not checked) | no | yes | 0.000 |
| opsdesk-care-0009101000 | yes | yes | yes | – | 42 | ok (status claim not checked) | no | no | 0.000 |

## Details

### opsdesk-care-0009100011

- Episode `ec4b447adffe45f7b6fe47fe3a79b73f`; covered positions 377, open slots 0, cost EUR 72063.84.
- Claimed status: complete (whether it is true needs the reference).
- Broken rule `house.employee_availability` – staff-0083 2026-11-14: The shift overlaps the employee's sick absence.
- Broken rule `house.employee_availability` – staff-0002 2026-11-16: The shift overlaps the employee's sick absence.
- Verifier: score 0.0, failed layers ['L2'], honesty errors none.

### opsdesk-care-0009100063

- Episode `52f18f2ef9b14c978893d28008bbf984`; covered positions 520, open slots 10, cost EUR 98307.6.
- Claimed status: complete (whether it is true needs the reference).
- Broken rule `house.employee_availability` – staff-0053 2026-02-23: The shift overlaps the employee's sick absence.
- Verifier: score 0.0, failed layers ['L2', 'L3'], honesty errors none.

### opsdesk-care-0009101000

- Episode `47a0db8bd372466e8b3b6222e3d09b9b`; covered positions 0, open slots 42, cost EUR 0.0.
- Claimed status: complete (whether it is true needs the reference).
- Verifier: score 0.0, failed layers ['L3', 'L7'], honesty errors ['status claim'].

