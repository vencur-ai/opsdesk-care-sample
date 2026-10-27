"""Single loader for task worlds.

The agent-visible bundle (``tasks/<id>/world.json``) does not contain the
house-rule policy or the staff personas.  Those live in a host-side file
``sample_private/<task_id>.json`` next to ``tasks/``.  Every component that
builds a world (episode session, scorer, self-check, certificate checker,
metamorphic checks, CLI) must use this loader so that all of them judge the
same world.  If the private part is missing the loader fails closed instead
of silently falling back to model defaults.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

PRIVATE_KEYS = ("house_rule_policy", "staff_personas")


class PrivateTaskDataMissing(FileNotFoundError):
    """Raised when the host-side part of a task world cannot be found."""


def _private_roots(task_dir: Path) -> list[Path]:
    roots: list[Path] = []
    env = os.environ.get("OPSDESK_PRIVATE_ROOT")
    if env:
        roots.append(Path(env))
    # host_data/ is the name used by buyer packs (2.5+); sample_private/ by the 2.4.x samples
    roots.append(task_dir.parent.parent / "host_data")
    roots.append(task_dir.parent.parent / "sample_private")
    return roots


def load_task(task_dir: Path) -> dict[str, Any]:
    return json.loads((Path(task_dir) / "task.json").read_text())


def load_world_data(task_dir: Path, task: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return the complete world dict (public world + host-side private part)."""
    task_dir = Path(task_dir)
    task = task if task is not None else load_task(task_dir)
    world_path = (task_dir / task.get("world_file", "world.json")).resolve()
    if not world_path.is_relative_to(task_dir.resolve()) or not world_path.is_file():
        raise FileNotFoundError("task not found")
    raw = json.loads(world_path.read_text())
    canonical_id = str(task.get("task_id", task_dir.name))
    private_data = None
    for root in _private_roots(task_dir):
        root = root.resolve()
        path = (root / f"{canonical_id}.json").resolve()
        if path.parent == root and path.is_file():
            private_data = json.loads(path.read_text())
            break
    if private_data is not None:
        raw.update({k: v for k, v in private_data.items()})
    elif "house_rule_policy" not in raw:
        raise PrivateTaskDataMissing(
            f"host-side task data for {canonical_id} not found; expected "
            f"sample_private/{canonical_id}.json next to the tasks directory "
            "(or set OPSDESK_PRIVATE_ROOT)"
        )
    if "seed" not in raw:
        suffix = canonical_id.rsplit("-", 1)[-1]
        if not suffix.isdigit():
            raise ValueError("world has no seed and task_id has no numeric suffix")
        raw["seed"] = int(suffix)
    return raw


def load_episode_state(task_dir: Path, task: dict[str, Any] | None = None):
    """The world exactly as an episode starts: loaded, validated, events announced.

    Since 2.4.12 every scheduled sick call and demand change is applied at reset
    (``sim.events.announce_sick_calls``), because references are solved on the
    event-projected world. The episode session, the scorer and the reference
    self-check all start from this one function so they judge the same world.
    """
    from opsdesk.core.state import WorldState
    from opsdesk.sim.events import announce_sick_calls

    state = WorldState.model_validate(load_world_data(task_dir, task))
    announce_sick_calls(state)
    return state
