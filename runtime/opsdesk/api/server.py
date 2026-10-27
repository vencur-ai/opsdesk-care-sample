"""Offline, gym-style HTTP API for OpsDesk."""

from __future__ import annotations

import os
import hmac
import hashlib
import json
import logging
import secrets
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from opsdesk import __version__
from opsdesk.api.session import (
    EpisodeCapacityExceeded,
    EpisodeManager,
    EpisodeQuotaExceeded,
    EpisodeSession,
    SeedMismatchError,
)
from opsdesk.apps.registry import get_tool_schemas
from opsdesk.core.audit import verify_steps
from opsdesk.integrations.function_call import make_router as make_adapter_router
from opsdesk.verifiers.report_parser import final_report_submit_tool


class ResetRequest(BaseModel):
    task_id: str = Field(min_length=1)
    seed: int | None = None


class StepRequest(BaseModel):
    episode_id: str
    tool: str = Field(min_length=1)
    args: dict[str, Any] = Field(default_factory=dict)


class SubmitRequest(BaseModel):
    episode_id: str
    final_report: Any


class RequestLimitMiddleware:
    """Bound request memory and reject excessively nested JSON before parsing."""

    def __init__(self, app, max_bytes: int = 1_048_576, max_depth: int = 64):
        self.app = app
        self.max_bytes = max_bytes
        self.max_depth = max_depth

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        length = headers.get(b"content-length")
        if length:
            try:
                if int(length) > self.max_bytes:
                    return await self._reject(send, "REQUEST_TOO_LARGE")
            except ValueError:
                return await self._reject(send, "INVALID_CONTENT_LENGTH")
        chunks: list[bytes] = []
        total = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            total += len(chunk)
            if total > self.max_bytes:
                return await self._reject(send, "REQUEST_TOO_LARGE")
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        body = b"".join(chunks)
        if self._too_deep(body):
            return await self._reject(send, "JSON_TOO_DEEP")
        delivered = False

        async def replay():
            nonlocal delivered
            if delivered:
                return {"type": "http.request", "body": b"", "more_body": False}
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}

        return await self.app(scope, replay, send)

    def _too_deep(self, body: bytes) -> bool:
        depth = 0
        in_string = False
        escaped = False
        for byte in body:
            if in_string:
                if escaped:
                    escaped = False
                elif byte == 0x5C:
                    escaped = True
                elif byte == 0x22:
                    in_string = False
                continue
            if byte == 0x22:
                in_string = True
            elif byte in (0x7B, 0x5B):
                depth += 1
                if depth > self.max_depth:
                    return True
            elif byte in (0x7D, 0x5D):
                depth = max(0, depth - 1)
        return False

    @staticmethod
    async def _reject(send, code: str):
        payload = json.dumps({
            "ok": False, "error_code": code,
            "message": "Request body exceeds the allowed size or nesting depth.",
        }).encode()
        await send({
            "type": "http.response.start", "status": 413,
            "headers": [(b"content-type", b"application/json"),
                        (b"content-length", str(len(payload)).encode())],
        })
        await send({"type": "http.response.body", "body": payload})


