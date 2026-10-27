"""Shared typed tool interface and JSON-schema generation."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum
import json
import math
from typing import Any, ClassVar, Type

from pydantic import ValidationError, model_serializer

from opsdesk.core.models import DomainModel
from opsdesk.core.state import WorldState


class ToolArgs(DomainModel):
    """Base class for strict JSON tool arguments."""


class ToolResult(DomainModel):
    """Uniform JSON result returned by an agent-facing tool."""

    ok: bool
    data: Any = None
    error_code: str | None = None
    message: str | None = None

    @model_serializer(mode="wrap")
    def serialize_result(self, handler, info):
        """Keep failures to the documented, compact JSON error contract."""
        serialized = handler(self)
        if not self.ok:
            return {
                "ok": False,
                "error_code": self.error_code or "INTERNAL_ERROR",
                "message": self.message or "Tool call failed.",
            }
        return {"ok": True, "data": serialized.get("data")}

    @property
    def error(self) -> str | None:
        """Compatibility alias for callers that display a result message."""
        return self.message

    @classmethod
    def failure(cls, error_code: str, message: str) -> ToolResult:
        """Construct a stable structured error response."""
        return cls(ok=False, error_code=error_code, message=message)


class ToolError(Exception):
    """Expected operational refusal surfaced to the agent."""

    def __init__(self, message: str, error_code: str = "OPERATION_REFUSED") -> None:
        super().__init__(message)
        self.error_code = error_code


class AppTool(ABC):
    """A named function exposed by one of the nine simulated apps."""

    app: ClassVar[str]
    name: ClassVar[str]
    description: ClassVar[str]
    args_model: ClassVar[Type[ToolArgs]]

    def definition(self) -> dict[str, Any]:
        """Return the JSON schema description used by tool-calling agents."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.args_model.model_json_schema(),
        }

    def invoke(self, state: WorldState, payload: Any) -> ToolResult:
        """Validate, execute, and log one tool call without leaking exceptions."""
        try:
            return self._invoke_validated(state, payload)
        except Exception:
            _record_action(
                state,
                self.app,
                self.name,
                {"error_code": "INTERNAL_ERROR"},
            )
            return ToolResult.failure("INTERNAL_ERROR", "Tool execution failed.")

    def _invoke_validated(self, state: WorldState, payload: Any) -> ToolResult:
        """Perform one call; the public wrapper converts unforeseen failures."""
        try:
            args = self.args_model.model_validate(payload)
        except ValidationError as exc:
            errors = [
                {
                    "field": ".".join(str(part) for part in issue["loc"]),
                    "message": issue["msg"],
                }
                for issue in exc.errors()
            ]
            errors.sort(key=lambda item: (item["field"], item["message"]))
            details = "; ".join(
                f"{item['field']}: {item['message']}" if item["field"] else item["message"]
                for item in errors
            )
            logged = _record_action(
                state,
                self.app,
                self.name,
                {"args": _json_safe(payload), "error_code": "INVALID_ARGUMENTS"},
            )
            if not logged:
                return ToolResult.failure("INTERNAL_ERROR", "Tool action could not be logged.")
            return ToolResult.failure(
                "INVALID_ARGUMENTS",
                f"Invalid arguments. {details}" if details else "Invalid arguments.",
            )
        except Exception:
            logged = _record_action(
                state,
                self.app,
                self.name,
                {"args": _json_safe(payload), "error_code": "INVALID_ARGUMENTS"},
            )
            if not logged:
                return ToolResult.failure("INTERNAL_ERROR", "Tool action could not be logged.")
            return ToolResult.failure("INVALID_ARGUMENTS", "Arguments must be a JSON object.")

        normalized_args = args.model_dump(mode="json", exclude_none=True)
        try:
            result = self.execute(state, args)
            normalized_result = to_json_data(result)
        except ToolError as exc:
            logged = _record_action(
                state,
                self.app,
                self.name,
                {"args": normalized_args, "error_code": exc.error_code},
            )
            if not logged:
                return ToolResult.failure("INTERNAL_ERROR", "Tool action could not be logged.")
            return ToolResult.failure(exc.error_code, str(exc))
        except Exception:
            logged = _record_action(
                state,
                self.app,
                self.name,
                {"args": normalized_args, "error_code": "INTERNAL_ERROR"},
            )
            if not logged:
                return ToolResult.failure("INTERNAL_ERROR", "Tool action could not be logged.")
            return ToolResult.failure("INTERNAL_ERROR", "Tool execution failed.")

        if not _record_action(state, self.app, self.name, {"args": normalized_args}):
            return ToolResult.failure("INTERNAL_ERROR", "Tool action could not be logged.")
        return ToolResult(ok=True, data=normalized_result)

    @abstractmethod
    def execute(self, state: WorldState, args: ToolArgs) -> Any:
        """Execute validated arguments against one world."""


def tool_specs(tools: list[AppTool] | tuple[AppTool, ...]) -> list[dict[str, Any]]:
    """Return stable tool specifications for every app function."""
    return [tool.definition() for tool in sorted(tools, key=lambda item: item.name)]


def _json_safe(value: Any) -> Any:
    """Convert malformed input to safe, deterministic logging data."""
    return _normalize_json(value, strict=False)


def _normalize_json(value: Any, *, strict: bool) -> Any:
    """Normalize common Python values and reject unsupported result objects."""
    if isinstance(value, DomainModel):
        return _normalize_json(value.model_dump(mode="python"), strict=strict)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (date, datetime, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        if strict:
            raise TypeError("non-finite numbers are not valid JSON results")
        return {"invalid_number": str(value)}
    if isinstance(value, dict):
        pairs = sorted(value.items(), key=lambda pair: str(pair[0]))
        normalized = {}
        for key, item in pairs:
            normalized_key = str(key.value if isinstance(key, Enum) else key)
            if normalized_key in normalized and strict:
                raise TypeError("result object contains colliding JSON keys")
            normalized[normalized_key] = _normalize_json(item, strict=strict)
        return normalized
    if isinstance(value, (list, tuple, set, frozenset)):
        items = [_normalize_json(item, strict=strict) for item in value]
        if isinstance(value, (set, frozenset)):
            return sorted(
                items,
                key=lambda item: json.dumps(
                    item,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ),
            )
        return items
    if strict:
        raise TypeError("tool result contains a non-JSON value")
    return {"unsupported_value_type": type(value).__name__}


def to_json_data(value: Any) -> Any:
    """Serialize a result to strict JSON-compatible data with stable ordering."""
    normalized = _normalize_json(value, strict=True)
    json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return normalized


def _record_action(
    state: WorldState,
    app: str,
    name: str,
    data: dict[str, Any],
) -> bool:
    """Record an action without allowing logging exceptions to leak to a caller."""
    try:
        state.record_action(app, name, data)
        return True
    except Exception:
        return False