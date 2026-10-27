"""Stable registry for the nine local agent-facing applications."""

from __future__ import annotations

from typing import Any

from opsdesk.apps.agency import ListAgencyOffersTool, RequestAgencyStaffTool
from opsdesk.apps.base import AppTool, ToolResult, tool_specs
from opsdesk.apps.documents import ListDocumentsTool, ReadDocumentTool
from opsdesk.apps.forecast import DemandForecastTool
from opsdesk.apps.hr import GetEmployeeTool, ListAbsencesTool, ListEmployeesTool
from opsdesk.apps.messaging import ReadInboxTool, SendMessageTool
from opsdesk.apps.payroll import ExportPayrollTool
from opsdesk.apps.roster import (
    AssignShiftTool,
    PublishRosterTool,
    SwapShiftsTool,
    UnassignShiftTool,
    ViewRosterTool,
)
from opsdesk.apps.timeclock import AttemptTimeClockEditTool, ReadTimeClockTool
from opsdesk.apps.works_council import SubmitRosterTool
from opsdesk.core.state import WorldState


TOOLS: tuple[AppTool, ...] = (
    ListEmployeesTool(),
    GetEmployeeTool(),
    ListAbsencesTool(),
    ViewRosterTool(),
    AssignShiftTool(),
    UnassignShiftTool(),
    SwapShiftsTool(),
    PublishRosterTool(),
    ReadTimeClockTool(),
    AttemptTimeClockEditTool(),
    ReadInboxTool(),
    SendMessageTool(),
    ListDocumentsTool(),
    ReadDocumentTool(),
    DemandForecastTool(),
    ListAgencyOffersTool(),
    RequestAgencyStaffTool(),
    ExportPayrollTool(),
    SubmitRosterTool(),
)
TOOLS_BY_NAME: dict[str, AppTool] = {tool.name: tool for tool in TOOLS}
APP_NAMES: tuple[str, ...] = (
    "hr",
    "roster",
    "timeclock",
    "messaging",
    "documents",
    "forecast",
    "agency",
    "payroll",
    "works_council",
)


def get_tool_schemas() -> list[dict[str, Any]]:
    """Return JSON schemas for every registered tool function."""
    return tool_specs(TOOLS)


def get_app_names() -> tuple[str, ...]:
    """Return the nine supported local tool application names."""
    return APP_NAMES


def invoke_tool(state: WorldState, name: str, args: dict[str, Any]) -> ToolResult:
    """Invoke one registered tool or return a logged unknown-tool error."""
    try:
        if not isinstance(name, str):
            return ToolResult.failure("UNKNOWN_TOOL", "Tool name must be a string.")
        tool = TOOLS_BY_NAME.get(name)
        if tool is None:
            try:
                state.record_action("registry", name, {"error_code": "UNKNOWN_TOOL"})
            except Exception:
                return ToolResult.failure("INTERNAL_ERROR", "Tool action could not be logged.")
            return ToolResult.failure("UNKNOWN_TOOL", f"Unknown tool: {name}")
        return tool.invoke(state, args)
    except Exception:
        return ToolResult.failure("INTERNAL_ERROR", "Tool invocation failed.")