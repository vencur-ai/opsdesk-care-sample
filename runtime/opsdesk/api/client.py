"""Minimal JSON client for the local OpsDesk HTTP API (used by the MCP adapter)."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any


def _json_request(url: str, *, method: str = "GET", body: Any = None,
                  headers: dict[str, str] | None = None, timeout: float = 30) -> Any:
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


class LocalAPI:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    @staticmethod
    def _headers(content_type: bool = False) -> dict[str, str]:
        headers = {"Content-Type": "application/json"} if content_type else {}
        api_key = os.environ.get("OPSDESK_API_KEY")
        if api_key:
            headers["x-api-key"] = api_key
        agent_key = os.environ.get("OPSDESK_AGENT_KEY")  # set when attaching to an episode started elsewhere
        if agent_key:
            headers["X-OpsDesk-Agent-Key"] = agent_key
        host_key = os.environ.get("OPSDESK_HOST_KEY")  # host-side runner only: allows GET /score
        if host_key:
            headers["X-OpsDesk-Host-Key"] = host_key
        return headers

    def get(self, path: str) -> Any:
        return _json_request(self.base_url + path, headers=self._headers())

    def post(self, path: str, body: Any) -> Any:
        try:
            return _json_request(self.base_url + path, method="POST", body=body,
                                 headers=self._headers(content_type=True))
        except urllib.error.HTTPError as exc:
            if path == "/submit" and exc.code == 409:
                return json.loads(exc.read())
            raise
