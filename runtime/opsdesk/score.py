"""Score an exported OpsDesk episode from the command line."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from opsdesk.core.audit import verify_episode_export
from opsdesk.core.state import WorldState
from opsdesk.verifiers.score import score_episode


def main() -> None:
    parser = argparse.ArgumentParser(description="Score a submitted OpsDesk episode.")
    parser.add_argument("--task", required=True, help="Task ID under OPSDESK_TASKS_ROOT")
    parser.add_argument("--episode", required=True, type=Path, help="JSON exported episode")
    parser.add_argument("--tasks-root", default=os.environ.get("OPSDESK_TASKS_ROOT", "./tasks"))
    args = parser.parse_args()
    task_dir = Path(args.tasks_root).resolve() / args.task
    if task_dir.parent != Path(args.tasks_root).resolve():
        parser.error("task must be a direct child of OPSDESK_TASKS_ROOT")
    try:
        episode = json.loads(args.episode.read_text())
        if not verify_episode_export(episode):
            parser.error("episode integrity check failed; recorded steps or episode payload were altered")
        state = WorldState.model_validate(episode["final_state"])
        actions = []
        for step in episode["steps"]:
            if step.get("scorer_action") is None:
                continue
            action = dict(step["scorer_action"])
            if action.pop("_args_in_step", False):
                action["args"] = step.get("args", {})
            actions.append(action)
        result = score_episode(
            task_dir,
            state,
            actions,
            episode["final_report"],
            strict_honesty=os.environ.get("OPSDESK_STRICT_HONESTY", "0").lower()
            in {"1", "true", "yes"},
        )
    except Exception as exc:
        parser.error(f"episode could not be scored: {exc}")
    print(json.dumps({
        "trust_status": "untrusted: client-supplied log",
        "integrity": "hash chain verified (not authenticated)",
        "score": result.model_dump(mode="json"),
    }, indent=2))


if __name__ == "__main__":
    main()
