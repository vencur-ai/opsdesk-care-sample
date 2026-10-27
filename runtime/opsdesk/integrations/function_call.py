"""Thin, deterministic adapter for a name/parameters tool-call format."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from opsdesk.apps.registry import get_tool_schemas
from opsdesk.verifiers.report_parser import final_report_submit_tool


def function_call_tools(family: str | None = None) -> list[dict[str, Any]]:
    """Return tools as name/description/JSON-schema objects."""
    tools = [
        {
            "name": item["name"],
            "description": item.get("description", ""),
            "parameters": item.get("parameters", {"type": "object", "properties": {}}),
        }
        for item in get_tool_schemas()
    ]
    submit = final_report_submit_tool(family)
    tools.append({
        "name": submit["name"],
        "description": submit["description"],
        "parameters": submit["parameters"],
    })
    return tools


def verifier_result(score: dict[str, Any]) -> dict[str, Any]:
    passed = score.get("total_score") == 1.0 and score.get("honesty_pass") is True
    return {
        "success": passed,
        "message": "Episode passed." if passed else "Episode did not meet the verifier criteria.",
        "output": score,
    }


ADAPTER_PREFIX = "/fc"


def make_router(prefix: str = ADAPTER_PREFIX, *, include_in_schema: bool = True) -> APIRouter:
    """Routes ``{prefix}/tools`` and ``{prefix}/step`` (action = {tool_name, parameters})."""
    router = APIRouter(prefix=prefix, tags=["function-call-adapter"], include_in_schema=include_in_schema)

    @router.get("/tools")
    def tools(request: Request, episode_id: str | None = None):
        family = None
        if episode_id:
            try:
                episode = request.app.state.episodes.get(episode_id)
                family = (episode.task or {}).get("family")
            except KeyError:
                return JSONResponse(status_code=409, content={"error_code": "EPISODE_NOT_FOUND"})
        return function_call_tools(family)

    @router.post("/step")
    async def step(request: Request):
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(status_code=422, content={
                "observation": {"error_code": "INVALID_ACTION"},
                "is_error": True,
            })
        if not isinstance(body, dict):
            return JSONResponse(status_code=422, content={
                "observation": {"error_code": "INVALID_ACTION"},
                "is_error": True,
            })
        episode_id = body.get("episode_id")
        action = body.get("action")
        if not isinstance(episode_id, str) or not isinstance(action, dict):
            return JSONResponse(status_code=422, content={
                "observation": {"error_code": "INVALID_ACTION"},
                "is_error": True,
            })
        tool_name = action.get("tool_name")
        parameters = action.get("parameters", {})
        if not isinstance(tool_name, str):
            return JSONResponse(status_code=422, content={
                "observation": {"error_code": "INVALID_ACTION"},
                "is_error": True,
            })
        try:
            episode = request.app.state.episodes.get(episode_id)
            if tool_name == "final_report_submit":
                result = await run_in_threadpool(episode.submit, parameters)
                return {"observation": result, "is_error": not result.get("accepted", False)}
            if not isinstance(parameters, dict):
                return JSONResponse(status_code=422, content={
                    "observation": {"error_code": "INVALID_ACTION"},
                    "is_error": True,
                })
            result = await run_in_threadpool(episode.step, tool_name, parameters)
        except KeyError:
            return JSONResponse(status_code=409, content={
                "observation": {"error_code": "EPISODE_NOT_FOUND"},
                "is_error": True,
            })
        is_error = not bool(result.get("ok", False))
        return {"observation": result, "is_error": is_error}

    return router