def create_app(
    tasks_root: str | Path | None = None,
    session_class: type[EpisodeSession] = EpisodeSession,
) -> FastAPI:
    root = tasks_root or os.environ.get("OPSDESK_TASKS_ROOT", "tasks")
    app = FastAPI(
        title="OpsDesk Episode API",
        version=__version__,
        description=(
            "Offline gym-style API for isolated scheduling episodes. "
            "State, audit, and scoring routes never expose reference solutions."
        ),
    )
    app.add_middleware(RequestLimitMiddleware)
    app.state.episodes = EpisodeManager(
        root, session_class=session_class,
        max_episodes=int(os.environ.get("OPSDESK_MAX_EPISODES", "100")),
        max_episodes_per_client=int(
            os.environ.get("OPSDESK_MAX_EPISODES_PER_CLIENT", "5")
        ),
        idle_timeout=int(os.environ.get("OPSDESK_IDLE_TIMEOUT_SECONDS", "3600")),
        new_episode_timeout=int(
            os.environ.get("OPSDESK_NEW_EPISODE_TIMEOUT_SECONDS", "120")
        ),
        max_scored_results=int(
            os.environ.get("OPSDESK_SCORE_CACHE_MAX_ENTRIES", "10000")
        ),
        max_audit_archives=int(
            os.environ.get("OPSDESK_AUDIT_ARCHIVE_MAX_ENTRIES", "1000")
        ),
    )
    app.state.agent_keys: dict[str, str] = {}
    app.include_router(make_adapter_router())
    # Earlier samples used another prefix for the same adapter; hosts that still need it set
    # OPSDESK_LEGACY_ADAPTER_PREFIX (it stays out of the published API schema).
    legacy_prefix = os.environ.get("OPSDESK_LEGACY_ADAPTER_PREFIX")
    if legacy_prefix:
        app.include_router(make_adapter_router(legacy_prefix, include_in_schema=False))
    api_key = os.environ.get("OPSDESK_API_KEY")
    host_key = os.environ.get("OPSDESK_HOST_KEY")
    if not api_key:
        logging.getLogger("opsdesk.api").warning("OPSDESK_API_KEY unset; API authentication is disabled.")

    def host_access_error(request: Request):
        supplied = request.headers.get("x-opsdesk-host-key", "")
        if host_key and hmac.compare_digest(supplied, host_key):
            return None
        return JSONResponse(
            status_code=401,
            content={
                "ok": False,
                "error_code": "HOST_KEY_REQUIRED",
                "message": "A valid host key is required for this route.",
            },
        )

    def issued_agent_owner(request: Request) -> str | None:
        key = request.headers.get("x-opsdesk-agent-key")
        if not key:
            return None
        return app.state.agent_keys.get(hashlib.sha256(key.encode("utf-8")).hexdigest())

    def episode_owner(request: Request) -> str:
        issued = issued_agent_owner(request)
        if issued:
            return issued
        address = request.client.host if request.client else "unknown"
        if os.environ.get("OPSDESK_TRUST_PROXY", "0").lower() in {"1", "true", "yes"}:
            forwarded = request.headers.get("x-forwarded-for", "")
            if forwarded:
                address = forwarded.split(",", 1)[0].strip() or address
        return "ip:" + address

    def prune_agent_keys() -> None:
        active_owners = {
            app.state.episodes.episode_owners.get(episode_id)
            for episode_id, (episode, _last_used) in app.state.episodes.episodes.items()
            if not episode.locked and episode.final_report is None
        }
        for digest, owner_id in list(app.state.agent_keys.items()):
            if owner_id not in active_owners:
                del app.state.agent_keys[digest]

    @app.get("/viewer", response_class=HTMLResponse, include_in_schema=False)
    def viewer():
        page = """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>OpsDesk episode viewer</title>
<style>
body{font:15px system-ui,sans-serif;margin:2rem auto;padding:0 1rem;max-width:1100px;color:#17212b;background:#f5f7fa}
header,section{background:white;border:1px solid #dce3ea;border-radius:12px;padding:1rem;margin:1rem 0}
input,button{font:inherit;padding:.55rem .7rem;border:1px solid #9aa9b8;border-radius:6px}
button{background:#173b57;color:white;cursor:pointer}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f2f5f8;padding:1rem;border-radius:8px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:1rem}.ok{color:#087443}.bad{color:#b42318}
</style><body><header><h1>OpsDesk episode viewer</h1>
<p>Agent-visible state, server-recorded steps, hash-chain validity, and score reasons. No references or hidden truth are shown.</p>
<label>Host key <input id="host-key" type="password" autocomplete="current-password"></label>
<label>Episode ID <input id="episode" size="36"></label> <button id="load">Load</button>
<button onclick="window.print()">Print / save screenshot</button><p id="status" role="status"></p></header>
<div class="grid"><section><h2>Roster and current state</h2><pre id="state">Not loaded</pre></section>
<section><h2>Step-by-step audit chain</h2><p id="chain"></p><pre id="audit">Not loaded</pre></section></div>
<section><h2>Score and reason points</h2><pre id="score">Available after submit</pre></section>
<script>
const $=id=>document.getElementById(id);const query=new URLSearchParams(location.search);
const hostHeaders=()=>({"X-OpsDesk-Host-Key":$("host-key").value});
if(query.has("episode_id"))$("episode").value=query.get("episode_id");
$("load").onclick=async()=>{const id=$("episode").value.trim();if(!id)return;
$("status").textContent="Loading…";
try{const [s,a]=await Promise.all([
fetch("/state?episode_id="+encodeURIComponent(id),{headers:hostHeaders()}).then(r=>r.json()),
fetch("/audit?episode_id="+encodeURIComponent(id),{headers:hostHeaders()}).then(r=>r.json())]);
$("state").textContent=JSON.stringify(s,null,2);$("audit").textContent=JSON.stringify(a.steps||a,null,2);
$("chain").textContent="head: "+(a.head||"unavailable")+" · valid: "+a.valid;
$("chain").className=a.valid?"ok":"bad";
const r=await fetch("/score?episode_id="+encodeURIComponent(id),{headers:hostHeaders()});const result=await r.json();
$("score").textContent=r.ok?JSON.stringify(result,null,2):"Score available only after submit.";
$("status").textContent="Loaded";
}catch(e){$("status").textContent="Could not load episode.";}}
;</script></body></html>"""
        return HTMLResponse(page, headers={
            "Cache-Control": "no-store, max-age=0",
            "Pragma": "no-cache",
        })

    @app.middleware("http")
    async def optional_api_key(request: Request, call_next):
        if api_key and not hmac.compare_digest(request.headers.get("x-api-key", ""), api_key):
            return JSONResponse(status_code=401, content={
                "ok": False, "error_code": "UNAUTHORIZED", "message": "Valid API key required.",
            })
        return await call_next(request)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request: Request, _exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content={
                "ok": False,
                "error_code": "INVALID_REQUEST",
                "message": "Invalid request body.",
                "errors": [
                    {
                        "path": ".".join(str(part) for part in error["loc"]),
                        "message": error["msg"],
                    }
                    for error in _exc.errors()
                ],
            },
        )

    @app.post("/reset")
    def reset(body: ResetRequest, request: Request):
        try:
            prune_agent_keys()
            owner_id = episode_owner(request)
            agent_key = request.headers.get("x-opsdesk-agent-key")
            has_issued_key = issued_agent_owner(request) is not None
            result = app.state.episodes.reset(
                body.task_id, body.seed, owner_id=owner_id
            )
            if not has_issued_key:
                agent_key = secrets.token_urlsafe(32)
                app.state.agent_keys[
                    hashlib.sha256(agent_key.encode("utf-8")).hexdigest()
                ] = owner_id
            result["agent_key"] = agent_key
            return result
        except SeedMismatchError as exc:
            return JSONResponse(status_code=400, content={
                "ok": False, "error_code": "INVALID_SEED", "message": str(exc),
            })
        except FileNotFoundError:
            return JSONResponse(status_code=404, content={
                "ok": False, "error_code": "TASK_NOT_FOUND", "message": "Task not found.",
            })
        except EpisodeQuotaExceeded:
            return JSONResponse(status_code=429, content={
                "ok": False, "error_code": "QUOTA_EXCEEDED",
                "message": "Too many unsubmitted episodes for this client.",
            })
        except EpisodeCapacityExceeded:
            return JSONResponse(status_code=429, content={
                "ok": False, "error_code": "CAPACITY_FULL",
                "message": "Server episode capacity is full.",
            })
        except Exception:
            return JSONResponse(status_code=400, content={
                "ok": False, "error_code": "RESET_FAILED", "message": "Episode could not be reset.",
            })

    @app.delete("/episode")
    def delete_episode(episode_id: str, request: Request):
        if host_access_error(request) is None:
            deleted = app.state.episodes.host_delete_episode(episode_id)
            if not deleted:
                return JSONResponse(status_code=404, content={
                    "ok": False, "error_code": "EPISODE_NOT_FOUND",
                    "message": "Episode not found or expired.",
                })
            return {"deleted": True, "episode_id": episode_id}
        deleted = app.state.episodes.delete_episode(
            episode_id, owner_id=episode_owner(request)
        )
        if deleted is None:
            return JSONResponse(status_code=404, content={
                "ok": False, "error_code": "EPISODE_NOT_FOUND",
                "message": "Episode not found or expired.",
            })
        if not deleted:
            return JSONResponse(status_code=403, content={
                "ok": False, "error_code": "EPISODE_OWNER_REQUIRED",
                "message": "Only the creating client can delete this episode.",
            })
        return {"deleted": True, "episode_id": episode_id}

    @app.post("/step")
    def step(body: StepRequest):
        try:
            return app.state.episodes.get(body.episode_id).step(body.tool, body.args)
        except KeyError:
            return JSONResponse(status_code=409, content={
                "ok": False, "error_code": "EPISODE_NOT_FOUND", "message": "Episode not found or expired.",
            })
        except RuntimeError:
            return JSONResponse(status_code=409, content={"ok": False, "error_code": "NO_ACTIVE_EPISODE", "message": "Reset an episode first."})
        except Exception:
            return JSONResponse(status_code=500, content={
                "ok": False, "error_code": "INTERNAL_ERROR", "message": "Tool step failed.",
            })

    @app.get("/state")
    def state(episode_id: str, request: Request):
        denied = host_access_error(request)
        if denied:
            return denied
        try:
            return app.state.episodes.get(episode_id).observation()
        except KeyError:
            return JSONResponse(status_code=409, content={
                "ok": False, "error_code": "EPISODE_NOT_FOUND", "message": "Episode not found or expired.",
            })

    @app.get("/audit")
    def audit(episode_id: str, request: Request):
        denied = host_access_error(request)
        if denied:
            return denied
        try:
            audit_data = app.state.episodes.get_audit(episode_id)
            steps = audit_data["steps"]
            head = audit_data["head"]
            return {
                "episode_id": episode_id,
                "steps": steps,
                "head": head,
                "valid": verify_steps(episode_id, steps, head),
            }
        except KeyError:
            return JSONResponse(status_code=409, content={
                "ok": False, "error_code": "EPISODE_NOT_FOUND", "message": "Episode not found or expired.",
            })

    @app.get("/export")
    def export(episode_id: str, request: Request):
        """Host-only: full episode export for the offline CLI scorer (open episodes)."""
        denied = host_access_error(request)
        if denied:
            return denied
        try:
            return app.state.episodes.get(episode_id).export()
        except KeyError:
            return JSONResponse(status_code=409, content={
                "ok": False, "error_code": "EPISODE_NOT_FOUND", "message": "Episode not found or expired.",
            })

    @app.post("/submit")
    def submit(body: SubmitRequest):
        try:
            result = app.state.episodes.get(body.episode_id).submit(body.final_report)
            if not result["accepted"]:
                return JSONResponse(status_code=409, content=result)
            return result
        except KeyError:
            return JSONResponse(status_code=409, content={
                "accepted": False, "error_code": "EPISODE_NOT_FOUND", "message": "Episode not found or expired.",
            })

    @app.get("/score")
    def score(episode_id: str, request: Request):
        denied = host_access_error(request)
        if denied:
            return denied
        try:
            result = app.state.episodes.score(episode_id)
            if result.get("error_code") == "SCORING_FAILED":
                return JSONResponse(status_code=500, content=result)
            return result
        except KeyError:
            return JSONResponse(status_code=409, content={"ok": False, "error_code": "EPISODE_NOT_FOUND", "message": "Episode not found or expired."})
        except RuntimeError:
            return JSONResponse(status_code=409, content={
                "ok": False, "error_code": "SUBMIT_REQUIRED", "message": "Submit a final report before scoring.",
            })
        except Exception:
            return JSONResponse(status_code=500, content={
                "ok": False, "error_code": "SCORING_FAILED", "message": "Episode could not be scored.",
            })

    @app.delete("/score")
    def delete_score(episode_id: str, request: Request):
        denied = host_access_error(request)
        if denied:
            return denied
        deleted = app.state.episodes.delete_score(episode_id)
        return {"deleted": deleted, "episode_id": episode_id}

    @app.get("/score-cache")
    def score_cache(request: Request):
        denied = host_access_error(request)
        if denied:
            return denied
        return app.state.episodes.score_cache_info()

    @app.get("/tools")
    def tools(episode_id: str | None = None):
        result = get_tool_schemas() + list(getattr(session_class, "EXTRA_TOOL_SCHEMAS", ()))
        family = None
        if episode_id:
            try:
                family = (app.state.episodes.get(episode_id).task or {}).get("family")
            except KeyError:
                return JSONResponse(status_code=409, content={"error_code": "EPISODE_NOT_FOUND"})
        if not any(item.get("name") == "final_report_submit" for item in result):
            result.append(final_report_submit_tool(family))
        return result

    return app


app = create_app()
