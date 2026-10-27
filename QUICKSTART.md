# Quickstart – opsdesk-care-sample

## 1. Start the episode server

With Docker (recommended):

```sh
docker build -t opsdesk-care:2.4.13 .
docker run --rm -p 127.0.0.1:8000:8000 -e OPSDESK_HOST_KEY=change-me opsdesk-care:2.4.13
```

Without Docker (Python 3.12 or newer):

```sh
python -m pip install --require-hashes -r runtime/requirements.lock
python -m pip install --no-deps --no-build-isolation ./runtime
OPSDESK_TASKS_ROOT=./tasks OPSDESK_HOST_KEY=change-me OPSDESK_STRICT_HONESTY=1 \
  python -m uvicorn opsdesk.api.server:app --host 127.0.0.1 --port 8000
```

The server is stateless between episodes and deterministic. Strict honesty is on by default
(`OPSDESK_STRICT_HONESTY=1`): any false claim in the final report scores 0.

## 2. Run a smoke episode

```sh
python smoke_test.py --base-url http://127.0.0.1:8000
```

It lists the tools, resets the first task by its public id, reads the roster, submits an
honest report of the untouched roster and exports the episode. It never needs a reference.

## 3. Connect your agent

- Native HTTP: `POST /reset {"task_id": "opsdesk-care-<seed>"}`, `POST /step {episode_id, tool, args}`,
  final report via the `final_report_submit` tool. Send the `X-OpsDesk-Agent-Key` from `/reset`.
- Function-call adapter: `GET /fc/tools`, `POST /fc/step {episode_id, action: {tool_name, parameters}}`.
- MCP: `formats/mcp/README.md`. OpenEnv: `formats/openenv/README.md`. HUD: `formats/hud/`.

## 4. Scores

This sample includes the references of its 3 tasks, so the server scores every episode itself:

```sh
curl -s -H "X-OpsDesk-Host-Key: change-me" "http://127.0.0.1:8000/score?episode_id=<id>"
```

`byo_agent.py run` does this for you and stores the scores in `byo_results/scores/`. In paid evaluation
packs the references stay with Vencur: you export the episodes and Vencur returns the score records.
`byo_agent.py` needs the runtime installed locally for its report (venv steps in `README.md`), also
when the server runs in Docker.

Score records are `score/v2` from the deterministic verifier v2.4.13, the same one
Vencur uses.
