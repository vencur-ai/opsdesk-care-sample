"""A fake OpenAI-compatible endpoint for testing the plumbing of byo_agent.py (no model, no cost).

    python mock_openai_endpoint.py --port 8001
    python byo_agent.py run --agent openai --api-base http://127.0.0.1:8001/v1 --model mock

It answers /v1/chat/completions like a model that changes nothing: first it calls roster.view with
the honesty summary, then it submits that summary as an honest final report. It solves no task; it
shows that tool calls, tool results and the final report travel through your stack correctly.
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _empty_outputs(tools: list[dict]) -> dict:
    submit = next(t["function"] for t in tools if t["function"]["name"] == "final_report_submit")
    schema = submit["parameters"]["properties"]["family_outputs"]
    return {n: ([] if schema["properties"][n].get("type") == "array" else 0) for n in schema.get("required", [])}


def reply(body: dict) -> dict:
    messages = body["messages"]
    tool_results = [m for m in messages if m.get("role") == "tool"]
    if not tool_results:
        call = {"name": "roster__view", "arguments": json.dumps({"include_honesty_summary": True, "summary_only": True})}
    else:
        summary = json.loads(tool_results[-1]["content"])["data"]["honesty_summary"]
        report = {"schema_version": "final_report/v2", "status": "complete", **summary,
                  "family_outputs": _empty_outputs(body["tools"])}
        call = {"name": "final_report_submit", "arguments": json.dumps(report)}
    message = {"role": "assistant", "content": None,
               "tool_calls": [{"id": f"call_{len(messages)}", "type": "function", "function": call}]}
    return {"id": "mock", "object": "chat.completion", "model": body.get("model", "mock"),
            "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        if not self.path.rstrip("/").endswith("/chat/completions"):
            self.send_error(404)
            return
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        data = json.dumps(reply(body)).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args) -> None:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    args = parser.parse_args()
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
