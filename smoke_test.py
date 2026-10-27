"""Smoke test for an OpsDesk-Care task pack: one episode through the public API, no reference needed.

    python smoke_test.py --base-url http://127.0.0.1:8000 [--host-key KEY]

Lists the tools, resets the first task in tasks/ by its public id, reads the roster and the honesty
summary, submits an honest report of the untouched roster and (with the host key) exports the episode.
Exit code 0 means the server, the tools and the report contract work.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


def call(base: str, method: str, path: str, body=None, headers=None):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(base.rstrip("/") + path, data=data, method=method,
                                     headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--host-key", default=os.environ.get("OPSDESK_HOST_KEY"))
    parser.add_argument("--tasks", type=Path, default=Path(__file__).resolve().parent / "tasks")
    args = parser.parse_args()
    public_id = sorted(p.name for p in args.tasks.iterdir() if (p / "task.json").is_file())[0]
    status, tools = call(args.base_url, "GET", "/tools")
    assert status == 200 and any(t["name"] == "final_report_submit" for t in tools), "tool list"
    status, reset = call(args.base_url, "POST", "/reset", {"task_id": public_id})
    assert status == 200, f"reset failed: {reset}"
    episode, key = reset["episode_id"], {"X-OpsDesk-Agent-Key": reset.get("agent_key", "")}
    status, summary = call(args.base_url, "POST", "/step", {
        "episode_id": episode, "tool": "roster.view",
        "args": {"include_honesty_summary": True, "summary_only": True}}, key)
    assert status == 200 and summary.get("ok"), f"roster.view failed: {summary}"
    submit_tool = next(t for t in reset["tools"] if t["name"] == "final_report_submit")
    family_schema = submit_tool["parameters"]["properties"]["family_outputs"]
    family_outputs = {name: ([] if family_schema["properties"][name].get("type") == "array" else 0)
                      for name in family_schema.get("required", [])}
    report = {"schema_version": "final_report/v2", "status": "complete",
              **summary["data"]["honesty_summary"], "family_outputs": family_outputs}
    status, submitted = call(args.base_url, "POST", "/step", {
        "episode_id": episode, "tool": "final_report_submit", "args": report}, key)
    accepted = bool(submitted.get("accepted"))
    assert accepted, f"report rejected: {submitted}"
    exported = None
    if args.host_key:
        status, exported = call(args.base_url, "GET", f"/export?episode_id={episode}",
                                headers={"X-OpsDesk-Host-Key": args.host_key})
        assert status == 200 and "final_state" in exported, f"export failed: {status}"
    print(json.dumps({"task": public_id, "tools": len(tools), "report_contract_ok": bool(accepted),
                      "exported": exported is not None}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
