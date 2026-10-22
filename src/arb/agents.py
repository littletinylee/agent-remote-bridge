"""Agent adapters: translate a vendor's hook/notify payload into bridge events.

Adding an agent = adding one small function here plus a recipe in
docs/AGENTS.md. Nothing in the daemon knows about vendors.

Verified payloads (real captures, see docs/AGENTS.md):
  claude-code  hook JSON on stdin: hook_event_name, session_id, cwd,
               tool_name, tool_input, message, transcript_path
  codex        notify argv program: {"type":"agent-turn-complete",
               "turn-id":…, "input-messages":[…], "last-assistant-message":…}
  dsh/generic  ARB_* environment variables + `arb notify` / MCP tools
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

from .util import one_line, safe_basename, truncate

AGENT_CLAUDE = "claude-code"
AGENT_CODEX = "codex"
AGENT_DSH = "dsh"
AGENT_GENERIC = "generic"


@dataclass
class AgentEvent:
    agent: str
    event: str
    project: str
    cwd: str
    key: str = ""
    label: str = ""
    parent_id: str | None = None
    title: str = ""
    body: str = ""
    kind: str = "info"
    tool: str = ""
    tool_input: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


def project_from_cwd(cwd: str) -> str:
    cwd = cwd or os.getcwd()
    base = safe_basename(cwd.rstrip("\\/"))
    return base or "project"


def _env_session() -> dict[str, str]:
    return {
        "session_id": os.environ.get("ARB_SESSION_ID", ""),
        "agent": os.environ.get("ARB_AGENT", ""),
        "project": os.environ.get("ARB_PROJECT", ""),
        "label": os.environ.get("ARB_SESSION_LABEL", ""),
        "parent": os.environ.get("ARB_PARENT_SESSION", ""),
    }


# --------------------------------------------------------------------------
# Claude Code
# --------------------------------------------------------------------------
def claude_event(payload: dict[str, Any], cwd: str | None = None) -> AgentEvent:
    env = _env_session()
    workdir = str(payload.get("cwd") or cwd or os.getcwd())
    name = str(payload.get("hook_event_name") or payload.get("event") or "")
    tool = str(payload.get("tool_name") or "")
    tool_input = payload.get("tool_input") or {}
    key = str(payload.get("session_id") or env["session_id"] or workdir)
    project = env["project"] or project_from_cwd(workdir)

    ev = AgentEvent(
        agent=AGENT_CLAUDE,
        event=name,
        project=project,
        cwd=workdir,
        key=key,
        label=env["label"],
        parent_id=(payload.get("parent_session_id") or env["parent"] or None),
        tool=tool,
        tool_input=tool_input if isinstance(tool_input, dict) else {},
        raw=payload,
    )

    if name == "UserPromptSubmit":
        prompt = str(payload.get("prompt") or "")
        ev.kind = "start"
        ev.title = "开始新任务"
        ev.body = one_line(prompt, 200)
    elif name == "Notification":
        ev.kind = "attention"
        ev.title = "需要留意"
        ev.body = one_line(str(payload.get("message") or "Claude 需要注意"), 300)
    elif name == "Stop":
        ev.kind = "done"
        ev.title = "一轮任务结束"
        ev.body = one_line(_transcript_tail(str(payload.get("transcript_path") or "")), 400)
    elif name == "SubagentStop":
        ev.kind = "done"
        ev.title = "子智能体结束"
    elif name == "SessionStart":
        ev.kind = "start"
        ev.title = "会话已连接"
    elif name == "PreToolUse":
        ev.kind = "approval"
        ev.title = f"请求使用工具 {tool or '未知'}"
        ev.body = describe_tool(tool, ev.tool_input)
    elif name == "SessionEnd":
        ev.kind = "done"
        ev.title = "会话结束"
    else:
        ev.kind = "info"
        ev.title = name or "事件"
    return ev


def _transcript_tail(path: str, limit: int = 6) -> str:
    """Last assistant text from a Claude transcript JSONL (best effort)."""
    if not path or not os.path.exists(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()[-80:]
    except OSError:
        return ""
    text = ""
    for line in reversed(lines):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("type") != "assistant":
            continue
        msg = rec.get("message") or {}
        parts = msg.get("content") or []
        if isinstance(parts, str):
            text = parts
        else:
            for part in parts:
                if isinstance(part, dict) and part.get("type") == "text":
                    text += str(part.get("text") or "")
        if text.strip():
            break
    return text


def describe_tool(tool: str, tool_input: dict[str, Any]) -> str:
    if tool == "Bash":
        return "$ " + one_line(str(tool_input.get("command") or ""), 300)
    if tool in {"Write", "Edit", "MultiEdit", "NotebookEdit"}:
        path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        extra = ""
        if tool == "Write":
            content = str(tool_input.get("content") or "")
            extra = f" ({len(content)} 字符)"
        elif tool in {"Edit", "MultiEdit"}:
            old = one_line(str(tool_input.get("old_string") or ""), 80)
            new = one_line(str(tool_input.get("new_string") or ""), 80)
            extra = f"\n- {old}\n+ {new}" if old or new else ""
        return f"文件: {path}{extra}"
    if tool == "WebFetch":
        return "URL: " + one_line(str(tool_input.get("url") or ""), 200)
    try:
        return one_line(json.dumps(tool_input, ensure_ascii=False), 300)
    except (TypeError, ValueError):
        return one_line(str(tool_input), 300)


def approval_prompt(ev: AgentEvent) -> str:
    return f"{ev.title}\n{ev.body}" if ev.body else ev.title


def hook_envelope(payload: dict[str, Any], agent: str, cwd: str | None = None) -> AgentEvent:
    """Normalise a *hook* payload (Claude Code and Codex share the shape).

    Codex 0.162 speaks the same hook contract as Claude Code - same event names
    (UserPromptSubmit / PreToolUse / Stop / SessionStart ...) and the same
    `hook_event_name` field - so one mapper serves both, with the agent label
    kept for routing and display.
    """
    ev = claude_event(payload, cwd)
    ev.agent = agent or ev.agent
    return ev


# --------------------------------------------------------------------------
# Codex
# --------------------------------------------------------------------------
def codex_event(payload: dict[str, Any], cwd: str | None = None) -> AgentEvent:
    env = _env_session()
    workdir = str(payload.get("cwd") or cwd or os.getcwd())
    etype = str(payload.get("type") or "")
    key = str(payload.get("thread-id") or payload.get("thread_id") or env["session_id"] or workdir)
    ev = AgentEvent(
        agent=AGENT_CODEX,
        event=etype or "notify",
        project=env["project"] or project_from_cwd(workdir),
        cwd=workdir,
        key=key,
        label=env["label"],
        parent_id=(env["parent"] or None),
        raw=payload,
    )
    if etype == "agent-turn-complete":
        ev.kind = "done"
        ev.title = "任务完成（Codex）"
        last = str(payload.get("last-assistant-message") or "")
        incoming = payload.get("input-messages") or []
        if not last and incoming:
            last = str(incoming[-1])
        ev.body = truncate(last.strip(), 800)
    else:
        ev.kind = "info"
        ev.title = etype or "Codex 事件"
    return ev


# --------------------------------------------------------------------------
# MCP / generic
# --------------------------------------------------------------------------
def generic_context(agent: str = AGENT_GENERIC, cwd: str | None = None, **kw: Any) -> dict[str, Any]:
    env = _env_session()
    workdir = cwd or os.getcwd()
    return {
        "agent": agent or env["agent"] or AGENT_GENERIC,
        "project": kw.get("project") or env["project"] or project_from_cwd(workdir),
        "cwd": workdir,
        "label": kw.get("label") or env["label"],
        "parent_id": kw.get("parent_id") or env["parent"] or None,
        "key": kw.get("key") or env["session_id"] or workdir,
    }


def sniff_agent(payload: dict[str, Any]) -> str:
    """Guess which agent produced a hook payload."""
    if "hook_event_name" in payload or "transcript_path" in payload:
        return AGENT_CLAUDE
    if str(payload.get("type") or "").startswith("agent-turn"):
        return AGENT_CODEX
    env = os.environ.get("ARB_AGENT")
    return env or AGENT_GENERIC
