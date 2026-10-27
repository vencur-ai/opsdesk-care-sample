"""Dependency-free OpenEnv-style reset/step/close wrapper over the native API."""

from __future__ import annotations

from typing import Any


class OpenEnvOpsDesk:
    """Client-side Gym-style wrapper; transport is injected for testability."""

    def __init__(self, api: Any, task_id: str, seed: int | None = None):
        self.api = api
        self.task_id = task_id
        self.seed = seed
        self.episode_id: str | None = None
        self.closed = False

    def reset(self) -> dict[str, Any]:
        if self.closed:
            raise RuntimeError("environment is closed")
        result = self.api.post("/reset", {"task_id": self.task_id, "seed": self.seed})
        self.episode_id = result["episode_id"]
        return {"observation": result["observation"], "tools": result["tools"]}

    def step(self, action: dict[str, Any]) -> dict[str, Any]:
        if self.closed or self.episode_id is None:
            raise RuntimeError("reset must be called before step")
        result = self.api.post("/step", {
            "episode_id": self.episode_id,
            "tool": action["tool_name"],
            "args": action.get("parameters", {}),
        })
        return {
            "observation": result,
            "reward": 0.0,
            "done": False,
            "is_error": not bool(result.get("ok", result.get("accepted", False))),
        }

    def close(self) -> None:
        self.closed = True
