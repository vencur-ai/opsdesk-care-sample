"""Single-active-episode session shared by HTTP and terminal clients."""

from __future__ import annotations

import json
from opsdesk.core.world_loader import load_world_data
from opsdesk.sim.events import announce_sick_calls
import hashlib
from collections import OrderedDict
from pathlib import Path
from typing import Any
import uuid
import os
import threading
import time
from functools import wraps
from pydantic import ValidationError

from opsdesk.apps.base import to_json_data
from opsdesk.apps.registry import get_tool_schemas, invoke_tool
from opsdesk.core.audit import ZERO_HASH, append_step, content_hash, episode_integrity_hash
from opsdesk.core.models import ActionLogEntry
from opsdesk.core.state import WorldState
from opsdesk.verifiers.score import FinalReportV1, FinalReportV2, score_episode
from opsdesk.verifiers.report_parser import (
    FINAL_REPORT_SCHEMA_LINE,
    final_report_submit_tool,
    normalize_final_report_payload,
    validate_family_outputs,
)


VISIBLE_FIELDS = (
    "home_id", "home_name", "jurisdiction", "start_date", "horizon_days",
    "sim_clock", "employees", "wards", "absences", "demand_slots", "roster",
    "messages", "documents", "timeclock_entries", "agency_offers",
    "agency_requests", "agency_budget", "payroll_exports", "works_council_required",
)
MAX_ACTION_ARGS_BYTES = 65_536
MAX_EPISODE_STEPS = int(os.environ.get("OPSDESK_MAX_STEPS", "3000"))
MAX_TOTAL_EPISODE_CALLS = int(os.environ.get("OPSDESK_MAX_TOTAL_CALLS", "10000"))
READ_ONLY_TOOLS = frozenset({
    "hr.list_employees", "hr.get_employee", "hr.list_absences",
    "roster.view", "timeclock.read", "documents.list", "documents.read",
    "forecast.get", "agency.list_offers", "messaging.read_inbox",
})


def _serialized(method):
    """Run one per-episode operation under the session's re-entrant lock."""
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self.lock:
            return method(self, *args, **kwargs)
    return wrapped


def public_task_id(task_id: str) -> str:
    suffix = task_id.rsplit("-", 1)[-1]
    return f"opsdesk-care-{suffix}" if suffix.isdigit() else task_id


def internal_task_id(tasks_root: Path, task_id: str) -> str:
    if (
        not task_id
        or task_id in {".", ".."}
        or "/" in task_id
        or "\\" in task_id
    ):
        raise FileNotFoundError("task not found")
    direct = tasks_root / task_id
    if direct.is_dir() and (direct / "task.json").is_file():
        return direct.name
    matches = []
    for path in tasks_root.iterdir():
        task_path = path / "task.json"
        if not path.is_dir() or not task_path.is_file():
            continue
        try:
            canonical_id = json.loads(task_path.read_text()).get("task_id", "")
        except (OSError, json.JSONDecodeError):
            continue
        if public_task_id(canonical_id) == task_id:
            matches.append(path.name)
    return matches[0] if len(matches) == 1 else task_id


class SeedMismatchError(ValueError):
    """Raised when a caller supplies a seed that differs from the task seed."""


class EpisodeQuotaExceeded(OverflowError):
    """Raised when one client has too many unsubmitted episodes."""


class EpisodeCapacityExceeded(OverflowError):
    """Raised when the server has no episode slot available."""


