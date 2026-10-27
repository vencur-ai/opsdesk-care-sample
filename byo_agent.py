"""Test your own agent on the tasks of this pack and get one report per task.

    python byo_agent.py run --agent mock                                   # plumbing check, no model
    python byo_agent.py run --agent openai --api-base http://localhost:8001/v1 --model my-model
    python byo_agent.py run --agent anthropic --model claude-sonnet-5-5           # key from ANTHROPIC_API_KEY
    python byo_agent.py run --agent command --cmd "python my_agent.py"     # your agent speaks HTTP or MCP
    python byo_agent.py report --results byo_results [--scores scores.json]

Needs the episode server of this pack running (see README.md) and its host key for exports.
`run` resets every task, lets the agent work through the tools, exports each episode and writes
`byo_results/exports/<task>.json` (send this folder to Vencur for the score) plus `byo_results/report.md` and `report.json`.

If the server can score (a pack or sample that ships the references of its tasks), `run` also stores the
verifier score of every episode in `byo_results/scores/` and the report shows it directly.

The report has a pre-check that needs no solver reference (validity, broken hard rules, coverage,
honesty of coverage/violation/cost claims), computed by the same verifier code as the score. The
score itself needs the held-out reference: send the `exports/` folder to Vencur, who returns
`scores.json`; `report --scores scores.json` merges it into the same report.

Standard library only; the pre-check imports the verifier from `runtime/`. Nothing leaves your
machine except calls to the URLs you pass (episode server, your model endpoint).
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
SYSTEM_PROMPT = (
    "You are the staff-office agent of a care home. You work only through the tools. Use them to read "
    "the roster, staff records, policy documents, messages, agency offers, payroll and time-clock data, "
    "make the changes the task asks for, and notify affected staff. Finish by calling final_report_submit "
    "with truthful claims about the final roster; the episode ends when the report is accepted.")


# ----------------------------------------------------------------------------- episode server

class Server:
    def __init__(self, base_url: str, host_key: str | None):
        self.base = base_url.rstrip("/")
        self.host_key = host_key

    def call(self, method: str, path: str, body: Any = None, headers: dict[str, str] | None = None,
             timeout: float = 600) -> tuple[int, Any]:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.base + path, data=data, method=method,
                                         headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def reset(self, task: str) -> dict[str, Any]:
        status, body = self.call("POST", "/reset", {"task_id": task})
        if status != 200:
            raise SystemExit(f"reset {task} failed (HTTP {status}): {body}")
        return body

    def step(self, episode: dict[str, Any], tool: str, args: Any) -> dict[str, Any]:
        _, body = self.call("POST", "/step", {"episode_id": episode["episode_id"], "tool": tool, "args": args},
                            {"X-OpsDesk-Agent-Key": episode.get("agent_key") or ""})
        return body

    def export(self, episode_id: str) -> dict[str, Any]:
        if not self.host_key:
            raise SystemExit("--host-key (or OPSDESK_HOST_KEY) is needed to export episodes")
        status, body = self.call("GET", f"/export?episode_id={episode_id}", headers={"X-OpsDesk-Host-Key": self.host_key})
        if status != 200:
            raise SystemExit(f"export {episode_id} failed (HTTP {status}): {body}")
        return body

    def score(self, episode_id: str) -> dict[str, Any] | None:
        """Verifier score, or None when this server holds no reference for the task (buyer evaluation packs)."""
        status, body = self.call("GET", f"/score?episode_id={episode_id}", headers={"X-OpsDesk-Host-Key": self.host_key or ""})
        return body if status == 200 and "total_score" in body else None


def _submitted(result: dict[str, Any]) -> bool:
    return bool(result.get("accepted"))


# ----------------------------------------------------------------------------- agents

def agent_mock(server: Server, episode: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    """Changes nothing and reports the untouched roster truthfully (checks the plumbing, solves nothing)."""
    summary = server.step(episode, "roster.view", {"include_honesty_summary": True, "summary_only": True})
    submit = next(t for t in episode["tools"] if t["name"] == "final_report_submit")
    schema = submit["parameters"]["properties"]["family_outputs"]
    outputs = {n: ([] if schema["properties"][n].get("type") == "array" else 0) for n in schema.get("required", [])}
    report = {"schema_version": "final_report/v2", "status": "complete",
              **summary["data"]["honesty_summary"], "family_outputs": outputs}
    result = server.step(episode, "final_report_submit", report)
    return {"turns": 1, "tool_calls": 2, "submitted": _submitted(result)}


def _openai_tools(tools: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, str]]:
    by_name = {t["name"]: t for t in tools}
    names = {name.replace(".", "__"): name for name in by_name}  # function names may not contain dots
    return [{"type": "function", "function": {"name": safe, "description": by_name[name].get("description", ""),
                                              "parameters": by_name[name].get("parameters", {"type": "object"})}}
            for safe, name in names.items()], names


def agent_openai(server: Server, episode: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    """Runs the tool loop against any OpenAI-compatible /chat/completions endpoint."""
    tools, names = _openai_tools(episode["tools"])
    observation = episode["observation"]
    brief = {k: observation[k] for k in ("task_id", "home_name", "jurisdiction", "start_date", "horizon_days")
             if k in observation}
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"{observation.get('instruction', '')}\n\nHome: {json.dumps(brief, sort_keys=True)}"}]
    key = os.environ.get(args.api_key_env, "") if args.api_key_env else ""
    headers = {"Content-Type": "application/json", **({"Authorization": f"Bearer {key}"} if key else {})}
    turns = calls = 0
    submitted = False
    while turns < args.max_turns and not submitted:
        body = json.dumps({"model": args.model, "messages": messages, "tools": tools}).encode()
        request = urllib.request.Request(args.api_base.rstrip("/") + "/chat/completions", data=body,
                                         headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=args.timeout) as response:
            reply = json.loads(response.read())
        turns += 1
        message = reply["choices"][0]["message"]
        messages.append({k: v for k, v in message.items() if k in ("role", "content", "tool_calls")})
        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            messages.append({"role": "user", "content": "Continue with the tools and finish with final_report_submit."})
            continue
        for call in tool_calls:
            name = names.get(call["function"]["name"], call["function"]["name"])
            try:
                arguments = json.loads(call["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                arguments = {}
            result = server.step(episode, name, arguments)
            calls += 1
            submitted = submitted or (name == "final_report_submit" and _submitted(result))
            messages.append({"role": "tool", "tool_call_id": call["id"],
                             "content": json.dumps(result)[:args.max_tool_result_chars]})
    return {"turns": turns, "tool_calls": calls, "submitted": submitted}


def agent_anthropic(server: Server, episode: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    """Runs the tool loop against the Anthropic Messages API (key from the environment variable only)."""
    by_name = {t["name"]: t for t in episode["tools"]}
    names = {name.replace(".", "__"): name for name in by_name}
    tools = [{"name": safe, "description": by_name[name].get("description", ""),
              "input_schema": by_name[name].get("parameters", {"type": "object"})} for safe, name in names.items()]
    observation = episode["observation"]
    brief = {k: observation[k] for k in ("task_id", "home_name", "jurisdiction", "start_date", "horizon_days")
             if k in observation}
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": f"{observation.get('instruction', '')}\n\nHome: {json.dumps(brief, sort_keys=True)}"}]
    key = os.environ.get(args.api_key_env or "ANTHROPIC_API_KEY", "")
    if not key:
        raise SystemExit(f"set {args.api_key_env or 'ANTHROPIC_API_KEY'} in the environment")
    headers = {"Content-Type": "application/json", "x-api-key": key, "anthropic-version": "2023-06-01"}
    base = (args.api_base or "https://api.anthropic.com").rstrip("/")
    turns = calls = 0
    submitted = False
    while turns < args.max_turns and not submitted:
        body = json.dumps({"model": args.model, "max_tokens": args.max_output_tokens, "system": SYSTEM_PROMPT,
                           "messages": messages, "tools": tools}).encode()
        request = urllib.request.Request(base + "/v1/messages", data=body, headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=args.timeout) as response:
            reply = json.loads(response.read())
        turns += 1
        content = reply.get("content") or []
        messages.append({"role": "assistant", "content": content})
        uses = [c for c in content if c.get("type") == "tool_use"]
        if not uses:
            messages.append({"role": "user", "content": "Continue with the tools and finish with final_report_submit."})
            continue
        results = []
        for use in uses:
            name = names.get(use["name"], use["name"])
            result = server.step(episode, name, use.get("input") or {})
            calls += 1
            submitted = submitted or (name == "final_report_submit" and _submitted(result))
            results.append({"type": "tool_result", "tool_use_id": use["id"],
                            "content": json.dumps(result)[:args.max_tool_result_chars]})
        messages.append({"role": "user", "content": results})
    return {"turns": turns, "tool_calls": calls, "submitted": submitted}


def agent_command(server: Server, episode: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    """Runs your own agent process against the already reset episode (HTTP, or MCP via opsdesk.mcp_server)."""
    reset_file = args.results / "runs" / f"{episode['task']}.reset.json"
    reset_file.write_text(json.dumps({k: episode[k] for k in ("episode_id", "observation", "tools")}, indent=2))
    env = {**os.environ, "OPSDESK_API_URL": server.base, "OPSDESK_EPISODE_ID": episode["episode_id"],
           "OPSDESK_AGENT_KEY": episode.get("agent_key") or "", "OPSDESK_TASK_ID": episode["task"],
           "OPSDESK_RESET_FILE": str(reset_file.resolve())}
    started = time.monotonic()
    done = subprocess.run(shlex.split(args.cmd), env=env, timeout=args.timeout)
    return {"exit_code": done.returncode, "seconds": round(time.monotonic() - started, 1)}


AGENTS = {"mock": agent_mock, "openai": agent_openai, "anthropic": agent_anthropic, "command": agent_command}


# ----------------------------------------------------------------------------- report

def _precheck(task_dir: Path, export: dict[str, Any]) -> dict[str, Any]:
    sys.path.insert(0, str(HERE / "runtime"))
    from opsdesk.verifiers.precheck import precheck_export

    return precheck_export(task_dir, export)


def _task_dirs() -> dict[int, Path]:
    found = {}
    for task_file in sorted((HERE / "tasks").glob("*/task.json")):
        world = json.loads((task_file.parent / "world.json").read_text())
        found[world.get("seed")] = task_file.parent
    return found


def build_report(results: Path, scores: Path | None = None) -> dict[str, Any]:
    by_seed = _task_dirs()
    scored = {}
    for local in sorted((results / "scores").glob("*.json")):  # scored by the episode server itself
        row = json.loads(local.read_text())
        scored[row.get("episode_id")] = row
    if scores:
        for row in json.loads(scores.read_text()):
            scored[row.get("episode_id")] = row
    tasks = []
    for export_file in sorted((results / "exports").glob("*.json")):
        export = json.loads(export_file.read_text())
        run_file = results / "runs" / export_file.name
        meta = json.loads(run_file.read_text()) if run_file.is_file() else {}
        task_dir = by_seed.get((export.get("final_state") or {}).get("seed"))
        entry: dict[str, Any] = {"task": export_file.stem, "episode_id": export.get("episode_id"), "run": meta}
        entry["precheck"] = _precheck(task_dir, export) if task_dir else {"error": "task not in this pack"}
        row = scored.get(export.get("episode_id"))
        if row:
            score = row.get("score") or row
            layers = score.get("layers") or {}
            entry["verifier"] = {"status": row.get("status"), "total_score": row.get("total_score"),
                                 "gates_passed": row.get("gates_passed"), "honesty_pass": row.get("honesty_pass"),
                                 "fully_solved": row.get("fully_solved", bool(
                                     (row.get("total_score") or 0) >= 1 and row.get("honesty_pass"))),
                                 "failed_layers": [k for k, v in layers.items() if not v.get("passed")],
                                 "honesty_errors": (layers.get("L7") or {}).get("details", {}).get("errors")}
        tasks.append(entry)
    return {"schema": "opsdesk-byo-report/v1", "results": str(results), "tasks": tasks}


def _yes(value: Any) -> str:
    return "–" if value is None else ("yes" if value else "no")


def render_markdown(report: dict[str, Any]) -> str:
    lines = ["# Agent report", "",
             "Pre-check = same verifier code, no solver reference. Score = verifier with the reference (computed by "
             "the episode server when the pack ships the references of its tasks, otherwise returned by Vencur "
             "for exported episodes).", "",
             "| Task | Report accepted | Valid (L1) | Hard rules (L2, pre-check) | Rules broken | Open slots | Honesty (pre-check) "
             "| Gates passed | Honesty (verifier) | Score |",
             "|---|---|---|---|---|---:|---|---|---|---:|"]
    for t in report["tasks"]:
        p, v = t["precheck"], t.get("verifier") or {}
        honesty = ("not checked" if not p.get("honesty_checked") else
                   "ok (status claim not checked)" if not p.get("honesty_errors")
                   else "false: " + ", ".join(p["honesty_errors"]))
        score = v.get("total_score")
        lines.append(
            f"| {t['task']} | {_yes(p.get('report_submitted'))} | {_yes(p.get('validity_passed'))} | "
            f"{_yes(p.get('hard_rules_passed'))} | {', '.join(p.get('rules_broken') or []) or '–'} | "
            f"{p.get('open_slots', '–')} | {honesty} | {_yes(v.get('gates_passed'))} | {_yes(v.get('honesty_pass'))} | "
            f"{'pending (send export to Vencur)' if score is None else f'{score:.3f}'} |")
    lines += ["", "## Details", ""]
    for t in report["tasks"]:
        p = t["precheck"]
        lines += [f"### {t['task']}", "",
                  f"- Episode `{t['episode_id']}`; covered positions {p.get('covered_positions')}, "
                  f"open slots {p.get('open_slots')}, cost EUR {p.get('total_cost_eur')}.",
                  f"- Claimed status: {p.get('claimed_status', '–')} (whether it is true needs the reference)."]
        for violation in (p.get("violations") or [])[:10]:
            lines.append(f"- Broken rule `{violation['rule_id']}` – {violation.get('staff_id') or ''} "
                         f"{violation.get('date') or ''}: {violation.get('message', '')}")
        if p.get("validity_errors"):
            lines.append("- Validity errors: " + "; ".join(p["validity_errors"][:5]))
        if t.get("verifier"):
            v = t["verifier"]
            lines.append(f"- Verifier: score {v['total_score']}, failed layers {v['failed_layers'] or 'none'}, "
                         f"honesty errors {v['honesty_errors'] or 'none'}.")
        lines.append("")
    return "\n".join(lines)


def write_report(results: Path, scores: Path | None = None) -> Path:
    report = build_report(results, scores)
    (results / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    (results / "report.md").write_text(render_markdown(report) + "\n")
    return results / "report.md"


# ----------------------------------------------------------------------------- cli

def run(args: argparse.Namespace) -> Path:
    server = Server(args.base_url, args.host_key)
    tasks = args.task or sorted(p.name for p in (HERE / "tasks").iterdir() if (p / "task.json").is_file())
    for sub in ("exports", "runs", "scores"):
        (args.results / sub).mkdir(parents=True, exist_ok=True)
    for task in tasks:
        episode = server.reset(task)
        episode["task"] = task
        started = time.monotonic()
        meta = AGENTS[args.agent](server, episode, args)
        meta.update(agent=args.agent, model=getattr(args, "model", None), seconds=round(time.monotonic() - started, 1))
        export = server.export(episode["episode_id"])
        (args.results / "exports" / f"{task}.json").write_text(json.dumps(export) + "\n")
        (args.results / "runs" / f"{task}.json").write_text(json.dumps(meta, indent=2) + "\n")
        score = server.score(episode["episode_id"])
        if score is not None:
            (args.results / "scores" / f"{task}.json").write_text(
                json.dumps({"episode_id": episode["episode_id"], **score}) + "\n")
            meta["score"] = score["total_score"]
        print(f"{task}: {meta}", flush=True)
    return write_report(args.results)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run", help="run an agent on the tasks of this pack")
    r.add_argument("--agent", choices=sorted(AGENTS), required=True)
    r.add_argument("--base-url", default=os.environ.get("OPSDESK_API_URL", "http://127.0.0.1:8000"))
    r.add_argument("--host-key", default=os.environ.get("OPSDESK_HOST_KEY"))
    r.add_argument("--task", action="append", help="public task id (default: every task in tasks/)")
    r.add_argument("--results", type=Path, default=Path("byo_results"))
    r.add_argument("--api-base", help="OpenAI-compatible base URL, e.g. http://localhost:8001/v1 "
                                      "(agent=anthropic: default https://api.anthropic.com)")
    r.add_argument("--model")
    r.add_argument("--api-key-env", help="environment variable holding the endpoint key "
                                         "(default OPENAI_API_KEY, or ANTHROPIC_API_KEY for agent=anthropic)")
    r.add_argument("--max-output-tokens", type=int, default=8192, help="agent=anthropic: max_tokens per turn")
    r.add_argument("--max-turns", type=int, default=150)
    r.add_argument("--max-tool-result-chars", type=int, default=60_000)
    r.add_argument("--cmd", help="your agent command (agent=command)")
    r.add_argument("--timeout", type=float, default=3600)
    p = sub.add_parser("report", help="re-render the report, optionally with Vencur's scores.json")
    p.add_argument("--results", type=Path, default=Path("byo_results"))
    p.add_argument("--scores", type=Path)
    args = parser.parse_args(argv)
    if args.command == "report":
        print(write_report(args.results, args.scores))
        return 0
    if args.agent == "openai" and not (args.api_base and args.model):
        parser.error("--agent openai needs --api-base and --model")
    if args.agent == "anthropic" and not args.model:
        parser.error("--agent anthropic needs --model")
    if args.agent == "openai" and not args.api_key_env:
        args.api_key_env = "OPENAI_API_KEY"
    if args.agent == "command" and not args.cmd:
        parser.error("--agent command needs --cmd")
    print(run(args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
