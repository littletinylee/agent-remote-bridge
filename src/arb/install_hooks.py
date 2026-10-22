"""Install / remove the Claude Code hooks for this bridge.

Editing a user's `~/.claude/settings.json` is invasive, so this module is
deliberately conservative:

* it only ever *adds* our own hook entries, or removes exactly those - never
  other people's hooks;
* it keeps a timestamped backup before the first modification;
* it is idempotent (running install twice does not duplicate anything);
* every operation can be dry-run.

The hook command points at a small wrapper script under ``ARB_HOME`` instead of
``python -m arb ...`` directly: agents start their hooks from their own
environment, which does not have this repo on ``PYTHONPATH``.
"""

from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any

HOOK_MARKER = "arb-hook"

# event -> (matcher or None, timeout seconds)
CLAUDE_EVENTS: dict[str, tuple[str | None, int]] = {
    "SessionStart": (None, 45),
    "UserPromptSubmit": (None, 45),
    "Notification": (None, 45),
    "Stop": (None, 60),
    "SubagentStop": (None, 60),
    "SessionEnd": (None, 45),
    "PreToolUse": ("Bash|Write|Edit|MultiEdit|NotebookEdit", 900),
}


def default_settings_path() -> Path:
    return Path.home() / ".claude" / "settings.json"


def src_dir() -> Path:
    return Path(__file__).resolve().parents[1]


def write_wrappers(arb_home: Path, python_exe: str) -> dict[str, Path]:
    """Create the standalone launchers the agents will call."""
    arb_home.mkdir(parents=True, exist_ok=True)
    src = src_dir().as_posix()
    hook = arb_home / "arb-hook.cmd"
    mcp = arb_home / "arb-mcp.cmd"
    header = '@echo off\nset "PYTHONPATH=' + src + ';%PYTHONPATH%"\n'
    hook.write_text(header + f'"{python_exe}" -m arb hook claude-code\n', encoding="ascii")
    mcp.write_text(header + f'"{python_exe}" -m arb mcp %*\n', encoding="ascii")
    return {"hook": hook, "mcp": mcp}


def hook_command(arb_home: Path) -> str:
    return f'"{arb_home / "arb-hook.cmd"}"'


def mcp_command(arb_home: Path) -> str:
    return f'"{arb_home / "arb-mcp.cmd"}" --agent claude-code'


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    raw = path.read_text(encoding="utf-8-sig").strip()
    return json.loads(raw) if raw else {}


def _save(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    dest = path.with_name(f"{path.name}.arb-backup-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(path, dest)
    return dest


def _is_ours(entry: dict[str, Any]) -> bool:
    cmd = entry.get("command")
    return isinstance(cmd, str) and HOOK_MARKER in cmd


def install(
    settings_path: Path,
    arb_home: Path,
    python_exe: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    python_exe = python_exe or sys.executable
    command = hook_command(arb_home)
    data = _load(settings_path)
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("settings.json 的 hooks 字段不是对象，已中止（未修改）")

    added: list[str] = []
    for event, (matcher, timeout) in CLAUDE_EVENTS.items():
        blocks = hooks.get(event)
        if not isinstance(blocks, list):
            blocks = []
            hooks[event] = blocks
        already = any(
            _is_ours(entry)
            for block in blocks
            if isinstance(block, dict)
            for entry in (block.get("hooks") or [])
            if isinstance(entry, dict)
        )
        if already:
            continue
        entry = {"type": "command", "command": command, "timeout": timeout}
        block: dict[str, Any] = {"hooks": [entry]}
        if matcher:
            block["matcher"] = matcher
        blocks.append(block)
        added.append(event)

    if dry_run:
        return {"changed": bool(added), "added": added, "dry_run": True, "path": str(settings_path)}

    wrappers = write_wrappers(arb_home, python_exe)
    if not added:
        return {
            "changed": False,
            "added": [],
            "path": str(settings_path),
            "wrappers": {k: str(v) for k, v in wrappers.items()},
        }
    backup = _backup(settings_path)
    _save(settings_path, data)
    return {
        "changed": True,
        "added": added,
        "backup": str(backup) if backup else None,
        "path": str(settings_path),
        "wrappers": {k: str(v) for k, v in wrappers.items()},
    }


def uninstall(settings_path: Path, dry_run: bool = False) -> dict[str, Any]:
    if not settings_path.exists():
        return {"changed": False, "removed": [], "path": str(settings_path)}
    data = _load(settings_path)
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return {"changed": False, "removed": [], "path": str(settings_path)}

    removed: list[str] = []
    for event in list(hooks.keys()):
        blocks = hooks.get(event)
        if not isinstance(blocks, list):
            continue
        new_blocks = []
        touched = False
        for block in blocks:
            if not isinstance(block, dict):
                continue
            entries = block.get("hooks") or []
            if not isinstance(entries, list):
                new_blocks.append(block)
                continue
            kept = [e for e in entries if not (isinstance(e, dict) and _is_ours(e))]
            if len(kept) != len(entries):
                touched = True
                if not kept:
                    continue  # drop the now-empty block
                block = {**block, "hooks": kept}
            new_blocks.append(block)
        if touched:
            removed.append(event)
            if new_blocks:
                hooks[event] = new_blocks
            else:
                hooks.pop(event, None)

    if dry_run or not removed:
        return {"changed": bool(removed), "removed": removed, "dry_run": dry_run, "path": str(settings_path)}

    backup = _backup(settings_path)
    _save(settings_path, data)
    return {"changed": True, "removed": removed, "backup": str(backup) if backup else None, "path": str(settings_path)}


def status(settings_path: Path) -> dict[str, Any]:
    data = _load(settings_path)
    hooks = data.get("hooks") if isinstance(data.get("hooks"), dict) else {}
    events = []
    for event, blocks in hooks.items():
        if not isinstance(blocks, list):
            continue
        n = sum(
            1
            for block in blocks
            if isinstance(block, dict)
            for entry in (block.get("hooks") or [])
            if isinstance(entry, dict) and _is_ours(entry)
        )
        if n:
            events.append({"event": event, "entries": n})
    return {"path": str(settings_path), "installed": bool(events), "events": events}