class EpisodeSession:
    """Owns one episode; references are read only by the host scorer."""

    def __init__(self, tasks_root: str | Path):
        self.tasks_root = Path(tasks_root).resolve()
        self.task_dir: Path | None = None
        self.task: dict[str, Any] | None = None
        self.initial: WorldState | None = None
        self.state: WorldState | None = None
        self.final_report: Any = None
        self.cached_score: dict[str, Any] | None = None
        self.locked = False
        self.episode_id: str | None = None
        self.steps: list[dict[str, Any]] = []
        self.audit_head = ZERO_HASH
        self.lock = threading.RLock()
        self.invalid_report_attempts = 0
        self.invalid_report_retry_limit = 0
        self.state_change_count = 0
        self.total_call_count = 0

    def reset(self, task_id: str, seed: int | None = None) -> dict[str, Any]:
        task_id = internal_task_id(self.tasks_root, task_id)
        task_dir = (self.tasks_root / task_id).resolve()
        if task_dir.parent != self.tasks_root or not (task_dir / "task.json").is_file():
            raise FileNotFoundError("task not found")
        task = json.loads((task_dir / "task.json").read_text())
        raw = load_world_data(task_dir, task)
        canonical_id = str(task.get("task_id", task_id))
        task_seed = int(raw["seed"])
        if seed is not None and seed != task_seed:
            raise SeedMismatchError("seed must match the task's world seed")
        raw["seed"] = task_seed
        state = WorldState.model_validate(raw)
        announce_sick_calls(state)  # same start world as load_episode_state (scorer)
        retry_setting = os.environ.get("OPSDESK_INVALID_REPORT_RETRIES", "auto").lower()
        if retry_setting == "auto":
            retry_limit = 3 if task.get("invalid_report_retries") == 3 else 0
        elif retry_setting in {"on", "true"}:
            retry_limit = 3
        elif retry_setting in {"off", "false"}:
            retry_limit = 0
        else:
            try:
                retry_limit = int(retry_setting)
            except ValueError as exc:
                raise ValueError("OPSDESK_INVALID_REPORT_RETRIES must be auto, on, off, or 0..3") from exc
            if retry_limit not in range(4):
                raise ValueError("OPSDESK_INVALID_REPORT_RETRIES must be auto, on, off, or 0..3")
        self.task_dir, self.task, self.initial = task_dir, task, state.model_copy(deep=True)
        self.state = state
        self.final_report = None
        self.cached_score = None
        self.locked = False
        self.invalid_report_attempts = 0
        self.invalid_report_retry_limit = retry_limit
        self.state_change_count = 0
        self.total_call_count = 0
        self.episode_id = uuid.uuid4().hex
        self.steps = []
        self.audit_head = ZERO_HASH
        return {
            "episode_id": self.episode_id,
            "observation": self.observation(brief=True),
            "tools": get_tool_schemas() + [
                final_report_submit_tool((self.task or {}).get("family"))
            ],
        }

    @_serialized
    def observation(self, brief: bool = False) -> dict[str, Any]:
        if self.state is None or self.task is None:
            raise RuntimeError("episode not initialized")
        from opsdesk.apps.base import to_json_data

        included_fields = (
            {"home_id", "home_name", "jurisdiction", "start_date", "horizon_days"}
            if brief else set(VISIBLE_FIELDS)
        )
        state_data = to_json_data(
            self.state.model_dump(mode="python", include=included_fields)
        )
        return {
            "task_id": public_task_id(self.task["task_id"]),
            "instruction": self.task.get("instruction", ""),
            **state_data,
        }

    @_serialized
    def step(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if self.state is None:
            raise RuntimeError("episode not initialized")
        if self.locked:
            return {"ok": False, "error_code": "EPISODE_LOCKED", "message": "Episode is locked after submit."}
        if name == "final_report_submit":
            return self.submit(args)
        if self.total_call_count >= MAX_TOTAL_EPISODE_CALLS:
            return {
                "ok": False,
                "error_code": "EPISODE_CALL_LIMIT",
                "message": "Episode total-call limit reached.",
            }
        self.total_call_count += 1
        try:
            safe_args = to_json_data(args)
            encoded_args = json.dumps(
                safe_args, default=str, allow_nan=False, sort_keys=True
            ).encode("utf-8")
        except (TypeError, ValueError):
            return self._record_rejected_step(
                name,
                {"error_code": "INVALID_ARGUMENTS"},
                "INVALID_ARGUMENTS",
                "Arguments must contain only finite JSON values.",
            )
        if name not in READ_ONLY_TOOLS and self.state_change_count >= MAX_EPISODE_STEPS:
            return self._record_rejected_step(
                name, {"error_code": "EPISODE_STEP_LIMIT"},
                "EPISODE_STEP_LIMIT", "Episode step limit reached.",
            )
        if len(encoded_args) > MAX_ACTION_ARGS_BYTES:
            return self._record_rejected_step(
                name,
                {"error_code": "ARGUMENTS_TOO_LARGE", "size_bytes": len(encoded_args)},
                "ARGUMENTS_TOO_LARGE",
                "Tool arguments exceed the 64 KiB limit.",
            )
        before = len(self.state.action_log)
        result = invoke_tool(self.state, name, args).model_dump(mode="json")
        action = self.state.action_log[-1].model_dump(mode="json") if len(self.state.action_log) > before else None
        if len(self.state.action_log) > before:
            action["sequence"] = len(self.steps)
            del self.state.action_log[before:]
        if name not in READ_ONLY_TOOLS and result.get("ok") and action is not None:
            self.state_change_count += 1
        assert self.episode_id is not None
        stored_args = (
            action.get("args", safe_args)
            if result.get("ok") and action is not None
            else (
                {"args_sha256": hashlib.sha256(encoded_args).hexdigest()}
                if not result.get("ok", False)
                else safe_args
            )
        )
        stored_action = action
        if result.get("ok") and action is not None:
            stored_action = {key: value for key, value in action.items() if key != "args"}
            stored_action["_args_in_step"] = True
        record = append_step(
            self.audit_head,
            episode_id=self.episode_id,
            step_index=len(self.steps),
            tool=name,
            args=stored_args,
            result=result,
            scorer_action=stored_action,
        )
        self.steps.append(record)
        self.audit_head = record["chain_hash"]
        return result

    def _record_rejected_step(
        self, name: str, audit_args: dict[str, Any], error_code: str, message: str
    ) -> dict[str, Any]:
        result = {"ok": False, "error_code": error_code, "message": message}
        assert self.state is not None and self.episode_id is not None
        self.state.record_action("api", name, {"error_code": error_code})
        record = append_step(
            self.audit_head,
            episode_id=self.episode_id,
            step_index=len(self.steps),
            tool=name,
            args=audit_args,
            result=result,
            scorer_action=self.state.action_log[-1].model_dump(mode="json"),
        )
        self.steps.append(record)
        self.audit_head = record["chain_hash"]
        return result

    @_serialized
    def submit(self, final_report: Any) -> dict[str, Any]:
        if self.state is None:
            raise RuntimeError("episode not initialized")
        if self.locked:
            return {"accepted": False, "error_code": "EPISODE_LOCKED", "message": "Episode is already locked."}
        try:
            normalized_report = normalize_final_report_payload(final_report)
            if normalized_report.get("schema_version") == "final_report/v2":
                validated_report = FinalReportV2.model_validate(normalized_report)
            else:
                raise ValueError("Live submissions require schema_version final_report/v2.")
            family_errors = validate_family_outputs(
                self.task.get("family"), validated_report.family_outputs
            )
            if family_errors:
                raise FamilyOutputValidationError(family_errors)
        except (ValidationError, ValueError, ArithmeticError) as exc:
            if isinstance(exc, ValidationError):
                validation_errors = exc.errors()
            else:
                validation_errors = [{
                    "loc": ("_invalid_response",), "msg": str(exc),
                    "type": "value_error",
                }]
            errors = sorted(
                [
                    {
                    "path": ".".join(str(part) for part in error["loc"]),
                    "message": error["msg"],
                    "type": error["type"],
                    }
                for error in validation_errors
                ],
                key=lambda error: (error["path"], error["type"], error["message"]),
            )
            self.invalid_report_attempts += 1
            attempt = self.invalid_report_attempts
            will_lock = attempt > self.invalid_report_retry_limit
            attempts_remaining = max(0, self.invalid_report_retry_limit + 1 - attempt)
            missing = [e["path"] for e in errors if e["type"] == "missing"]
            extra = [e["path"] for e in errors if e["type"] == "extra_forbidden"]
            detail_parts = []
            if missing:
                detail_parts.append("Missing fields: " + ", ".join(missing) + ".")
            if extra:
                detail_parts.append("Extra fields: " + ", ".join(extra) + ".")
            if not detail_parts:
                detail_parts.append("Validation errors: " + "; ".join(
                    f"{e['path']}: {e['message']}" for e in errors
                ))
            result = {
                "accepted": False,
                "error_code": "INVALID_REPORT",
                "message": (
                    f"Attempt {attempt}; {attempts_remaining} retries remain. "
                    + " ".join(detail_parts)
                    + f" Expected schema: {FINAL_REPORT_SCHEMA_LINE}"
                ),
                "errors": errors,
                "attempt": attempt,
                "attempts_remaining": attempts_remaining,
                "episode_locked": will_lock,
            }
            if will_lock:
                self.final_report = {"_rejected_report": errors}
                self.locked = True
                result["error_code"] = "INVALID_REPORT_LIMIT"
                result["message"] += " No retries remain; episode locked with an L1 failure."
                result["l1_failure"] = True
            self._append_audit("submit.invalid_report", {"attempt": attempt}, result)
            return result
        self.final_report = validated_report.model_dump(mode="json")
        self.locked = True
        try:
            self.cached_score = self._compute_score()
        except Exception:
            # Submission remains accepted; a failed score can be retried by the host.
            self.cached_score = None
        return {"accepted": True, "episode_locked": True}

    def _append_audit(self, tool: str, args: dict[str, Any], result: dict[str, Any]) -> None:
        assert self.episode_id is not None
        record = append_step(
            self.audit_head,
            episode_id=self.episode_id,
            step_index=len(self.steps),
            tool=tool,
            args=args,
            result=result,
            scorer_action=None,
        )
        self.steps.append(record)
        self.audit_head = record["chain_hash"]

    def score(self) -> dict[str, Any]:
        if self.cached_score is not None:
            return self.cached_score
        return self._compute_score()

    def _compute_score(self) -> dict[str, Any]:
        if not self.locked or self.final_report is None:
            raise RuntimeError("submit a final report before requesting the score")
        assert self.task_dir is not None and self.state is not None
        return score_episode(
            self.task_dir, self.state, self.scorer_actions(), self.final_report,
            strict_honesty=os.environ.get("OPSDESK_STRICT_HONESTY", "0").lower()
            in {"1", "true", "yes"},
        ).model_dump(mode="json")

    def scorer_actions(self) -> list[ActionLogEntry]:
        actions = []
        for step in self.steps:
            if step.get("scorer_action") is None:
                continue
            action = dict(step["scorer_action"])
            if action.pop("_args_in_step", False):
                action["args"] = step.get("args", {})
            actions.append(ActionLogEntry.model_validate(action))
        return actions

    def export(self) -> dict[str, Any]:
        if self.state is None:
            raise RuntimeError("episode not initialized")
        episode = {
            "episode_id": self.episode_id,
            "final_state": self.state.model_dump(mode="json"),
            "steps": self.steps,
            "audit_head": self.audit_head,
            "final_report": self.final_report,
            "trust_status": "untrusted: client-supplied log",
        }
        episode["integrity_hash"] = episode_integrity_hash(episode)
        return episode


class EpisodeManager:
    """Concurrent isolated episode registry with bounded lifetime."""

    def __init__(
        self,
        tasks_root: str | Path,
        max_episodes: int = 100,
        max_episodes_per_client: int = 5,
        idle_timeout: int = 3600,
        new_episode_timeout: int = 120,
        max_scored_results: int = 10_000,
        max_audit_archives: int = 1_000,
        session_class: type[EpisodeSession] = EpisodeSession,
    ):
        self.tasks_root = tasks_root
        self.max_episodes = max_episodes
        self.max_episodes_per_client = max_episodes_per_client
        self.idle_timeout = idle_timeout
        self.new_episode_timeout = new_episode_timeout
        self.max_scored_results = max_scored_results
        self.max_audit_archives = max_audit_archives
        self.session_class = session_class
        self.episodes: dict[str, tuple[EpisodeSession, float]] = {}
        self.episode_owners: dict[str, str] = {}
        self.scored_results: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self.audit_archives: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self.score_cache_evictions = 0
        self.audit_archive_evictions = 0
        # Scores of submitted episodes that the host has not read yet. They are
        # never evicted; new episodes are refused instead (CAPACITY_FULL).
        self.unread_scores: set[str] = set()
        self.unread_owner: dict[str, str] = {}
        self.max_unread_per_client = max(1, max_scored_results // 10)
        self.lock = threading.RLock()

    def reset(
        self,
        task_id: str,
        seed: int | None = None,
        owner_id: str = "local",
    ) -> dict[str, Any]:
        with self.lock:
            self._expire()
            client_count = sum(
                self.episode_owners.get(key) == owner_id
                and not episode.locked
                and episode.final_report is None
                for key, (episode, _last_used) in self.episodes.items()
            )
            if client_count >= self.max_episodes_per_client:
                raise EpisodeQuotaExceeded("per-client unsubmitted episode quota reached")
            owner_unread = sum(
                1 for key in self.unread_scores if self.unread_owner.get(key) == owner_id
            )
            if owner_unread >= self.max_unread_per_client:
                raise EpisodeQuotaExceeded(
                    "per-client limit of unread submitted scores reached; "
                    "the host must read or delete scores"
                )
            if len(self.unread_scores) >= self.max_scored_results:
                raise EpisodeCapacityExceeded(
                    "unread score store is full; the host must read or delete scores"
                )
            if len(self.episodes) >= self.max_episodes:
                completed = [
                    (last_used, key, episode)
                    for key, (episode, last_used) in self.episodes.items()
                    if episode.locked and episode.final_report is not None
                ]
                if not completed:
                    raise EpisodeCapacityExceeded("global episode capacity reached")
                _, key, episode = min(completed)
                self._cache_score(key, episode)
                self._archive_audit(key, episode)
                del self.episodes[key]
                self.episode_owners.pop(key, None)
            episode = self.session_class(self.tasks_root)
            result = episode.reset(task_id, seed)
            self.episodes[episode.episode_id] = (episode, time.monotonic())
            self.episode_owners[episode.episode_id] = owner_id
            return result

    def get(self, episode_id: str) -> EpisodeSession:
        with self.lock:
            self._expire()
            entry = self.episodes.get(episode_id)
            if entry is None:
                raise KeyError("episode not found or expired")
            self.episodes[episode_id] = (entry[0], time.monotonic())
            return entry[0]

    def get_audit(self, episode_id: str) -> dict[str, Any]:
        with self.lock:
            self._expire()
            entry = self.episodes.get(episode_id)
            if entry is not None:
                self.episodes[episode_id] = (entry[0], time.monotonic())
                return {
                    "steps": [dict(record) for record in entry[0].steps],
                    "head": entry[0].audit_head,
                }
            archived = self.audit_archives.get(episode_id)
            if archived is None:
                raise KeyError("episode not found or expired")
            return {
                "steps": [dict(record) for record in archived["steps"]],
                "head": archived["head"],
            }

    def _archive_audit(self, episode_id: str, episode: EpisodeSession) -> None:
        self.audit_archives[episode_id] = {
            "steps": [dict(record) for record in episode.steps],
            "head": episode.audit_head,
        }
        self.audit_archives.move_to_end(episode_id)
        while len(self.audit_archives) > self.max_audit_archives:
            victim = next(
                (key for key in self.audit_archives if key not in self.unread_scores),
                next(iter(self.audit_archives)),
            )
            self.audit_archives.pop(victim)
            self.audit_archive_evictions += 1

    def score(self, episode_id: str) -> dict[str, Any]:
        with self.lock:
            self._expire()
            cached = self.scored_results.get(episode_id)
            if cached is not None:
                self.scored_results.move_to_end(episode_id)
                self.unread_scores.discard(episode_id)
                self.unread_owner.pop(episode_id, None)
                return cached
            entry = self.episodes.get(episode_id)
            if entry is None:
                raise KeyError("episode not found or expired")
            self.episodes[episode_id] = (entry[0], time.monotonic())
            result = self._score_and_cache(episode_id, entry[0])
            self.unread_scores.discard(episode_id)
            self.unread_owner.pop(episode_id, None)
            return result

    def _cache_score(self, episode_id: str, episode: EpisodeSession) -> None:
        if episode.locked and episode.final_report is not None:
            self._score_and_cache(episode_id, episode)

    def _score_and_cache(
        self, episode_id: str, episode: EpisodeSession
    ) -> dict[str, Any]:
        if not episode.locked or episode.final_report is None:
            return episode.score()
        try:
            result = episode.score()
        except Exception:
            result = {
                "ok": False,
                "error_code": "SCORING_FAILED",
                "message": "Episode could not be scored; other episodes remain available.",
            }
        if result.get("error_code") != "SCORING_FAILED":
            self._remember_score(episode_id, result)
        return result

    def _remember_score(self, episode_id: str, result: dict[str, Any]) -> None:
        if episode_id not in self.scored_results:
            self.unread_scores.add(episode_id)
            owner = self.episode_owners.get(episode_id)
            if owner is not None:
                self.unread_owner[episode_id] = owner
        self.scored_results[episode_id] = result
        self.scored_results.move_to_end(episode_id)
        while len(self.scored_results) > self.max_scored_results:
            victim = next(
                (key for key in self.scored_results if key not in self.unread_scores),
                None,
            )
            if victim is None:
                break  # only unread scores left: keep them, reset refuses instead
            self.scored_results.pop(victim)
            self.score_cache_evictions += 1

    def score_cache_info(self) -> dict[str, Any]:
        with self.lock:
            return {
                "capacity": self.max_scored_results,
                "cached": len(self.scored_results),
                "evictions": self.score_cache_evictions,
                "eviction_policy": "least-recently-used among scores the host has read; unread scores are never evicted",
                "unread": len(self.unread_scores),
                "audit_archives": len(self.audit_archives),
                "audit_archive_capacity": self.max_audit_archives,
                "audit_archive_evictions": self.audit_archive_evictions,
                "audit_archive_eviction_policy": "oldest archived episode first, skipping episodes with unread scores",
            }

    def delete_score(self, episode_id: str) -> bool:
        with self.lock:
            self.unread_scores.discard(episode_id)
            self.unread_owner.pop(episode_id, None)
            return self.scored_results.pop(episode_id, None) is not None

    def delete_episode(self, episode_id: str, owner_id: str) -> bool | None:
        """Delete an active episode only for its creating client."""
        with self.lock:
            self._expire()
            if episode_id not in self.episodes:
                return None
            if self.episode_owners.get(episode_id) != owner_id:
                return False
            if self.episodes[episode_id][0].locked:
                return False
            self._archive_audit(episode_id, self.episodes[episode_id][0])
            self.episodes.pop(episode_id, None)
            self.episode_owners.pop(episode_id, None)
            self.scored_results.pop(episode_id, None)
            return True

    def host_delete_episode(self, episode_id: str) -> bool:
        with self.lock:
            self._expire()
            existed = (
                episode_id in self.episodes
                or episode_id in self.audit_archives
                or episode_id in self.scored_results
            )
            self.episodes.pop(episode_id, None)
            self.episode_owners.pop(episode_id, None)
            self.scored_results.pop(episode_id, None)
            self.audit_archives.pop(episode_id, None)
            self.unread_scores.discard(episode_id)
            self.unread_owner.pop(episode_id, None)
            return existed

    def _expire(self) -> None:
        now = time.monotonic()
        expired: list[str] = []
        for key, (episode, last_used) in list(self.episodes.items()):
            timeout = (
                self.new_episode_timeout
                if not episode.steps
                else self.idle_timeout
            )
            if now - last_used > timeout:
                if episode.locked and episode.final_report is not None:
                    self._cache_score(key, episode)
                expired.append(key)
            elif now - last_used > self.idle_timeout and episode.locked:
                self._cache_score(key, episode)
        for key in expired:
            self._archive_audit(key, self.episodes[key][0])
        self.episodes = {
            key: value for key, value in self.episodes.items()
            if key not in expired
        }
        for key in expired:
            self.episode_owners.pop(key, None)


class FamilyOutputValidationError(ValueError):
    def __init__(self, errors: list[dict[str, str]]):
        self.errors = errors
        super().__init__("; ".join(
            f"{error['path']}: {error['message']}" for error in errors
        ))


def _sanitize_invalid_json(value: Any) -> Any:
    """Replace non-finite values so rejected requests can still be audited safely."""
    if hasattr(value, "isoformat") and callable(value.isoformat):
        return value.isoformat()
    if isinstance(value, float):
        import math

        if not math.isfinite(value):
            return f"<non-finite:{value}>"
        return value
    if isinstance(value, dict):
        return {str(key): _sanitize_invalid_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize_invalid_json(item) for item in value]
    return value
