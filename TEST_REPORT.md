# Test report

OpsDesk-Care 2.4.13. Every number below comes from a stored run (scripted agents at pack build; models via `opsdesk motor eval`, whose episode records, transcripts and exported final states are kept by Vencur). Fully solved = all gates passed, honesty check passed and score >= 0.999.

## Functional proof (scripted agents, not models)

Two scripted agents ran through the episode API of exactly these packed tasks and were scored by the verifier with strict honesty. Gold replays the solver reference through the public tools and must reach 1.0; no-op changes nothing, reports honestly and must score 0 because the coverage gate fails. This shows that tasks, tools, report contract and verifier work end to end. It says nothing about how well any model performs.

| Task | Family | Agent | Tool calls | Gates | Honesty | Score | Failed layers |
|---|---|---|---:|---|---|---:|---|
| opsdesk-care-0009101000 | F01_build_roster (UK) | gold (replays the solver reference) | 214 | yes | yes | 1.000 | – |
| opsdesk-care-0009101000 | F01_build_roster (UK) | no-op (changes nothing, keeps the default status `complete` on this infeasible task) | 2 | no | no | 0.000 | L3, L7 |
| opsdesk-care-0009100011 | F02_sick_call_repair (UK) | gold (replays the solver reference) | 10 | yes | yes | 1.000 | – |
| opsdesk-care-0009100011 | F02_sick_call_repair (UK) | no-op (changes nothing, honest report) | 2 | no | yes | 0.000 | L2 |
| opsdesk-care-0009100063 | F04_demand_spike (DE) | gold (replays the solver reference) | 81 | yes | yes | 1.000 | – |
| opsdesk-care-0009100063 | F04_demand_spike (DE) | no-op (changes nothing, honest report) | 2 | no | yes | 0.000 | L2, L3 |

## No model results

This sample pack reports no model results and no success rates. Run your own agent with `byo_agent.py` (see `BRING_YOUR_OWN_AGENT.md`).
