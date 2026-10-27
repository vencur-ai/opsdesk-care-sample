# Bring your own agent

Run your agent on the 3 tasks of this pack and get one report per task: whether the
gates passed, which rule was broken, whether the final report was honest, and the score.

## 1. Start the episode server

As in `README.md` (venv) or `QUICKSTART.md` (Docker). The examples use
`http://127.0.0.1:8000` and host key `change-me`.

## 2. Connect your agent – pick one

**A. OpenAI-compatible endpoint** (vLLM, TGI, LiteLLM, any `/v1/chat/completions` with tools):

```sh
export OPENAI_API_KEY=...        # only if your endpoint needs one
python byo_agent.py run --agent openai --api-base http://localhost:8001/v1 --model my-model --host-key change-me
```

`byo_agent.py` runs the tool loop itself (at most 150 model turns per task, `--max-turns`): it sends
the task instruction, offers every tool as a function (dots in tool names become `__`), executes the
calls on the episode server and stops when `final_report_submit` is accepted. Your endpoint's costs
are yours; this script sends nothing anywhere else.

**B. Your own agent over HTTP or MCP:**

```sh
python byo_agent.py run --agent command --cmd "python my_agent.py" --host-key change-me
```

For every task the script resets the episode and starts your command with:

| Variable | Meaning |
|---|---|
| `OPSDESK_API_URL` | episode server |
| `OPSDESK_EPISODE_ID` | the episode to work on |
| `OPSDESK_AGENT_KEY` | send as header `X-OpsDesk-Agent-Key` |
| `OPSDESK_TASK_ID` | public task id |
| `OPSDESK_RESET_FILE` | JSON with the reset answer: `observation` (task instruction, home) and `tools` (this task's schemas, including the required `family_outputs`) |

- HTTP: read the instruction and the tools from `OPSDESK_RESET_FILE`, then
  `POST /step {"episode_id", "tool", "args"}` with the agent-key header; finish with the tool
  `final_report_submit`.
- MCP: let your MCP client start `python -m opsdesk.mcp_server` (with `runtime/` on `PYTHONPATH`).
  With `OPSDESK_EPISODE_ID` set it attaches to that episode, so your agent calls the tools directly
  and finishes with `opsdesk_submit` (no `opsdesk_reset` needed). Give the agent the instruction
  from `OPSDESK_RESET_FILE`.

**C. Plumbing check without any model:**

```sh
python byo_agent.py run --agent mock --host-key change-me
# or through the OpenAI path, with a fake endpoint that changes nothing:
python examples/mock_openai_endpoint.py --port 8001 &
python byo_agent.py run --agent openai --api-base http://127.0.0.1:8001/v1 --model mock --host-key change-me
```

## 3. Read the report

`byo_results/report.md` (and `report.json`) has one row per task.

| Column | Source |
|---|---|
| Report accepted, Valid (L1) | your final report and final state pass the schema and validity checks |
| Hard rules (L2), Rules broken | working-time and house rules broken by the final roster (rule id, employee, day) |
| Open slots | demand positions left uncovered |
| Honesty (pre-check) | your coverage, violation and cost claims compared with the truth |
| Gates passed, Honesty (verifier), Score | the full verifier with the reference (included for the 3 sample tasks) |

The pre-check uses the same verifier code as the score but no solver reference. Some parts need the
reference: the score, whether a task can be fully covered, the status claim, and the swap decisions.

## 4. Get the score

In this sample the episode server holds the references of its 3 tasks: `byo_agent.py run` fetches the
verifier score of every episode (`GET /score`) into `byo_results/scores/`, and the report shows it.
In a paid evaluation pack the references stay with Vencur: send `byo_results/exports/` (one `/export`
per episode, with a hash chain) to info@getvencur.com and you get `scores.json` back. Then:

```sh
python byo_agent.py report --results byo_results --scores scores.json
```

`examples/byo_example_report.md` shows a finished report for the do-nothing mock agent, including
the scores Vencur returned. It proves that the plumbing works; a do-nothing agent solves nothing.
