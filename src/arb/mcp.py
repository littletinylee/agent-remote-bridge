"""MCP stdio server: the universal agent-facing surface.

Any MCP-capable agent (Claude Code, Codex, DSH, OpenCode, …) gets three tools
without touching its own workflow:

    ask_human(prompt, options, dangerous)  -> blocks only *that* agent's turn
    notify(title, body, kind)              -> fire-and-forget push
    progress(message)                      -> throttled progress push

The blocking `ask_human` call is what makes "resume the original session"
trivial: the answer is returned as the tool result, so the agent continues in
its own context. Other agents keep running because this is per-process stdio.
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
from typing import Any

from .agents import AGENT_GENERIC, generic_context
from .client import ApiError, Client, DaemonUnavailable
from .config import load
from .util import json_dumps, log_stderr, one_line, truncate

SERVER_NAME = "agent-remote-bridge"
SERVER_VERSION = "1.0.0"
PROTOCOL_VERSION = "2024-11-05"

TOOLS = [
    {
        "name": "ask_human",
        "description": (
            "Ask the human for a decision through WeChat and block until they answer. "
            "Use this whenever you need a judgement call, a permission, or a choice between "
            "options. The answer is returned to you in this same conversation, so after it "
            "returns you simply continue your work. Do NOT use it for rhetorical questions. "
            "If it returns status=timeout/unavailable, treat the operation as NOT approved."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "The question, including enough context to decide."},
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional choices. If omitted the human replies with free text.",
                },
                "timeout_seconds": {"type": "integer", "description": "How long to wait. Default 900."},
                "dangerous": {
                    "type": "boolean",
                    "description": "Mark destructive actions. Approval then requires an explicit 'yes' and failure/timeout means DENIED.",
                },
                "context": {"type": "string", "description": "Short context snippet shown to the human."},
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "notify",
        "description": "Push a notification to the human's WeChat (task start, notable progress, error, completion).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "body": {"type": "string"},
                "kind": {"type": "string", "enum": ["start", "progress", "done", "error", "attention", "info"]},
            },
            "required": ["title"],
        },
    },
    {
        "name": "progress",
        "description": "Push a short progress update (throttled by the bridge).",
        "inputSchema": {
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
        },
    },
    {
        "name": "check_answer",
        "description": "Non-blocking check: has the human answered question #CODE yet?",
        "inputSchema": {
            "type": "object",
            "properties": {"code": {"type": "string"}},
            "required": ["code"],
        },
    },
    {
        "name": "list_pending",
        "description": "List questions currently waiting for the human.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _text(payload: Any, is_error: bool = False) -> dict[str, Any]:
    body = payload if isinstance(payload, str) else json_dumps(payload)
    return {"content": [{"type": "text", "text": body}], "isError": is_error}


class McpServer:
    def __init__(self, agent: str = AGENT_GENERIC):
        self.cfg = load()
        self.client = Client(self.cfg)
        self.agent = agent or os.environ.get("ARB_AGENT") or AGENT_GENERIC
        self.session: dict[str, Any] | None = None
        self.ctx = generic_context(self.agent)
        self._started = time.time()

    # -- session ---------------------------------------------------------
    def ensure_session(self) -> dict[str, Any]:
        if self.session is not None:
            return self.session
        info = self.client.ensure()
        self.session = self.client.session(
            agent=self.ctx["agent"],
            project=self.ctx["project"],
            cwd=self.ctx["cwd"],
            label=self.ctx.get("label") or "",
            parent_id=self.ctx.get("parent_id"),
            key=self.ctx.get("key"),
            meta={"mcp_pid": os.getpid()},
        )
        return self.session

    # -- tools -----------------------------------------------------------
    def tool_ask_human(self, args: dict[str, Any]) -> dict[str, Any]:
        prompt = str(args.get("prompt") or "").strip()
        if not prompt:
            return _text({"status": "error", "error": "prompt is required"}, is_error=True)
        timeout = int(args.get("timeout_seconds") or self.cfg.ask_timeout_s)
        options = [str(o) for o in (args.get("options") or [])]
        dangerous = bool(args.get("dangerous"))
        try:
            sess = self.ensure_session()
            resp = self.client.ask(
                session_id=sess["id"],
                prompt=prompt,
                options=options,
                timeout_s=timeout,
                dangerous=dangerous,
                context={"note": truncate(str(args.get("context") or ""), 500)},
            )
        except (DaemonUnavailable, ApiError) as exc:
            return _text(
                {
                    "status": "unavailable",
                    "error": str(exc),
                    "guidance": "The remote bridge is offline. Do NOT assume approval: stop and report the question locally.",
                },
                is_error=True,
            )

        q = resp["question"]
        code = q["code"]
        deadline = time.time() + timeout + 5
        while time.time() < deadline:
            q = self.client.question(id=q["id"])["question"]
            if q["state"] != "pending":
                break
            remaining = max(1.0, min(30.0, deadline - time.time()))
            try:
                q = self.client.wait(q["id"], remaining)["question"]
            except (DaemonUnavailable, ApiError) as exc:
                return _text(
                    {"status": "unavailable", "code": code, "error": str(exc), "approval": False},
                    is_error=True,
                )
            if q["state"] != "pending":
                break

        if q["state"] == "answered":
            return _text(
                {
                    "status": "answered",
                    "code": code,
                    "answer": q.get("answer"),
                    "answer_raw": q.get("answer_raw"),
                    "question": prompt,
                    "session": sess["project"],
                }
            )
        if q["state"] == "expired":
            return _text(
                {
                    "status": "timeout",
                    "code": code,
                    "approval": False,
                    "guidance": "No answer arrived. Treat as refusal; do not perform destructive actions.",
                }
            )
        if q["state"] == "cancelled":
            return _text({"status": "cancelled", "code": code, "approval": False})
        return _text(
            {
                "status": "pending",
                "code": code,
                "guidance": f"Still waiting. Call check_answer with code {code}, or continue unrelated work and check later.",
            }
        )

    def tool_notify(self, args: dict[str, Any]) -> dict[str, Any]:
        try:
            sess = self.ensure_session()
            self.client.notify(
                session_id=sess["id"],
                title=str(args.get("title") or ""),
                body=str(args.get("body") or ""),
                kind=str(args.get("kind") or "info"),
            )
            return _text({"status": "queued"})
        except (DaemonUnavailable, ApiError) as exc:
            return _text({"status": "unavailable", "error": str(exc)}, is_error=True)

    def tool_progress(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.tool_notify(
            {"title": one_line(str(args.get("message") or ""), 120), "body": "", "kind": "progress"}
        )

    def tool_check_answer(self, args: dict[str, Any]) -> dict[str, Any]:
        code = str(args.get("code") or "").strip().lstrip("#")
        try:
            q = self.client.question(code=code)["question"]
        except (DaemonUnavailable, ApiError) as exc:
            return _text({"status": "unavailable", "error": str(exc)}, is_error=True)
        return _text({"status": q["state"], "code": q["code"], "answer": q["answer"]})

    def tool_list_pending(self, args: dict[str, Any]) -> dict[str, Any]:
        try:
            pending = self.client.pending()["pending"]
        except (DaemonUnavailable, ApiError) as exc:
            return _text({"status": "unavailable", "error": str(exc)}, is_error=True)
        return _text(
            {
                "pending": [
                    {"code": q["code"], "prompt": one_line(q["prompt"], 120), "dangerous": q["dangerous"]}
                    for q in pending
                ]
            }
        )

    # -- protocol --------------------------------------------------------
    def handle(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        method = msg.get("method")
        msg_id = msg.get("id")
        params = msg.get("params") or {}
        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                },
            }
        if method in {"notifications/initialized", "initialized"}:
            return None
        if method == "ping":
            return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}}
        if method == "tools/call":
            name = params.get("name")
            args = params.get("arguments") or {}
            handlers = {
                "ask_human": self.tool_ask_human,
                "notify": self.tool_notify,
                "progress": self.tool_progress,
                "check_answer": self.tool_check_answer,
                "list_pending": self.tool_list_pending,
            }
            fn = handlers.get(name)
            if fn is None:
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": -32601, "message": f"unknown tool: {name}"},
                }
            try:
                result = fn(args)
            except Exception as exc:  # pragma: no cover - defensive
                log_stderr(traceback.format_exc())
                result = _text({"status": "error", "error": str(exc)}, is_error=True)
            return {"jsonrpc": "2.0", "id": msg_id, "result": result}
        if msg_id is None:
            return None  # notification we don't care about
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32601, "message": f"method not found: {method}"},
        }

    # -- stdio loop ------------------------------------------------------
    def run(self) -> int:
        stdin = sys.stdin.buffer
        stdout = sys.stdout.buffer
        while True:
            frame = read_frame(stdin)
            if frame is None:
                return 0
            try:
                msg = json.loads(frame)
            except ValueError:
                write_frame(stdout, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
                continue
            try:
                resp = self.handle(msg)
            except Exception as exc:  # pragma: no cover
                log_stderr(traceback.format_exc())
                resp = {"jsonrpc": "2.0", "id": msg.get("id"), "error": {"code": -32603, "message": str(exc)}}
            if resp is not None:
                write_frame(stdout, resp)


def read_frame(stdin) -> bytes | None:
    """Read one JSON-RPC message: newline-delimited or Content-Length framed."""
    while True:
        line = stdin.readline()
        if not line:
            return None
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.lower().startswith(b"content-length:"):
            try:
                length = int(stripped.split(b":", 1)[1])
            except (IndexError, ValueError):
                continue
            # consume remaining headers
            while True:
                hdr = stdin.readline()
                if hdr in (b"\r\n", b"\n", b""):
                    break
            return stdin.read(length)
        if stripped.startswith(b"{"):
            return stripped
        # unknown preamble (e.g. logs leaking into stdout) - skip it


def write_frame(stdout, payload: dict[str, Any]) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    try:
        stdout.write(data + b"\n")
        stdout.flush()
    except (BrokenPipeError, ValueError):
        pass


def main(agent: str = "") -> int:
    server = McpServer(agent)
    try:
        return server.run()
    except KeyboardInterrupt:
        return 0
