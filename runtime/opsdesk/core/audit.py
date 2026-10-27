"""Integrity helpers for server-recorded tool-call episodes."""

from __future__ import annotations

import hashlib
import json
from typing import Any

ZERO_HASH = "0" * 64


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def append_step(
    previous_hash: str,
    *,
    episode_id: str,
    step_index: int,
    tool: str,
    args: dict[str, Any],
    result: dict[str, Any],
    scorer_action: dict[str, Any] | None,
) -> dict[str, Any]:
    body = {
        "episode_id": episode_id,
        "step_index": step_index,
        "tool": tool,
        "args": args,
        "result_hash": content_hash(result),
        "scorer_action": scorer_action,
        "previous_hash": previous_hash,
    }
    return {**body, "chain_hash": content_hash(body)}


def verify_steps(
    episode_id: str, steps: list[dict[str, Any]], expected_head: str
) -> bool:
    previous = ZERO_HASH
    for index, record in enumerate(steps):
        if record.get("episode_id") != episode_id:
            return False
        if record.get("step_index") != index:
            return False
        if record.get("previous_hash") != previous:
            return False
        body = {key: value for key, value in record.items() if key != "chain_hash"}
        current = content_hash(body)
        if record.get("chain_hash") != current:
            return False
        previous = current
    return expected_head == previous


def episode_integrity_hash(episode: dict[str, Any]) -> str:
    payload = {key: value for key, value in episode.items() if key != "integrity_hash"}
    return content_hash(payload)


def verify_episode_export(episode: dict[str, Any]) -> bool:
    episode_id = episode.get("episode_id")
    steps = episode.get("steps")
    if not isinstance(episode_id, str) or not isinstance(steps, list):
        return False
    if not verify_steps(episode_id, steps, episode.get("audit_head", "")):
        return False
    return episode.get("integrity_hash") == episode_integrity_hash(episode)
